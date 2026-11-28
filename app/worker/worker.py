"""Worker flow: claim a job, transform, extract with the LLM, match, write the quote, update the job.

    recover expired leases -> claim (SKIP LOCKED) -> heartbeat thread keeps the lease
    -> transform (step 1) -> extract (step 2) -> match each line (step 3) + identify customer
    -> one transaction: quotes + quote_lines + outputs/<rfq_id>.json + job succeeded
    -> or on error: retry later (pending + backoff) or escalate (failed + pending_review)

Error policy (docs/CONTRACT.md §3.3):
- retryable   ExtractionError(retryable=True): timeouts/API down after the in-call retries,
              or no usable answer even from the fallback model. Retried until max_attempts.
- permanent   unsupported file type, unreadable/corrupt file, missing file, non-retryable
              API error (e.g. 400/401). Escalated at once: retrying cannot help.
- unexpected  any other exception (a bug): retried like a retryable error, so a transient
              DB hiccup doesn't kill the job, and escalated after max_attempts.

Run:  python -m app.worker [--once] [--max-jobs N] [--worker-id ID]
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from rapidfuzz import fuzz, process

from app.catalog import load_catalog_from_db
from app.db import connect
from app.paths import ROOT
from app.pipeline_log import PipelineLogger, _console, safe_error
from app.worker.extractor import ExtractionError, ExtractionResult, Extractor
from app.worker.matcher import Matcher
from app.worker.transformer import TransformError, transform

CUSTOMER_MATCH_MIN_SCORE = 88   # rapidfuzz WRatio, 0-100: "Mesa Fab" vs "Mesa Fabrication" passes


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class WorkerSettings:
    poll_seconds: float          # WORKER_POLL_SECONDS: wait when the queue is empty
    lease_seconds: int           # WORKER_LEASE_SECONDS: a job is reclaimed this long after the last heartbeat
    retry_backoff_seconds: float  # WORKER_RETRY_BACKOFF_SECONDS: job retry delay = base * 2^(attempts-1)
    count: int = 1               # WORKER_COUNT: worker processes started by python -m app.worker

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        count = int(_env_float("WORKER_COUNT", 1))
        if count < 1:
            raise RuntimeError("WORKER_COUNT must be 1 or more")
        return cls(poll_seconds=_env_float("WORKER_POLL_SECONDS", 5.0),
                   lease_seconds=int(_env_float("WORKER_LEASE_SECONDS", 600)),
                   retry_backoff_seconds=_env_float("WORKER_RETRY_BACKOFF_SECONDS", 60.0),
                   count=count)


class PermanentJobError(Exception):
    """The job can never succeed as is: escalate to a person without retrying."""


class LeaseLost(Exception):
    """Another worker (or lease recovery) took the job: drop our result."""


# --------------------------------------------------------------------------- heartbeat

class Heartbeat:
    """Extends the job's lease every lease/3 seconds on its own connection."""

    def __init__(self, job_id: uuid.UUID, worker_id: str, lease_seconds: int,
                 log: PipelineLogger | None = None):
        self.job_id, self.worker_id, self.lease_seconds = job_id, worker_id, lease_seconds
        self.log = log
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{job_id}", daemon=True)

    def _run(self) -> None:
        try:
            with connect(autocommit=True) as conn:
                while not self._stop.wait(max(self.lease_seconds / 3, 1)):
                    n = conn.execute(
                        """UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s)
                           WHERE job_id = %s AND locked_by = %s AND job_status = 'running'""",
                        (self.lease_seconds, self.job_id, self.worker_id)).rowcount
                    if n == 0:
                        self.lost = True
                        if self.log:
                            self.log.warning("lease_lost", "Heartbeat found the job no longer ours",
                                             job_id=self.job_id)
                        return
        except Exception as exc:  # noqa: BLE001 - a missed heartbeat only risks a reclaim; the final UPDATE is guarded
            if self.log:  # leave a trace: it explains a later lease_expired
                self.log.warning("heartbeat_failed", f"Lease no longer extended: {safe_error(exc)}",
                                 job_id=self.job_id)

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


# --------------------------------------------------------------------------- worker

