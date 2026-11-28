"""End-to-end logging to the pipeline_logs table (and the console).

Each process run gets a run_id. Every row is searchable by job:

- a row about one RFQ carries its ``job_id``;
- a run-level row (run_started, run_finished, worker_stopped, ...) has no single job, so it
  carries ``job_ids``: the jobs this logger has logged so far in the run.

``SELECT * FROM job_log WHERE job_id = ...`` (a view) returns both, plus job_events.

The logger uses its own autocommit connection, so log rows are kept even when the
caller's transaction rolls back. A dropped connection (DB restart) is reopened once per
write; if the database stays unreachable, rows go to the console only: logging never
breaks the pipeline. Safe to share between threads (the worker heartbeat logs too).
"""
import logging
import sys
import threading
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.db import connect

_console = logging.getLogger("ridgeline")
if not _console.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    _console.addHandler(_handler)
    _console.setLevel(logging.INFO)

MAX_RUN_JOB_IDS = 1000   # a long worker session lists its most recent jobs only

_INSERT = """INSERT INTO pipeline_logs (run_id, job_id, job_ids, component, actor, level, step, message,
                                        duration_ms, detail)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


class PipelineLogger:
    def __init__(self, component: str, actor: str | None = None, run_id: uuid.UUID | None = None):
        self.component = component
        self.actor = actor or component
        self.run_id = run_id or uuid.uuid4()
        self._jobs: dict[uuid.UUID, None] = {}      # ordered set of jobs seen in this run
        self._lock = threading.Lock()
        self._conn: psycopg.Connection | None = None
        self._open()

    @property
    def job_ids(self) -> list[uuid.UUID]:
        return list(self._jobs)

    def _open(self) -> bool:
        try:
            self._conn = connect(autocommit=True)
            return True
        except Exception as exc:  # noqa: BLE001 - logging must not stop the pipeline
            self._conn = None
            _console.warning("pipeline_logs unavailable, console only: %s", exc.__class__.__name__)
            return False

    def log(self, level: str, step: str, message: str, *, job_id: Any = None,
            job_ids: list[Any] | None = None, duration_ms: int | None = None, **detail: Any) -> None:
        job = _as_uuid(job_id)
        with self._lock:
            if job is not None:
                self._jobs.pop(job, None)
                self._jobs[job] = None
                while len(self._jobs) > MAX_RUN_JOB_IDS:
                    self._jobs.pop(next(iter(self._jobs)))
                related = [_as_uuid(j) for j in job_ids or []]
            else:  # run-level row: list the jobs of this run
                related = [_as_uuid(j) for j in job_ids] if job_ids is not None else list(self._jobs)
            suffix = f" job={job}" if job else (f" jobs={len(related)}" if related else "")
            _console.log(getattr(logging, level), "[%s] %s: %s%s", self.component, step, message, suffix)
            params = (self.run_id, job, related, self.component, self.actor, level, step, message,
                      duration_ms, Jsonb(detail))
            for attempt in (1, 2):
                if self._conn is None or self._conn.closed:
                    if attempt == 2 or not self._open():
                        return
                try:
                    self._conn.execute(_INSERT, params)
                    return
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    # connection dropped (DB restart, idle timeout): reopen once and retry
                    _console.warning("pipeline_logs connection lost (%s), reconnecting", exc.__class__.__name__)
                    self._close_quietly()
                except Exception as exc:  # noqa: BLE001 - a bad row must not break the pipeline
                    _console.warning("could not write pipeline_logs row %s: %s", step, exc.__class__.__name__)
                    return

    def link_run_rows(self) -> None:
        """Before the last row of a run: put the run's jobs on its earlier run-level rows
        (run_started, files_found, worker_started were written before any job existed)."""
        with self._lock:
            jobs = list(self._jobs)
            if not jobs or self._conn is None or self._conn.closed:
                return
            try:
                self._conn.execute(
                    "UPDATE pipeline_logs SET job_ids = %s WHERE run_id = %s AND job_id IS NULL",
                    (jobs, self.run_id))
            except Exception as exc:  # noqa: BLE001
                _console.warning("could not link run rows to jobs: %s", exc.__class__.__name__)

    def info(self, step: str, message: str, **kw: Any) -> None:
        self.log("INFO", step, message, **kw)

    def warning(self, step: str, message: str, **kw: Any) -> None:
        self.log("WARNING", step, message, **kw)

    def error(self, step: str, message: str, **kw: Any) -> None:
        self.log("ERROR", step, message, **kw)

    def _close_quietly(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close_quietly()

    def __enter__(self) -> "PipelineLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def safe_error(exc: BaseException, limit: int = 300) -> str:
    """An exception as text that is safe for logs and the portal: class name plus the first line
    of the message. Database errors keep only the primary message, never the DETAIL line, which
    can contain the failing row, that is, customer RFQ text (CONTRACT §3.4)."""
    name = exc.__class__.__name__
    diag = getattr(exc, "diag", None)
    if diag is not None and getattr(diag, "message_primary", None):
        text = diag.message_primary
        if getattr(diag, "constraint_name", None):
            text += f" (constraint {diag.constraint_name})"
        if getattr(exc, "sqlstate", None):
            text += f" [sqlstate {exc.sqlstate}]"
    else:
        text = (str(exc).strip().splitlines() or [""])[0]
    return f"{name}: {text}"[:limit]
