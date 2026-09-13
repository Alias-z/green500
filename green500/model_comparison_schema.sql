-- Verified model snapshots are published explicitly; HTTP only reads them.
CREATE TABLE IF NOT EXISTS model_comparison_snapshots (
    snapshot_id text PRIMARY KEY,
    run_id text NOT NULL,
    run_directory text NOT NULL,
    prediction_as_of date NOT NULL,
    summary jsonb NOT NULL,
    company_count integer NOT NULL CHECK (company_count > 0),
    published_at timestamptz NOT NULL DEFAULT now(),
    is_current boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS model_comparison_one_current
    ON model_comparison_snapshots ((1)) WHERE is_current;
CREATE TABLE IF NOT EXISTS model_comparison_company_rows (
    snapshot_id text NOT NULL REFERENCES model_comparison_snapshots(snapshot_id),
    company_cik text NOT NULL REFERENCES companies(cik),
    company_name text NOT NULL,
    ticker text NOT NULL,
    industry text NOT NULL,
    source_row jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, company_cik)
);
CREATE TABLE IF NOT EXISTS model_comparison_predictions (
    snapshot_id text NOT NULL,
    company_cik text NOT NULL,
    target text NOT NULL CHECK (target IN ('esg', 'csa')),
    ebm double precision NOT NULL,
    catboost double precision NOT NULL,
    evaluation jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, company_cik, target),
    FOREIGN KEY (snapshot_id, company_cik) REFERENCES model_comparison_company_rows(snapshot_id, company_cik)
);
CREATE INDEX IF NOT EXISTS model_comparison_prediction_sort
    ON model_comparison_predictions (snapshot_id, target, ebm DESC);
CREATE TABLE IF NOT EXISTS model_comparison_feature_values (
    snapshot_id text NOT NULL,
    company_cik text NOT NULL,
    target text NOT NULL CHECK (target IN ('esg', 'csa')),
    feature_name text NOT NULL,
    category text NOT NULL,
    numeric_value double precision,
    text_value text,
    PRIMARY KEY (snapshot_id, company_cik, target, feature_name),
    FOREIGN KEY (snapshot_id, company_cik, target) REFERENCES model_comparison_predictions(snapshot_id, company_cik, target)
);
CREATE INDEX IF NOT EXISTS model_comparison_feature_filter
    ON model_comparison_feature_values (snapshot_id, target, feature_name, numeric_value);
