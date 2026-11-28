"""Postgres queries and guarded mutations used by the Streamlit portal.

The UI deliberately has no filesystem access. All reviewer actions are committed
with their audit event in the same transaction.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.catalog import normalize_part_number
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
                      count(*) FILTER (WHERE job_status = 'succeeded') AS succeeded,
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


def get_job_timeline(job_id: str | UUID) -> list[dict[str, Any]]:
    """Read operational steps and audit events for a single job in time order."""
    with connect(row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT created_at, source, step, component, actor, level,
                      message, duration_ms, run_id, detail
               FROM (
                   SELECT created_at, 'Pipeline log' AS source, step, component, actor,
                          level, message, duration_ms, run_id, detail, log_id AS entry_id
                   FROM pipeline_logs WHERE job_id = %(job_id)s
                   UNION ALL
                   SELECT created_at, 'Audit event', event, NULL, actor,
                          NULL, NULL, NULL, NULL, detail, event_id
                   FROM job_events WHERE job_id = %(job_id)s
               ) entries
               ORDER BY created_at, source, entry_id""",
            {"job_id": _job_id(job_id)},
        ).fetchall()


def search_products(query: str, limit: int = 8, *, include_discontinued: bool = False) -> list[dict[str, Any]]:
    """Find catalog candidates by part number, category, or description."""
    query = query.strip()
    if not query:
        return []
    with connect(row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT part_number, description, category, unit, list_price, status,
                      primary_warehouse, portal_revision,
                      greatest(similarity(description, %(query)s),
                               similarity(category, %(query)s),
                               similarity(part_number, %(query)s)) AS score
               FROM products
               WHERE (%(include_discontinued)s OR status = 'active')
                 AND (part_number_norm = normalize_part_number(%(query)s)
                      OR part_number ILIKE %(term)s OR description ILIKE %(term)s
                      OR category ILIKE %(term)s
                      OR to_tsvector('english', description || ' ' || category)
                         @@ plainto_tsquery('english', %(query)s)
                      OR description %% %(query)s OR part_number %% %(query)s)
               ORDER BY (normalize_part_number(part_number) = normalize_part_number(%(query)s)) DESC,
                        score DESC, part_number
               LIMIT %(limit)s""",
            {"query": query, "term": f"%{query}%", "limit": limit,
             "include_discontinued": include_discontinued},
        ).fetchall()


def similar_products(part_number: str, limit: int = 8) -> list[dict[str, Any]]:
    """Rank active alternatives by description similarity to the selected part."""
    with connect(row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT p.part_number, p.description, p.category, p.unit, p.list_price,
                      similarity(p.description, chosen.description) AS score
               FROM products chosen
               JOIN products p ON p.part_number <> chosen.part_number
                              AND p.description %% chosen.description
               WHERE chosen.part_number = %s AND p.status = 'active'
               ORDER BY score DESC, (p.category = chosen.category) DESC,
                        (p.unit = chosen.unit) DESC, p.part_number
               LIMIT %s""",
            (part_number, limit),
        ).fetchall()


