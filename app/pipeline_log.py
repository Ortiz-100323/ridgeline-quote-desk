"""End-to-end logging to the pipeline_logs table (and the console).

Each process run gets a run_id. Every log row carries it, plus the job_id when the
step concerns one RFQ, so one RFQ can be followed from dispatch to review.

The logger uses its own autocommit connection, so log rows are kept even when the
caller's transaction rolls back. If the database is unreachable, rows go to the
console only: logging never breaks the pipeline.
"""
import logging
import sys
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from app.db import connect

_console = logging.getLogger("ridgeline")
if not _console.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    _console.addHandler(_handler)
    _console.setLevel(logging.INFO)


class PipelineLogger:
    def __init__(self, component: str, actor: str | None = None, run_id: uuid.UUID | None = None):
        self.component = component
        self.actor = actor or component
        self.run_id = run_id or uuid.uuid4()
        try:
            self._conn = connect(autocommit=True)
        except Exception as exc:  # noqa: BLE001 - logging must not stop the pipeline
            self._conn = None
            _console.warning("pipeline_logs unavailable, console only: %s", exc)

    def log(self, level: str, step: str, message: str, *, job_id: uuid.UUID | None = None,
            duration_ms: int | None = None, **detail: Any) -> None:
        job = f" job={job_id}" if job_id else ""
        _console.log(getattr(logging, level), "[%s] %s: %s%s", self.component, step, message, job)
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """INSERT INTO pipeline_logs (run_id, job_id, component, actor, level, step, message,
                                              duration_ms, detail)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (self.run_id, job_id, self.component, self.actor, level, step, message, duration_ms,
                 Jsonb(detail)),
            )
        except Exception as exc:  # noqa: BLE001
            _console.warning("could not write pipeline_logs row: %s", exc)

    def info(self, step: str, message: str, **kw: Any) -> None:
        self.log("INFO", step, message, **kw)

    def warning(self, step: str, message: str, **kw: Any) -> None:
        self.log("WARNING", step, message, **kw)

    def error(self, step: str, message: str, **kw: Any) -> None:
        self.log("ERROR", step, message, **kw)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def __enter__(self) -> "PipelineLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
