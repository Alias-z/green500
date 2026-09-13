CREATE TABLE IF NOT EXISTS companies (
    cik text PRIMARY KEY CHECK (cik ~ '^[0-9]{10}$'),
    name text NOT NULL,
    sector text NOT NULL,
    symbols text[] NOT NULL,
    is_current boolean NOT NULL DEFAULT true,
    website text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS documents (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_cik text REFERENCES companies(cik),
    url text NOT NULL,
    final_url text NOT NULL,
    title text NOT NULL DEFAULT '',
    kind text NOT NULL CHECK (kind IN ('index','report','feed','directory')),
    sha256 text NOT NULL,
    byte_count bigint NOT NULL,
    content_type text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    last_checked_at timestamptz NOT NULL DEFAULT now(),
    source_updated_at text,
    revision text,
    reporting_year integer,
    parse_status text NOT NULL DEFAULT 'pending',
    parsed_sha256 text,
    parse_error text,
    UNIQUE NULLS NOT DISTINCT (company_cik, url, sha256, kind)
);
CREATE TABLE IF NOT EXISTS index_snapshots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES documents(id),
    parser_version text NOT NULL,
    result_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(document_id,parser_version,result_digest)
);
CREATE TABLE IF NOT EXISTS index_memberships (
    document_id bigint REFERENCES documents(id),
    snapshot_id bigint NOT NULL REFERENCES index_snapshots(id),
    symbol text NOT NULL,
    company_cik text NOT NULL REFERENCES companies(cik),
    source_row integer NOT NULL,
    source_fields jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, symbol)
);
CREATE TABLE IF NOT EXISTS tasks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('collect_sp500','collect_source','extract')),
    company_cik text REFERENCES companies(cik),
    input jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','succeeded','partial','failed','unknown','cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    expires_at timestamptz,
    error text,
    result jsonb,
    input_key text NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON tasks(input_key) WHERE status IN ('pending','running');
