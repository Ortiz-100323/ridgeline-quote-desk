# Ridgeline Quote Desk

Ridgeline Quote Desk turns incoming RFQs into draft quotes backed by a human
review queue. PostgreSQL is the shared contract between the dispatcher, workers,
and the Streamlit review portal.

## How to Run

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.seed
streamlit run app/portal/streamlit_app.py
```

Database credentials are loaded from the `DATABASE_*` variables documented in
`.env.example`. The portal does not read RFQ or output files directly.

## Portal

- **Dashboard:** queue and review metrics, plus recent activity.
- **Processing queue:** follow worker activity, scheduled retries, attempts,
  and per-job history. Refreshes every five seconds while open.
- **Review queue:** inspect draft lines, prices from the product catalog,
  confidence/flags, and the audit history.
- **New matches:** search catalog candidates and save a corrected part number
  without overwriting the worker's original match.
- **Decisions:** approve or reject completed drafts, requeue failed work, and
  cancel jobs which have not started.

Every write uses a state precondition and creates a `job_events` audit record.
See [`docs/CONTRACT.md`](docs/CONTRACT.md) for the full data contract.

## Repository

- `app/dispatcher/`: discovers and queues RFQ files.
- `app/worker/`: extracts and matches draft quote lines.
- `app/portal/`: Streamlit UI and transactional Postgres operations.
- `db/schema.sql`: authoritative PostgreSQL schema.
- `docs/CONTRACT.md`: component boundaries and lifecycle rules.

## Approach

The queue uses PostgreSQL leases and `FOR UPDATE SKIP LOCKED` so workers can run
concurrently. Reviewers see only database-backed drafts; catalog prices remain
read-only and every quote requires a human decision.

## Evaluation

## Production
