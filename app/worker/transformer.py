"""Worker step 1: turn an RFQ file into plain text, or route it straight to the LLM.

Routes (TransformResult.route):

- ``text``        PDF with a text layer, or XLSX: converted to a UTF-8 .txt in work/text/.
                  Plain-text RFQs (.txt, .eml, ...) are already text: passed through as-is.
- ``llm_direct``  Images, and PDFs with too few characters (scanned documents). Not
                  transformed: the original file goes to the LLM (vision).
- ``unsupported`` A type with no extractor (``other``, legacy .xls). Needs a person.

Run on the RFQ folder without the database:  python -m app.worker.transformer [files...]
"""
from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from app.paths import ROOT, TEXT_DIR, safe_path

if TYPE_CHECKING:
    from app.pipeline_log import PipelineLogger

Route = Literal["text", "llm_direct", "unsupported"]

# A PDF page with fewer non-whitespace characters than this, on average, is treated
# as scanned. Real RFQ pages have hundreds; a scanned page has none or a stray header.
MIN_CHARS_PER_PAGE = int(os.getenv("TRANSFORMER_MIN_CHARS_PER_PAGE", "50"))

TEXT_EXTENSIONS = {".txt", ".eml", ".md"}
PDF_EXTENSIONS = {".pdf"}
XLSX_EXTENSIONS = {".xlsx", ".xlsm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic"}


class TransformError(Exception):
    """The file is of a supported type but could not be read (corrupt, encrypted, ...)."""


@dataclass(frozen=True)
class TransformResult:
    route: Route
    source_path: Path                 # absolute path of the original file
    text_path: Path | None            # the .txt to send to the LLM (route "text" only)
    reason: str                       # why this route was chosen, for logs and the portal
    char_count: int = 0               # non-whitespace characters in the extracted text
    page_count: int | None = None     # PDF pages, or XLSX sheets
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str | None:
        return self.text_path.read_text(encoding="utf-8") if self.text_path else None


# --------------------------------------------------------------------------- helpers

def _count_chars(text: str) -> int:
    return sum(1 for c in text if not c.isspace())