CREATE TABLE IF NOT EXISTS task_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id bigint NOT NULL REFERENCES tasks(id),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running',
    detail jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS extractions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id bigint NOT NULL REFERENCES tasks(id),
    document_id bigint NOT NULL REFERENCES documents(id),
    input_sha256 text NOT NULL,
    cache_key text,
    replayed_from bigint REFERENCES extractions(id),
    response_sha256 text,
    result_sha256 text,
    model text NOT NULL,
    prompt_version text NOT NULL,
    chunk_number integer NOT NULL,
    status text NOT NULL,
    usage jsonb NOT NULL DEFAULT '{}',
    duration_ms integer,
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS completed_extraction_input ON extractions(input_sha256) WHERE status='succeeded';
CREATE TABLE IF NOT EXISTS metrics (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id bigint NOT NULL REFERENCES extractions(id),
    company_cik text NOT NULL REFERENCES companies(cik),
    metric_code text NOT NULL,
    reporting_year integer,
    raw_value text NOT NULL,
    raw_unit text NOT NULL,
    value numeric,
    unit text,
    details jsonb NOT NULL,
    validation_status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS controls (
    id integer PRIMARY KEY CHECK (id=1),
    is_paused boolean NOT NULL DEFAULT false,
    current_index_document_id bigint REFERENCES documents(id),
    current_index_snapshot_id bigint REFERENCES index_snapshots(id)
);
INSERT INTO controls(id) VALUES (1) ON CONFLICT DO NOTHING;
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_kind_check;
ALTER TABLE documents ADD CONSTRAINT documents_kind_check CHECK (kind IN ('index','report','feed','directory','dataset'));
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_key text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS acquisition_method text NOT NULL DEFAULT 'scrapy';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS preview_sha256 text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS preview_version text;
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_kind_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_kind_check CHECK (kind IN ('collect_sp500','collect_source','extract','collect_financial','collect_targets','collect_sec_reports','collect_sustainability_reports'));
CREATE TABLE IF NOT EXISTS structured_observations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_cik text NOT NULL REFERENCES companies(cik),
    source_key text NOT NULL,
    dataset_id bigint NOT NULL REFERENCES documents(id),
    metric_code text NOT NULL,
    value jsonb NOT NULL,
    unit text,
    period_start date,
    period_end date,
    period_type text NOT NULL CHECK (period_type IN ('annual','quarterly','instant','current')),
    published_at date,
    source_record jsonb NOT NULL,
    match_method text NOT NULL,
    fingerprint text NOT NULL UNIQUE,
    observed_at timestamptz NOT NULL DEFAULT now(),
    last_checked_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS observations_company ON structured_observations(company_cik,source_key,metric_code);
CREATE TABLE IF NOT EXISTS source_checks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_cik text REFERENCES companies(cik),
    source_key text NOT NULL,
    status text NOT NULL,
    error text,
    dataset_id bigint REFERENCES documents(id),
    details jsonb NOT NULL DEFAULT '{}',
    checked_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS checks_company_source ON source_checks(company_cik,source_key,id DESC);
CREATE OR REPLACE VIEW latest_observations AS
SELECT DISTINCT ON (company_cik,source_key,metric_code,period_type) *
FROM structured_observations
ORDER BY company_cik,source_key,metric_code,period_type,period_end DESC NULLS LAST,
         published_at DESC NULLS LAST,observed_at DESC,id DESC;
CREATE OR REPLACE VIEW source_status AS
SELECT DISTINCT ON (company_cik,source_key) * FROM source_checks
ORDER BY company_cik,source_key,id DESC;
CREATE TABLE IF NOT EXISTS source_schedules (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE,
    task_kind text NOT NULL,
    company_cik text REFERENCES companies(cik),
    input jsonb NOT NULL DEFAULT '{}',
    interval_hours integer NOT NULL CHECK (interval_hours>=1),
    next_check_at timestamptz NOT NULL DEFAULT now(),
    is_enabled boolean NOT NULL DEFAULT true,
    last_task_id bigint REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS report_sources (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_cik text NOT NULL REFERENCES companies(cik),
    url text NOT NULL,
    title text NOT NULL,
    categories text[] NOT NULL CHECK (
        cardinality(categories)>0 AND categories <@ ARRAY['financial_report','environment_report','social_employee','financial_targets','climate_targets']::text[]),
    reporting_period text,
    discovered_via text NOT NULL DEFAULT 'agent',
    evidence jsonb NOT NULL DEFAULT '[]',
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(company_cik,url)
);
ALTER TABLE report_sources ADD COLUMN IF NOT EXISTS download_status text;
ALTER TABLE report_sources ADD COLUMN IF NOT EXISTS latest_download_task_id bigint REFERENCES tasks(id);
ALTER TABLE report_sources ADD COLUMN IF NOT EXISTS last_download_attempt_at timestamptz;
ALTER TABLE report_sources ADD COLUMN IF NOT EXISTS review jsonb NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS completed_extraction_cache ON extractions(cache_key) WHERE status='succeeded';
CREATE UNIQUE INDEX IF NOT EXISTS one_offline_replay ON extractions(replayed_from) WHERE replayed_from IS NOT NULL;
CREATE OR REPLACE VIEW analysis_metrics AS
SELECT DISTINCT ON (m.company_cik,d.sha256,m.metric_code,m.reporting_year,m.raw_value,m.raw_unit,
                    m.details->>'boundary',m.details->>'calculation_method',m.details->>'value_kind')
       m.*,e.document_id
FROM metrics m JOIN extractions e ON e.id=m.extraction_id JOIN documents d ON d.id=e.document_id
WHERE m.details->>'meaning_checked' = 'true'
ORDER BY m.company_cik,d.sha256,m.metric_code,m.reporting_year,m.raw_value,m.raw_unit,
         m.details->>'boundary',m.details->>'calculation_method',m.details->>'value_kind',m.id DESC;