def get_product(part_number: str) -> dict[str, Any] | None:
    with connect(row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT part_number, description, category, unit, primary_warehouse,
                      list_price, status, portal_revision
               FROM products WHERE part_number = %s""",
            (part_number,),
        ).fetchone()


def save_product(
    reviewer: str, part_number: str, details: dict[str, str],
    *, list_price: str = "", revision: int | None = None,
    quote_line: tuple[UUID, int] | None = None,
) -> None:
    actor = _actor(reviewer)
    fields = ("description", "category", "unit", "primary_warehouse")
    values = tuple(details[field].strip() for field in fields)
    if not all(values):
        raise ValueError("Enter a description, category, unit, and warehouse.")
    if revision is None:
        part_number = part_number.strip().upper()
        if not normalize_part_number(part_number):
            raise ValueError("Enter a part number.")
        try:
            price = Decimal(list_price.strip())
            if not price.is_finite() or not 0 <= price <= Decimal("9999999999.99"):
                raise ValueError("Enter a nonnegative list price up to 9999999999.99.")
            if price != price.quantize(Decimal("0.01")):
                raise ValueError("Enter a list price with at most two decimal places.")
        except InvalidOperation:
            raise ValueError("Enter a valid list price.") from None

    with connect() as conn:
        if quote_line:
            _lock_draft(conn, quote_line[0])
        if revision is None:
            try:
                conn.execute(
                    """INSERT INTO products (part_number, description, category, unit,
                                             primary_warehouse, list_price, status, portal_revision)
                       VALUES (%s, %s, %s, %s, %s, %s, 'active', 1)""",
                    (part_number, *values, price),
                )
            except UniqueViolation:
                raise ValueError("That part number already exists in the catalog.") from None
            step = "product_created"
        else:
            updated = conn.execute(
                """UPDATE products SET description = %s, category = %s, unit = %s,
                          primary_warehouse = %s, portal_revision = portal_revision + 1
                   WHERE part_number = %s AND portal_revision = %s""",
                (*values, part_number, revision),
            )
            if updated.rowcount != 1:
                raise ConcurrentActionError("The product changed. Reload it before saving again.")
            step = "product_updated"
        if quote_line and revision is None:
            _set_corrected_part(conn, *quote_line, part_number, actor)
        conn.execute(
            """INSERT INTO pipeline_logs (run_id, job_id, component, actor, level, step, message, detail)
               VALUES (%s, %s, 'portal', %s, 'INFO', %s, %s, %s)""",
            (uuid4(), quote_line[0] if quote_line else None, actor, step,
             step.replace("_", " ").capitalize(),
             Jsonb({"part_number": part_number, **dict(zip(fields, values))})),
        )


def _lock_draft(conn, job_id: UUID) -> None:
    job = conn.execute(
        """SELECT job_id FROM jobs
           WHERE job_id = %s AND job_status = 'succeeded' AND review_status = 'pending_review'
           FOR UPDATE""",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ConcurrentActionError("The quote is no longer awaiting review.")


def _set_corrected_part(conn, job_id: UUID, line: int, part_number: str, actor: str) -> None:
    previous = conn.execute(
        """SELECT coalesce(corrected_part_number, ridgeline_part_number)
           FROM quote_lines WHERE job_id = %s AND line = %s""",
        (job_id, line),
    ).fetchone()
    updated = conn.execute(
        """UPDATE quote_lines l SET corrected_part_number = p.part_number
           FROM products p WHERE l.job_id = %s AND l.line = %s
             AND p.part_number = %s AND p.status = 'active'""",
        (job_id, line, part_number),
    )
    if updated.rowcount != 1:
        raise ConcurrentActionError("The quote line or active catalog product is unavailable.")
    detail = {"line": line, "from": previous[0], "to": part_number}
    conn.execute(
        "INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, 'part_corrected', %s, %s)",
        (job_id, actor, Jsonb(detail)),
    )
    _log_portal(conn, job_id, actor, "part_corrected",
                f"Line {line}: {previous[0] or 'no part'} -> {part_number}", detail)


def _log_portal(conn, job_id: UUID, actor: str, step: str, message: str, detail: dict[str, Any]) -> None:
    """pipeline_logs row for a review action, in the caller's transaction (CONTRACT §3.4)."""
    conn.execute(
        """INSERT INTO pipeline_logs (run_id, job_id, component, actor, level, step, message, detail)
           VALUES (%s, %s, 'portal', %s, 'INFO', %s, %s, %s)""",
        (uuid4(), job_id, actor, step, message, Jsonb(detail)),
    )


def correct_part(job_id: str | UUID, line: int, part_number: str, reviewer: str) -> None:
    actor = _actor(reviewer)
    jid = _job_id(job_id)
    with connect() as conn:
        _lock_draft(conn, jid)
        _set_corrected_part(conn, jid, line, part_number, actor)


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
        _log_portal(conn, jid, actor, event, f"Job {event} by {actor}", detail or {})