def _cell(value: Any) -> str:
    """Render one table/cell value. Integral floats lose the '.0', dates become ISO."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    return " ".join(str(value).split())  # collapse newlines inside a cell


def _row(cells: list[str]) -> str:
    while cells and not cells[-1]:
        cells = cells[:-1]
    return " | ".join(cells)


def _write_text(text: str, rfq_id: str, job_id: str | None, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{rfq_id}_{str(job_id)[:8]}.txt" if job_id else f"{rfq_id}.txt"
    path = out_dir / name
    path.write_text(text.rstrip() + "\n", encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------- PDF

def _pdf_page_text(page: Any) -> tuple[str, int]:
    """Page text in reading order, with each detected table rendered as ' | ' rows.

    The page is cut into horizontal bands around the tables, so the header text,
    the table and the footer text stay in their original order and the table
    columns stay separated (plain extract_text() merges them with spaces).
    """
    tables = sorted(page.find_tables(), key=lambda t: t.bbox[1])
    parts: list[str] = []
    top = page.bbox[1]
    for table in tables:
        x0, t_top, x1, t_bottom = table.bbox
        if t_top > top:
            band = page.crop((page.bbox[0], top, page.bbox[2], t_top)).extract_text() or ""
            parts.append(band.strip())
        rows = [_row([_cell(c) for c in row]) for row in table.extract()]
        parts.append("\n".join(r for r in rows if r))
        top = max(top, t_bottom)
    if top < page.bbox[3]:
        band = page.crop((page.bbox[0], top, page.bbox[2], page.bbox[3])).extract_text() or ""
        parts.append(band.strip())
    return "\n\n".join(p for p in parts if p), len(tables)


def _pdf_to_text(path: Path) -> tuple[str, dict[str, Any]]:
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    pages_text: list[str] = []
    per_page_chars: list[int] = []
    image_pages = 0
    table_count = 0
    try:
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                text, n_tables = _pdf_page_text(page)
                table_count += n_tables
                per_page_chars.append(_count_chars(text))
                image_pages += bool(page.images)
                pages_text.append(f"=== Page {number} ===\n{text}")
    except PDFPasswordIncorrect as exc:
        raise TransformError(f"PDF is password protected: {path.name}") from exc
    except Exception as exc:  # noqa: BLE001 - pdfminer raises many unrelated types
        raise TransformError(f"Cannot read PDF {path.name}: {exc}") from exc
    return "\n\n".join(pages_text), {
        "per_page_chars": per_page_chars,
        "pages_with_images": image_pages,
        "tables": table_count,
    }


# --------------------------------------------------------------------------- XLSX

def _xlsx_to_text(path: Path) -> tuple[str, dict[str, Any]]:
    """Every sheet, every non-empty row, as 'R<row>: a | b | c'.

    Row numbers are kept so a line can be traced back to the spreadsheet. Formulas
    are read as their cached values; a formula never calculated by Excel reads empty.
    """
    import openpyxl

    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    except Exception as exc:  # noqa: BLE001
        raise TransformError(f"Cannot read workbook {path.name}: {exc}") from exc

    sheets_text: list[str] = []
    sheets: list[dict[str, Any]] = []
    try:
        for ws in wb.worksheets:
            state = "" if ws.sheet_state == "visible" else f" ({ws.sheet_state})"
            rows: list[str] = []
            for r_idx, values in enumerate(ws.iter_rows(values_only=True), start=1):
                line = _row([_cell(v) for v in values])
                if line.replace("|", "").strip():
                    rows.append(f"R{r_idx}: {line}")
            sheets.append({"name": ws.title, "state": ws.sheet_state, "rows": len(rows)})
            if rows:
                sheets_text.append(f"=== Sheet: {ws.title}{state} ===\n" + "\n".join(rows))
    finally:
        wb.close()
    return "\n\n".join(sheets_text), {"sheets": sheets}


# --------------------------------------------------------------------------- entry point

def transform(source: str | Path, *, rfq_id: str | None = None, job_id: str | None = None,
              out_dir: Path = TEXT_DIR, log: PipelineLogger | None = None) -> TransformResult:
    """Transform one RFQ file. ``source`` may be a jobs.source_path (repo-relative).

    Raises TransformError when a supported file cannot be read, and PermissionError
    for anything inside answer_key/.
    """
    started = time.monotonic()
    path = safe_path(ROOT / source)
    if not path.is_file():
        raise FileNotFoundError(path)
    rfq_id = rfq_id or path.stem
    ext = path.suffix.lower()

    if ext in TEXT_EXTENSIONS:
        text = path.read_text(encoding="utf-8", errors="replace")
        result = TransformResult("text", path, path, "plain text, passed through",
                                 char_count=_count_chars(text))
    elif ext in IMAGE_EXTENSIONS:
        result = TransformResult("llm_direct", path, None, "image: sent to the LLM as is")
    elif ext in PDF_EXTENSIONS:
        text, detail = _pdf_to_text(path)
        pages = len(detail["per_page_chars"])
        chars = sum(detail["per_page_chars"])
        detail["min_chars_per_page"] = MIN_CHARS_PER_PAGE
        if pages == 0 or chars / pages < MIN_CHARS_PER_PAGE:
            result = TransformResult(
                "llm_direct", path, None,
                f"scanned PDF: {chars} chars over {pages} page(s), "
                f"below {MIN_CHARS_PER_PAGE} per page",
                char_count=chars, page_count=pages, detail=detail)
        else:
            result = TransformResult("text", path, _write_text(text, rfq_id, job_id, out_dir),
                                     "PDF text layer extracted", char_count=chars,
                                     page_count=pages, detail=detail)
    elif ext in XLSX_EXTENSIONS:
        text, detail = _xlsx_to_text(path)
        chars = _count_chars(text)
        if chars == 0:
            raise TransformError(f"Workbook {path.name} has no data")
        result = TransformResult("text", path, _write_text(text, rfq_id, job_id, out_dir),
                                 "workbook converted to text", char_count=chars,
                                 page_count=len(detail["sheets"]), detail=detail)
    else:
        why = "legacy .xls is not supported, save it as .xlsx" if ext == ".xls" else f"no extractor for '{ext}'"
        result = TransformResult("unsupported", path, None, why)

    if log is not None:
        step = {"text": "transformed", "llm_direct": "routed_to_llm", "unsupported": "unsupported_file"}[result.route]
        level = "WARNING" if result.route == "unsupported" else "INFO"
        log.log(level, step, f"{path.name}: {result.reason}", job_id=job_id,
                duration_ms=int((time.monotonic() - started) * 1000), route=result.route,
                text_path=_rel(result.text_path), char_count=result.char_count,
                page_count=result.page_count, **result.detail)
    return result


def _rel(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    import argparse

    from app.paths import RFQ_DIR

    parser = argparse.ArgumentParser(description="Transform RFQ files to text (no database).")
    parser.add_argument("files", nargs="*", type=Path, help="default: every file in rfqs/")
    args = parser.parse_args(argv)
    files = args.files or sorted(p for p in safe_path(RFQ_DIR).iterdir()
                                 if p.is_file() and not p.name.startswith("."))
    failed = 0
    for f in files:
        try:
            r = transform(f)
        except (TransformError, OSError) as exc:
            failed += 1
            print(f"{Path(f).name:32} ERROR        {exc}")
            continue
        print(f"{r.source_path.name:32} {r.route:12} {_rel(r.text_path) or '-':40} {r.reason}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