class Worker:
    def __init__(self, worker_id: str | None = None, settings: WorkerSettings | None = None,
                 extractor: Extractor | None = None):
        self.worker_id = f"worker:{worker_id or f'{socket.gethostname()}-{os.getpid()}'}"
        self.settings = settings or WorkerSettings.from_env()
        self.log = PipelineLogger("worker", actor=self.worker_id)
        self.extractor = extractor or Extractor(log=self.log)
        self._stopping = False

    # ---- queue operations ------------------------------------------------

    def recover_leases(self) -> int:
        """Running jobs whose lease expired go back to pending, or are escalated (CONTRACT §3.3)."""
        with connect() as conn, conn.transaction():
            rows = conn.execute(
                """UPDATE jobs SET
                       job_status = CASE WHEN attempts < max_attempts THEN 'pending' ELSE 'failed' END,
                       review_status = CASE WHEN attempts < max_attempts THEN 'not_ready' ELSE 'pending_review' END,
                       finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
                       escalation_reason = CASE WHEN attempts < max_attempts THEN escalation_reason
                                           ELSE 'Worker lease expired on the last attempt' END,
                       last_error = 'lease expired (worker ' || coalesce(locked_by, '?') || ' stopped responding)',
                       run_after = now(), locked_by = NULL, locked_at = NULL, lease_expires_at = NULL
                   WHERE job_status = 'running' AND lease_expires_at < now()
                   RETURNING job_id, job_status, last_error""").fetchall()
            for job_id, status, error in rows:
                conn.execute("INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, 'lease_expired', %s, %s)",
                             (job_id, self.worker_id, Jsonb({"new_status": status})))
                self.log.warning("lease_expired", f"{error}; job now {status}", job_id=job_id)
        return len(rows)

    def claim(self) -> dict[str, Any] | None:
        with connect(row_factory=dict_row) as conn, conn.transaction():
            job = conn.execute(
                """WITH next AS (
                       SELECT job_id FROM jobs
                       WHERE job_status = 'pending' AND run_after <= now()
                       ORDER BY priority DESC, created_at
                       FOR UPDATE SKIP LOCKED
                       LIMIT 1)
                   UPDATE jobs j SET job_status = 'running', attempts = attempts + 1,
                          locked_by = %(worker)s, locked_at = now(),
                          lease_expires_at = now() + make_interval(secs => %(lease)s),
                          started_at = coalesce(started_at, now()), last_error = NULL
                   FROM next WHERE j.job_id = next.job_id
                   RETURNING j.*""",
                {"worker": self.worker_id, "lease": self.settings.lease_seconds}).fetchone()
            if job:
                conn.execute("INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, 'claimed', %s, %s)",
                             (job["job_id"], self.worker_id, Jsonb({"attempt": job["attempts"]})))
        if job:
            self.log.info("claimed", f"{job['file_name']} (attempt {job['attempts']}/{job['max_attempts']})",
                          job_id=job["job_id"], rfq_id=job["rfq_id"], file_type=job["file_type"],
                          attempt=job["attempts"])
        return job

    # ---- one job -----------------------------------------------------------

    def process(self, job: dict[str, Any]) -> str:
        """Run the whole pipeline for a claimed job. Returns the final job_status."""
        job_id = job["job_id"]
        started = time.monotonic()
        try:
            with Heartbeat(job_id, self.worker_id, self.settings.lease_seconds, self.log) as hb:
                output, extraction = self._run_pipeline(job)
                if hb.lost:
                    raise LeaseLost("lease lost during processing")
                self._succeed(job, output, extraction, started)
            return "succeeded"
        except LeaseLost as exc:
            self.log.error("lease_lost", f"{exc}: result discarded", job_id=job_id)
            return "lease_lost"
        except PermanentJobError as exc:
            return self._fail(job, str(exc), retryable=False)
        except ExtractionError as exc:
            return self._fail(job, str(exc), retryable=exc.retryable)
        except Exception as exc:  # noqa: BLE001 - unexpected: retry, escalate after max_attempts
            # full traceback on the console only; the DB gets text without RFQ content (CONTRACT §3.4)
            _console.exception("unexpected error in job %s", job_id)
            return self._fail(job, safe_error(exc), retryable=True)

    def _run_pipeline(self, job: dict[str, Any]) -> tuple[dict[str, Any], ExtractionResult]:
        job_id = job["job_id"]

        # step 1: transform
        try:
            tr = transform(job["source_path"], rfq_id=job["rfq_id"], job_id=str(job_id), log=self.log)
        except (TransformError, FileNotFoundError, PermissionError) as exc:
            raise PermanentJobError(f"Cannot read {job['file_name']}: {exc}") from exc
        if tr.route == "unsupported":
            raise PermanentJobError(f"Unsupported file {job['file_name']}: {tr.reason}")

        # step 2: LLM extraction (retries + fallback model inside)
        result = self.extractor.extract(tr, job_id=job_id)
        ex = result.extraction

        # step 3: match lines against the current catalog (portal edits included) + customer
        with connect() as conn:
            catalog = load_catalog_from_db(conn)
            customers = conn.execute("SELECT customer_id, name FROM customers").fetchall()
        matcher = Matcher(catalog)
        customer_id, customer_name = self._identify_customer(ex.customer_name, customers, job_id)

        lines = []
        for n, line in enumerate(ex.lines, start=1):
            m = matcher.match_line(line, from_image=result.from_image, log=self.log, job_id=str(job_id))
            lines.append({"line": n,                      # renumbered: 1..n with no gaps (CONTRACT §4)
                          "as_written": line.as_written.strip() or (line.description or "?"),
                          "quantity": line.quantity, "unit": line.unit, **m.to_line_fields()})

        output = {"rfq_id": job["rfq_id"], "customer_id": customer_id,
                  "customer_name": customer_name, "lines": lines,
                  "non_item_rows": list(ex.non_item_rows)}
        return output, result

    def _identify_customer(self, name: str | None, customers: list[tuple[str, str]],
                           job_id: Any) -> tuple[str | None, str | None]:
        if not name:
            return None, None
        best = process.extractOne(name, {cid: cname for cid, cname in customers},
                                  scorer=fuzz.WRatio, processor=str.lower)
        if best and best[1] >= CUSTOMER_MATCH_MIN_SCORE:
            cname, score, cid = best
            self.log.info("customer_matched", f"{cid} ({score:.0f})", job_id=job_id, customer_id=cid,
                          score=round(score, 1))
            return cid, cname
        self.log.warning("customer_unknown", "No customer above the match threshold", job_id=job_id,
                         best_customer_id=best[2] if best else None,
                         best_score=round(best[1], 1) if best else None)
        return None, name

    # ---- outcomes ------------------------------------------------------------

    def _succeed(self, job: dict[str, Any], output: dict[str, Any], result: ExtractionResult,
                 started: float) -> None:
        job_id = job["job_id"]
        output_rel = f"outputs/{job['rfq_id']}.json"
        review_lines = [ln["line"] for ln in output["lines"] if ln["needs_review"]]
        with connect() as conn, conn.transaction():
            # Guard first: lock our row, still ours?
            if conn.execute("SELECT 1 FROM jobs WHERE job_id = %s AND locked_by = %s AND job_status = 'running' "
                            "FOR UPDATE", (job_id, self.worker_id)).fetchone() is None:
                raise LeaseLost("job is no longer locked by this worker")
            conn.execute("DELETE FROM quotes WHERE job_id = %s", (job_id,))  # a requeued job's old draft
            conn.execute(
                """INSERT INTO quotes (job_id, rfq_id, customer_id, customer_name, non_item_rows, output_json)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (job_id, job["rfq_id"], output["customer_id"], output["customer_name"],
                 output["non_item_rows"], Jsonb(output)))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO quote_lines (job_id, line, as_written, quantity, unit, ridgeline_part_number,
                                                match_method, confidence, needs_review, flags, note)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(job_id, ln["line"], ln["as_written"], ln["quantity"], ln["unit"],
                      ln["ridgeline_part_number"], ln["match_method"], ln["confidence"],
                      ln["needs_review"], ln["flags"], ln["note"]) for ln in output["lines"]])
            _write_json_atomic(ROOT / output_rel, output)
            conn.execute(
                """UPDATE jobs SET job_status = 'succeeded', review_status = 'pending_review', finished_at = now(),
                          output_path = %s, model_used = %s, fallback_used = %s, last_error = NULL,
                          escalation_reason = NULL, locked_by = NULL, locked_at = NULL, lease_expires_at = NULL
                   WHERE job_id = %s AND locked_by = %s""",
                (output_rel, result.model_used, result.fallback_used, job_id, self.worker_id))
            detail = {"lines": len(output["lines"]), "lines_needing_review": review_lines,
                      "customer_id": output["customer_id"], "model_used": result.model_used,
                      "fallback_used": result.fallback_used, "fallback_reason": result.fallback_reason,
                      "document_confidence": result.document_confidence,
                      "input_tokens": result.input_tokens, "output_tokens": result.output_tokens}
            conn.execute("INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, 'succeeded', %s, %s)",
                         (job_id, self.worker_id, Jsonb(detail)))
        self.log.info("quote_written", f"{output_rel}: {len(output['lines'])} line(s), "
                      f"{len(review_lines)} need review", job_id=job_id,
                      duration_ms=int((time.monotonic() - started) * 1000), output_path=output_rel, **detail)

    def _fail(self, job: dict[str, Any], error: str, *, retryable: bool) -> str:
        job_id, attempts, max_attempts = job["job_id"], job["attempts"], job["max_attempts"]
        error = error[:2000]
        retry = retryable and attempts < max_attempts
        with connect() as conn, conn.transaction():
            if retry:
                delay = self.settings.retry_backoff_seconds * (2 ** (attempts - 1))
                n = conn.execute(
                    """UPDATE jobs SET job_status = 'pending', run_after = now() + make_interval(secs => %s),
                              last_error = %s, locked_by = NULL, locked_at = NULL, lease_expires_at = NULL
                       WHERE job_id = %s AND locked_by = %s""",
                    (delay, error, job_id, self.worker_id)).rowcount
                event, status = "retry_scheduled", "pending"
                detail = {"attempt": attempts, "max_attempts": max_attempts, "retry_in_s": delay, "error": error[:300]}
            else:
                reason = (f"Gave up after {attempts} attempt(s): {error}" if retryable
                          else f"Cannot be processed automatically: {error}")
                n = conn.execute(
                    """UPDATE jobs SET job_status = 'failed', review_status = 'pending_review', finished_at = now(),
                              last_error = %s, escalation_reason = %s,
                              locked_by = NULL, locked_at = NULL, lease_expires_at = NULL
                       WHERE job_id = %s AND locked_by = %s""",
                    (error, reason[:2000], job_id, self.worker_id)).rowcount
                event, status = "escalated", "failed"
                detail = {"attempt": attempts, "retryable": retryable, "reason": reason[:300]}
            if n == 0:
                self.log.error("lease_lost", f"Could not record {event}: job no longer ours", job_id=job_id)
                return "lease_lost"
            conn.execute("INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, %s, %s, %s)",
                         (job_id, event, self.worker_id, Jsonb(detail)))
        if retry:
            self.log.warning("retry_scheduled", f"attempt {attempts}/{max_attempts} failed, retry in "
                             f"{detail['retry_in_s']:.0f}s: {error[:200]}", job_id=job_id, **detail)
        else:
            self.log.error("escalated", f"{detail['reason'][:300]}", job_id=job_id, **detail)
        return status

    def release(self, job: dict[str, Any], why: str) -> None:
        """Give a job back without spending an attempt (worker shutting down)."""
        with connect() as conn, conn.transaction():
            n = conn.execute(
                """UPDATE jobs SET job_status = 'pending', attempts = greatest(attempts - 1, 0), run_after = now(),
                          locked_by = NULL, locked_at = NULL, lease_expires_at = NULL, last_error = %s
                   WHERE job_id = %s AND locked_by = %s AND job_status = 'running'""",
                (why, job["job_id"], self.worker_id)).rowcount
            if n:
                conn.execute("INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, 'requeued', %s, %s)",
                             (job["job_id"], self.worker_id, Jsonb({"reason": why})))
        if n:
            self.log.warning("released", why, job_id=job["job_id"])
        else:
            self.log.warning("lease_lost", f"Not released ({why}): job was no longer ours", job_id=job["job_id"])

    # ---- loop ----------------------------------------------------------------

    def run(self, *, once: bool = False, max_jobs: int | None = None) -> dict[str, int]:
        """Process jobs until the queue is empty (once=True), max_jobs is reached, or Ctrl+C."""
        counts: dict[str, int] = {}
        self.log.info("worker_started", f"{self.worker_id} started", once=once, max_jobs=max_jobs,
                      model=self.extractor.settings.model, fallback_model=self.extractor.settings.fallback_model)
        job = None
        try:
            while max_jobs is None or sum(counts.values()) < max_jobs:
                self.recover_leases()
                job = self.claim()
                if job is None:
                    if once:
                        break
                    time.sleep(self.settings.poll_seconds)
                    continue
                status = self.process(job)
                counts[status] = counts.get(status, 0) + 1
                job = None
        except KeyboardInterrupt:
            if job is not None:
                self.release(job, "worker stopped (Ctrl+C) during processing")
        except Exception as exc:  # DB down, bug in the loop: log an ERROR, give the job back, re-raise
            self.log.error("worker_crashed", f"{self.worker_id} crashed: {safe_error(exc)}",
                           job_id=job["job_id"] if job else None, **counts)
            if job is not None:
                try:
                    self.release(job, f"worker crashed: {exc.__class__.__name__}")
                except Exception:  # noqa: BLE001 - DB unreachable: the lease will expire and be recovered
                    pass
            raise
        finally:
            self.log.link_run_rows()
            self.log.info("worker_stopped", f"{self.worker_id} stopped: {counts or 'no jobs'}", **counts)
            self.log.close()
        return counts



def run_worker(worker_id: str, once: bool = False, max_jobs: int | None = None) -> dict[str, int]:
    """Entry point of one worker process. It lives in an importable module (not __main__)
    so ProcessPoolExecutor can start it with spawn on Windows."""
    return Worker(worker_id).run(once=once, max_jobs=max_jobs)

def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)

