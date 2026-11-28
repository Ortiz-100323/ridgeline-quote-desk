"""Dispatcher: scan the RFQ folder and queue one job per file.

Testing mode: every run queues every file again, with a new job_id. The content
hash is stored but is not a key yet (see TODO(dedupe) in docs/CONTRACT.md).
"""
import hashlib
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

from psycopg.types.json import Jsonb

from app.db import connect
from app.paths import ROOT, RFQ_DIR, safe_path
from app.pipeline_log import PipelineLogger

FILE_TYPES = {
    ".txt": "text", ".eml": "text", ".md": "text", ".msg": "text",
    ".pdf": "pdf",
    ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xlsx",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".tif": "image", ".tiff": "image", ".bmp": "image", ".heic": "image",
}


@dataclass(frozen=True)
class RfqFile:
    rfq_id: str
    source_path: str      # POSIX, relative to the repo root
    file_name: str
    file_type: str
    file_extension: str
    mime_type: str | None
    file_size_bytes: int
    source_sha256: str


def classify(path: Path) -> str:
    return FILE_TYPES.get(path.suffix.lower(), "other")


def describe(path: Path) -> RfqFile:
    path = safe_path(path)
    data = path.read_bytes()
    try:
        source_path = path.relative_to(ROOT).as_posix()
    except ValueError:  # folder outside the repo: keep the absolute path
        source_path = path.as_posix()
    return RfqFile(
        rfq_id=path.stem,
        source_path=source_path,
        file_name=path.name,
        file_type=classify(path),
        file_extension=path.suffix.lower(),
        mime_type=mimetypes.guess_type(path.name)[0],
        file_size_bytes=len(data),
        source_sha256=hashlib.sha256(data).hexdigest(),
    )


def find_rfq_files(folder: Path) -> list[Path]:
    """Files directly in the folder, sorted by name. Hidden files are skipped."""
    folder = safe_path(folder)
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))


def dispatch(folder: Path = RFQ_DIR) -> list[str]:
    """Queue every file in the folder. Returns the new job ids."""
    started = time.monotonic()
    job_ids: list[str] = []
    with PipelineLogger("dispatcher") as log:
        log.info("run_started", f"Scanning {folder}", folder=str(folder))
        try:
            files = find_rfq_files(folder)
        except (OSError, PermissionError) as exc:
            log.error("run_failed", f"Cannot read folder: {exc}", folder=str(folder))
            raise
        log.info("files_found", f"{len(files)} file(s) found", files=[p.name for p in files])

        with connect() as conn:
            for path in files:
                try:
                    rfq = describe(path)
                    with conn.transaction():
                        job_id = conn.execute(
                            """INSERT INTO jobs (rfq_id, source_path, file_name, file_type, file_extension,
                                                 mime_type, file_size_bytes, source_sha256, dispatch_run_id)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING job_id""",
                            (rfq.rfq_id, rfq.source_path, rfq.file_name, rfq.file_type, rfq.file_extension,
                             rfq.mime_type, rfq.file_size_bytes, rfq.source_sha256, log.run_id),
                        ).fetchone()[0]
                        conn.execute(
                            "INSERT INTO job_events (job_id, event, actor, detail) VALUES (%s, 'created', 'dispatcher', %s)",
                            (job_id, Jsonb({"run_id": str(log.run_id), "source_path": rfq.source_path})),
                        )
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the others
                    log.error("job_failed", f"Could not queue {path.name}: {exc}", file_name=path.name)
                    continue
                job_ids.append(str(job_id))
                log.info("job_created", f"Queued {rfq.file_name} as {rfq.file_type}", job_id=job_id,
                         rfq_id=rfq.rfq_id, source_path=rfq.source_path, file_type=rfq.file_type,
                         file_size_bytes=rfq.file_size_bytes, source_sha256=rfq.source_sha256)
                if rfq.file_type == "other":
                    log.warning("unknown_file_type", f"{rfq.file_name}: no extractor for this type",
                                job_id=job_id, file_extension=rfq.file_extension)

        log.info("run_finished", f"{len(job_ids)} job(s) queued, {len(files) - len(job_ids)} failed",
                 duration_ms=int((time.monotonic() - started) * 1000),
                 queued=len(job_ids), failed=len(files) - len(job_ids))
    return job_ids
