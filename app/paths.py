"""Project paths, plus a guard so the pipeline never reads the answer key."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RFQ_DIR = ROOT / "rfqs"
OUTPUT_DIR = ROOT / "outputs"
WORK_DIR = ROOT / "work"            # worker scratch files (git-ignored)
TEXT_DIR = WORK_DIR / "text"        # text extracted from PDF/XLSX RFQs

PRODUCTS_CSV = DATA_DIR / "products.csv"
CUSTOMERS_CSV = DATA_DIR / "customers.csv"
CROSS_REFERENCE_CSV = DATA_DIR / "cross_reference.csv"

# Only the evaluation script may read this folder. The pipeline must not.
_ANSWER_KEY_DIR = ROOT / "answer_key"


def safe_path(path: Path) -> Path:
    """Resolve a path and refuse it if it points inside answer_key/."""
    resolved = Path(path).resolve()
    if resolved == _ANSWER_KEY_DIR or _ANSWER_KEY_DIR in resolved.parents:
        raise PermissionError(f"The pipeline may not read the answer key: {resolved}")
    return resolved
