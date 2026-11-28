"""python -m app.dispatcher [--folder rfqs]"""
import argparse
from pathlib import Path

from app.db import apply_schema, connect
from app.dispatcher.dispatcher import dispatch
from app.paths import RFQ_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue one job per RFQ file.")
    parser.add_argument("--folder", type=Path, default=RFQ_DIR, help="folder to scan (default: rfqs/)")
    args = parser.parse_args()
    with connect() as conn:
        apply_schema(conn)  # idempotent: keeps the tables current
    dispatch(args.folder)


if __name__ == "__main__":
    main()
