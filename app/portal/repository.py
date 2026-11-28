"""Postgres queries and guarded mutations used by the Streamlit portal.

The UI deliberately has no filesystem access. All reviewer actions are committed
with their audit event in the same transaction.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.db import connect


class ConcurrentActionError(RuntimeError):
    """The job no longer satisfies the action's state precondition."""


def _job_id(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _actor(reviewer: str) -> str:
    reviewer = reviewer.strip()
    if not reviewer:
        raise ValueError("Enter your reviewer name before changing a job.")
    return f"portal:{reviewer}"


def dashboard() -> dict[str, Any]:
    """Return aggregate queue metrics and recent jobs."""
    with connect(row_factory=dict_row) as conn:
        totals = conn.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE job_status = 'pending') AS pending,
                      count(*) FILTER (WHERE job_status = 'running') AS running,
                      count(*) FILTER (WHERE job_status = 'failed') AS failed,
                      count(*) FILTER (WHERE review_status = 'pending_review') AS pending_review,
                      count(*) FILTER (WHERE review_status = 'approved') AS approved,
                      count(*) FILTER (WHERE review_status = 'rejected') AS rejected
               FROM jobs"""
        ).fetchone()
        recent = conn.execute(
            """SELECT job_id, rfq_id, file_name, job_status, review_status,
                      attempts, max_attempts, created_at, finished_at
               FROM jobs ORDER BY created_at DESC LIMIT 12"""
        ).fetchall()
    return {"totals": totals, "recent": recent}


def list_jobs(
    *, review_status: str | None = None, job_status: str | None = None, search: str = ""
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if review_status:
        clauses.append("j.review_status = %s")
        params.append(review_status)
    if job_status:
        clauses.append("j.job_status = %s")
        params.append(job_status)
    if search.strip():
        clauses.append("(j.rfq_id ILIKE %s OR j.file_name ILIKE %s)")
        term = f"%{search.strip()}%"
        params.extend((term, term))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""SELECT j.job_id, j.rfq_id, j.file_name, j.file_type, j.job_status,
                     j.review_status, j.priority, j.attempts, j.max_attempts,
                     j.escalation_reason, j.created_at, j.started_at, j.finished_at,
                     j.run_after, j.locked_by, j.lease_expires_at,
                     (j.job_status = 'pending' AND j.run_after <= now()) AS ready_to_claim,
                     count(l.line) FILTER (WHERE l.needs_review) AS lines_to_review,
                     count(l.line) AS line_count
              FROM jobs j
              LEFT JOIN quote_lines l ON l.job_id = j.job_id
              {where}
              GROUP BY j.job_id
              ORDER BY coalesce(j.finished_at, j.created_at) DESC"""
    with connect(row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def get_job(job_id: str | UUID) -> dict[str, Any] | None:
    with connect(row_factory=dict_row) as conn:
        return conn.execute("SELECT * FROM jobs WHERE job_id = %s", (_job_id(job_id),)).fetchone()


def get_quote(job_id: str | UUID) -> dict[str, Any] | None:
    with connect(row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT q.job_id, q.rfq_id, q.customer_id, q.customer_name,
                      q.non_item_rows, q.created_at, c.name AS catalog_customer_name,
                      c.pricing_tier
               FROM quotes q LEFT JOIN customers c USING (customer_id)
               WHERE q.job_id = %s""",
            (_job_id(job_id),),
        ).fetchone()


def get_quote_lines(job_id: str | UUID) -> list[dict[str, Any]]:
    with connect(row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT l.*, coalesce(l.corrected_part_number, l.ridgeline_part_number) AS effective_part_number,
                      p.description, p.category, p.unit AS catalog_unit, p.list_price,
                      p.status AS product_status, p.primary_warehouse
               FROM quote_lines l
               LEFT JOIN products p
                 ON p.part_number = coalesce(l.corrected_part_number, l.ridgeline_part_number)
               WHERE l.job_id = %s ORDER BY l.line""",
            (_job_id(job_id),),
        ).fetchall()


def get_events(job_id: str | UUID) -> list[dict[str, Any]]:
    with connect(row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT event, actor, detail, created_at FROM job_events
               WHERE job_id = %s ORDER BY created_at DESC, event_id DESC""",
            (_job_id(job_id),),
        ).fetchall()


def search_products(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Find active catalog candidates by part number or fuzzy description."""
    query = query.strip()
    if not query:
        return []
    with connect(row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT part_number, description, category, unit, list_price, status,
                      primary_warehouse,
                      greatest(similarity(description, %(query)s),
                               similarity(part_number, %(query)s)) AS score
               FROM products
               WHERE status = 'active'
                 AND (part_number ILIKE %(term)s OR description ILIKE %(term)s
                      OR description %% %(query)s OR part_number %% %(query)s)
               ORDER BY (normalize_part_number(part_number) = normalize_part_number(%(query)s)) DESC,
                        score DESC, part_number
               LIMIT %(limit)s""",
            {"query": query, "term": f"%{query}%", "limit": limit},
        ).fetchall()


def correct_part(
    job_id: str | UUID, line: int, part_number: str, reviewer: str
) -> None:
    actor = _actor(reviewer)
    jid = _job_id(job_id)
    with connect() as conn:
        previous = conn.execute(
            "SELECT corrected_part_number FROM quote_lines WHERE job_id = %s AND line = %s",
            (jid, line),
        ).fetchone()
        updated = conn.execute(
            """UPDATE quote_lines l SET corrected_part_number = %s
               FROM jobs j, products p
               WHERE l.job_id = %s AND l.line = %s AND j.job_id = l.job_id
                 AND j.review_status = 'pending_review' AND p.part_number = %s
               RETURNING l.corrected_part_number""",
            (part_number, jid, line, part_number),
        ).fetchone()
        if not updated:
            raise ConcurrentActionError(
                "The quote is no longer awaiting review, or that catalog part does not exist."
            )
        conn.execute(
            "INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, 'part_corrected', %s, %s)",
            (
                jid,
                actor,
                Jsonb(
                    {
                        "line": line,
                        "from": previous[0] if previous else None,
                        "to": part_number,
                    }
                ),
            ),
        )


def approve(job_id: str | UUID, reviewer: str) -> None:
    _transition(
        job_id,
        reviewer,
        event="approved",
        sql="""UPDATE jobs SET review_status = 'approved', reviewed_by = %s, reviewed_at = now()
               WHERE job_id = %s AND job_status = 'succeeded' AND review_status = 'pending_review'""",
    )


def reject(job_id: str | UUID, reviewer: str, reason: str) -> None:
    reason = reason.strip()
    if not reason:
        raise ValueError("A rejection reason is required.")
    _transition(
        job_id,
        reviewer,
        event="rejected",
        sql="""UPDATE jobs SET review_status = 'rejected', reviewed_by = %s,
                      reviewed_at = now(), rejection_reason = %s
               WHERE job_id = %s AND review_status = 'pending_review'""",
        extra_params=(reason,),
        detail={"reason": reason},
    )


def requeue(job_id: str | UUID, reviewer: str) -> None:
    _transition(
        job_id,
        reviewer,
        event="requeued",
        sql="""UPDATE jobs SET job_status = 'pending', review_status = 'not_ready', attempts = 0,
                      run_after = now(), finished_at = NULL, escalation_reason = NULL,
                      last_error = NULL, reviewed_by = NULL, reviewed_at = NULL,
                      rejection_reason = NULL
               WHERE job_id = %s AND job_status = 'failed' AND review_status = 'pending_review'""",
        reviewer_in_update=False,
    )


def cancel(job_id: str | UUID, reviewer: str) -> None:
    _transition(
        job_id,
        reviewer,
        event="cancelled",
        sql="""UPDATE jobs SET job_status = 'cancelled', finished_at = now()
               WHERE job_id = %s AND job_status = 'pending'""",
        reviewer_in_update=False,
    )


def _transition(
    job_id: str | UUID,
    reviewer: str,
    *,
    event: str,
    sql: str,
    extra_params: tuple[Any, ...] = (),
    detail: dict[str, Any] | None = None,
    reviewer_in_update: bool = True,
) -> None:
    actor = _actor(reviewer)
    jid = _job_id(job_id)
    params = ((reviewer.strip(),) if reviewer_in_update else ()) + extra_params + (jid,)
    with connect() as conn:
        result = conn.execute(sql, params)
        if result.rowcount != 1:
            raise ConcurrentActionError("This job was already changed by another reviewer or worker.")
        conn.execute(
            "INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, %s, %s, %s)",
            (jid, event, actor, Jsonb(detail or {})),
        )
