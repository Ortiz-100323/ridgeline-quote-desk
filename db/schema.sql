-- Ridgeline Quote Desk: database schema (PostgreSQL 16).
-- Source of truth for tables. Described for humans and for Codex in docs/CONTRACT.md.
-- Idempotent: safe to run more than once (python -m app.seed).

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy description matching (future matcher)

-- Same rule as app.catalog.normalize_part_number(): uppercase, drop whitespace . / -
CREATE OR REPLACE FUNCTION normalize_part_number(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT regexp_replace(upper(coalesce(value, '')), '[[:space:]./-]', '', 'g') $$;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- ---------------------------------------------------------------------------
-- Reference data (loaded from data/*.csv by app/seed.py)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS customers (
    customer_id   text PRIMARY KEY,
    name          text NOT NULL,
    region        text NOT NULL,
    pricing_tier  text NOT NULL CHECK (pricing_tier IN ('contract', 'standard'))
);

CREATE TABLE IF NOT EXISTS products (
    part_number        text PRIMARY KEY,
    part_number_norm   text GENERATED ALWAYS AS (normalize_part_number(part_number)) STORED UNIQUE,
    description        text NOT NULL,
    category           text NOT NULL,
    unit               text NOT NULL,
    list_price         numeric(12, 2) NOT NULL CHECK (list_price >= 0),
    status             text NOT NULL CHECK (status IN ('active', 'discontinued')),
    replaced_by        text REFERENCES products (part_number),
    primary_warehouse  text NOT NULL,
    CHECK (replaced_by IS NULL OR replaced_by <> part_number)
);
CREATE INDEX IF NOT EXISTS products_description_trgm ON products USING gin (description gin_trgm_ops);
CREATE INDEX IF NOT EXISTS products_category ON products (category);

-- One competitor part may map to several Ridgeline parts (ambiguous on purpose),
-- so the key is the pair, not the competitor part number alone.
CREATE TABLE IF NOT EXISTS cross_references (
    cross_reference_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    competitor                   text NOT NULL,
    competitor_part_number       text NOT NULL,
    competitor_part_number_norm  text GENERATED ALWAYS AS (normalize_part_number(competitor_part_number)) STORED,
    ridgeline_part_number        text NOT NULL REFERENCES products (part_number),
    notes                        text,
    last_verified                date,
    source                       text NOT NULL DEFAULT 'seed' CHECK (source IN ('seed', 'portal_correction')),
    created_at                   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (competitor_part_number_norm, ridgeline_part_number)
);
CREATE INDEX IF NOT EXISTS cross_references_norm ON cross_references (competitor_part_number_norm);

-- ---------------------------------------------------------------------------
-- Job queue: one row per incoming RFQ file
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS jobs (
    job_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rfq_id            text NOT NULL,                 -- file stem, e.g. rfq_01_email

    -- the file the worker must read
    source_path       text NOT NULL,                 -- POSIX path relative to the repo root, e.g. rfqs/rfq_01_email.txt
    file_name         text NOT NULL,                 -- e.g. rfq_01_email.txt
    file_type         text NOT NULL CHECK (file_type IN ('text', 'pdf', 'xlsx', 'image', 'other')),  -- document type
    file_extension    text,                          -- e.g. .pdf (lowercase)
    mime_type         text,
    file_size_bytes   bigint CHECK (file_size_bytes >= 0),
    source_sha256     text NOT NULL,                 -- content hash. NOT unique while testing: every run queues every file.
                                                     -- TODO(dedupe): make it unique before production (docs/CONTRACT.md §8)
    dispatch_run_id   uuid,                          -- the dispatcher run that queued the job (pipeline_logs.run_id)

    job_status        text NOT NULL DEFAULT 'pending'
                      CHECK (job_status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    review_status     text NOT NULL DEFAULT 'not_ready'
                      CHECK (review_status IN ('not_ready', 'pending_review', 'approved', 'rejected')),
    priority          smallint NOT NULL DEFAULT 0,   -- higher runs first

    -- retries
    attempts          smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts      smallint NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    run_after         timestamptz NOT NULL DEFAULT now(),  -- backoff: not claimable before this

    -- worker lease (a crashed worker's job is reclaimed after lease_expires_at)
    locked_by         text,
    locked_at         timestamptz,
    lease_expires_at  timestamptz,

    -- outcome
    output_path       text,                          -- e.g. outputs/rfq_01_email.json
    model_used        text,
    fallback_used     boolean NOT NULL DEFAULT false,
    last_error        text,                          -- technical error of the last attempt
    escalation_reason text,                          -- why the whole request went to a person

    -- review (portal)
    reviewed_by       text,
    reviewed_at       timestamptz,
    rejection_reason  text,

    -- timestamps
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,                   -- first claim
    finished_at       timestamptz,                   -- reached succeeded / failed / cancelled

    CHECK (job_status <> 'running' OR (locked_by IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK (job_status NOT IN ('succeeded', 'failed', 'cancelled') OR finished_at IS NOT NULL),
    CHECK (review_status = 'not_ready' OR job_status IN ('succeeded', 'failed')),
    CHECK (review_status NOT IN ('approved', 'rejected') OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)),
    CHECK (review_status <> 'rejected' OR nullif(btrim(rejection_reason), '') IS NOT NULL),
    CHECK (review_status <> 'approved' OR job_status = 'succeeded'),
    CHECK (attempts <= max_attempts)
);
CREATE INDEX IF NOT EXISTS jobs_claimable ON jobs (priority DESC, created_at) WHERE job_status = 'pending';
CREATE INDEX IF NOT EXISTS jobs_lease ON jobs (lease_expires_at) WHERE job_status = 'running';
CREATE INDEX IF NOT EXISTS jobs_review ON jobs (review_status, finished_at);
CREATE INDEX IF NOT EXISTS jobs_rfq ON jobs (rfq_id);
-- Upgrades for databases created before these columns existed
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS file_extension text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dispatch_run_id uuid;
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_source_sha256_key;   -- testing: duplicates allowed
CREATE INDEX IF NOT EXISTS jobs_sha256 ON jobs (source_sha256);
CREATE INDEX IF NOT EXISTS jobs_dispatch_run ON jobs (dispatch_run_id);
DROP TRIGGER IF EXISTS jobs_updated_at ON jobs;
CREATE TRIGGER jobs_updated_at BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Audit trail of every status change
CREATE TABLE IF NOT EXISTS job_events (
    event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      uuid NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    event       text NOT NULL CHECK (event IN (
                    'created', 'claimed', 'retry_scheduled', 'succeeded', 'failed', 'escalated',
                    'lease_expired', 'approved', 'rejected', 'cancelled', 'requeued',
                    'part_corrected')),
    actor       text NOT NULL,     -- dispatcher, worker:<id>, portal:<user>
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- CREATE TABLE IF NOT EXISTS does not update an existing CHECK when a new
-- audit event is introduced, so refresh this named constraint idempotently.
ALTER TABLE job_events DROP CONSTRAINT IF EXISTS job_events_event_check;
ALTER TABLE job_events ADD CONSTRAINT job_events_event_check CHECK (event IN (
    'created', 'claimed', 'retry_scheduled', 'succeeded', 'failed', 'escalated',
    'lease_expired', 'approved', 'rejected', 'cancelled', 'requeued',
    'part_corrected'
));
CREATE INDEX IF NOT EXISTS job_events_job ON job_events (job_id, created_at);

-- End-to-end operational log: every step of every component, with or without a job.
-- job_events records status changes; pipeline_logs records everything that happened.
-- Follow one RFQ from dispatch to review with: WHERE job_id = ... ORDER BY log_id.
CREATE TABLE IF NOT EXISTS pipeline_logs (
    log_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    run_id       uuid NOT NULL,          -- one process run (a dispatcher run, a worker session, a portal request)
    job_id       uuid REFERENCES jobs (job_id) ON DELETE SET NULL,
    component    text NOT NULL CHECK (component IN ('dispatcher', 'worker', 'portal', 'seed', 'evaluation')),
    actor        text NOT NULL,          -- dispatcher, worker:<id>, portal:<user>
    level        text NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
    step         text NOT NULL,          -- e.g. run_started, file_found, job_created, run_finished
    message      text NOT NULL,
    duration_ms  integer CHECK (duration_ms >= 0),
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS pipeline_logs_job ON pipeline_logs (job_id, log_id);
CREATE INDEX IF NOT EXISTS pipeline_logs_run ON pipeline_logs (run_id, log_id);
CREATE INDEX IF NOT EXISTS pipeline_logs_errors ON pipeline_logs (created_at) WHERE level IN ('WARNING', 'ERROR');

-- ---------------------------------------------------------------------------
-- Draft quote produced by a worker (mirrors outputs/<rfq_id>.json)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS quotes (
    job_id         uuid PRIMARY KEY REFERENCES jobs (job_id) ON DELETE CASCADE,
    rfq_id         text NOT NULL,
    customer_id    text REFERENCES customers (customer_id),   -- null when not identified
    customer_name  text,
    non_item_rows  text[] NOT NULL DEFAULT '{}',
    output_json    jsonb NOT NULL,                             -- exact content written to outputs/
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quote_lines (
    job_id                 uuid NOT NULL REFERENCES quotes (job_id) ON DELETE CASCADE,
    line                   integer NOT NULL CHECK (line >= 1),
    as_written             text NOT NULL CHECK (as_written <> ''),
    quantity               numeric,
    unit                   text,
    ridgeline_part_number  text REFERENCES products (part_number),
    match_method           text CHECK (match_method IN ('ridgeline_part_number', 'cross_reference', 'description', 'llm')),
    confidence             numeric(4, 3) CHECK (confidence BETWEEN 0 AND 1),
    needs_review           boolean NOT NULL,
    flags                  text[] NOT NULL DEFAULT '{}',
    note                   text,
    corrected_part_number  text REFERENCES products (part_number),  -- portal fix (future)
    PRIMARY KEY (job_id, line),
    CHECK (ridgeline_part_number IS NULL OR match_method IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS quote_lines_review ON quote_lines (job_id) WHERE needs_review;
