"""python -m app.worker [--workers N] [--once] [--max-jobs N] [--worker-id ID]

Starts WORKER_COUNT worker processes (env, default 1; --workers overrides it). They share
the queue safely: the claim uses SKIP LOCKED, so each job goes to exactly one worker.
"""
import argparse
import os
import socket
from concurrent.futures import ProcessPoolExecutor

from app.db import apply_schema, connect
from app.worker.worker import WorkerSettings, run_worker


def main() -> int:
    parser = argparse.ArgumentParser(description="Consume RFQ jobs: transform, extract, match, write quotes.")
    parser.add_argument("--workers", type=int, default=None, help="worker processes (default: WORKER_COUNT, or 1)")
    parser.add_argument("--once", action="store_true", help="stop when the queue is empty instead of polling")
    parser.add_argument("--max-jobs", type=int, default=None, help="stop each worker after this many jobs")
    parser.add_argument("--worker-id", default=None, help="id prefix (default: <hostname>-<pid>)")
    args = parser.parse_args()

    count = args.workers if args.workers is not None else WorkerSettings.from_env().count
    if count < 1:
        parser.error("--workers must be 1 or more")
    with connect() as conn:
        apply_schema(conn)  # idempotent: keeps the tables current

    base = args.worker_id or f"{socket.gethostname()}-{os.getpid()}"
    if count == 1:
        print(run_worker(base, args.once, args.max_jobs))
        return 0

    ids = [f"{base}-{n}" for n in range(1, count + 1)]
    totals: dict[str, int] = {}
    with ProcessPoolExecutor(max_workers=count) as pool:
        futures = {wid: pool.submit(run_worker, wid, args.once, args.max_jobs) for wid in ids}
        for wid, fut in futures.items():
            counts = fut.result()
            print(f"worker:{wid} {counts}")
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
    print(f"total {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
