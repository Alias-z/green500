Updated: 2026-09-13

# Setup and operation

## Standard financial extraction

The team schema is implemented in `green500/processing/financial_schema.py`. The versioned extraction and review prompts live in `green500/processing/financial_prompt.py`. Root-level `financial.py` is not required.

Configure any OpenAI-compatible Chat Completions service through `GREEN500_LLM_BASE_URL` (including its `/v1` prefix when required), `GREEN500_LLM_API_KEY`, and `GREEN500_LLM_MODEL`. Granny Data and Coding/Agent Plans are optional deployment integrations.

Prepare the latest saved annual filing for each company without calling a model:

```bash
uv run python -m green500.processing.financial_batch --from-catalog --write-manifest data/financial_extractions/annual_manifest.json
```

Process that manifest with independent concurrent requests:

```bash
uv run python -m green500.processing.financial_batch --manifest data/financial_extractions/annual_manifest.json --concurrency 4
```

Only 10-K, 20-F and 40-F annual filings enter the catalog-generated manifest. Missing annual filings are listed in `data/financial_extractions/annual_filing_gaps.json`. For standalone use, supply a JSON list with `company_id`, `company_name`, `path`, `content_type`, `source_document` and `source_url` per original; `sha256`, `document_id`, `filename` and `fiscal_year` are optional verified context. No database is needed for a supplied manifest.

Each company/result directory contains `financial.json` after local schema and source checks pass, plus `evidence.json`, requests, raw responses and a durable `receipt.json`. `progress.json` reports completed, failed and unknown outcomes. Successful results are reused after hash validation. `--retry-failed` explicitly admits another attempt for failed or unknown work; reconcile unknown provider outcomes before using it. The source, schema, prompts, endpoint and model determine result identity.

The model returns compact metric values and source references. Program code adds shared company/source metadata, validates the team schema and hydrates exact citations. Money uses its currency code (for example `USD`) with amounts already scaled to base units; employees use `employees`. Missing values remain null. Total capex never substitutes for green transition capex. By default, successful extraction needs one model call. Add `--review-with-model` to request a separate model review of period, scope and meaning. Local checks do not invoke a model. Receipts record whether model review was requested and performed. Selected evidence may omit disclosures, and absence notes preserve that limitation.

## Fixed environment, social, climate-target and financial-target features

The fixed feature schemas process four report categories directly from saved category reports:

- `environment_report`
- `social_employee`
- `climate_targets`
- `financial_targets`

Create a source manifest without calling a model:

```bash
uv run python -m green500.processing.fixed_report_batch \
  --category environment_report \
  --write-manifest data/structured_extractions/environment_report_manifest.json
```

Process one saved manifest through a configured OpenAI-compatible Chat Completions service:

```bash
uv run python -m green500.processing.fixed_report_batch \
  --category environment_report \
  --manifest data/structured_extractions/environment_report_manifest.json \
  --output-dir data/structured_extractions/environment_report \
  --concurrency 3
```

Use the same commands with the other category names. Catalog-generated manifests admit saved
originals registered for the category. A current `latest_verified` source is preferred; an
available source with unknown or older freshness remains eligible and keeps that status in its
source metadata. Companies without a saved original remain explicit gaps. This workflow does not
read earlier model output and does not project legacy environmental or climate records into the
fixed schema. Existing successful annual financial extraction remains separate and unchanged.

The category definitions are in `green500/processing/environment_features.py`,
`social_features.py`, `climate_features.py` and `financial_target_features.py`. Every fixed result
contains all declared keys under both `values` and `metadata`. Missing or unsupported values are
null. Metadata retains status, year, reason, qualification, confidence and source evidence outside
the primitive predictor values. Unknown units remain null. Numeric evidence must support the
canonical unit and conversion scale; an invalid individual value is omitted without inventing a
replacement.

Every category has exactly ten predictor fields. Environment contains actual emissions and
resource-use measures. Social contains workforce, turnover, representation, worker-safety,
supplier-audit, confirmed-violation and community-investment measures. Climate contains net-zero,
SBTi, and absolute Scope 1+2 and complete Scope 3 target baseline/year/reduction/progress fields.
Financial targets contain lower and upper percentage endpoints for annual revenue growth and
operating margin and long-term revenue growth, operating margin and EPS growth. A point financial
target fills both endpoints with the same value; a one-sided bound leaves the unsupported endpoint
null.

For numeric fields, a nonliteral model quotation can be replaced with an exact source row when a
field label and number identify one row, or with nearby original text when the raw number occurs
once in its referenced source block. Numeric grouping punctuation can be restored from that row.
Ambiguous or absent numbers remain null. Boolean quotations must resolve to original text directly.
The current climate contract contains target reduction magnitudes and progress percentages on a
zero-to-one-hundred scale. Negative emissions, energy, water, waste or social actuals remain invalid.

Text preparation is category-specific and lives in `green500/processing/document_evidence.py`.
Readable PDF text retains page identifiers; a fully image-based PDF uses bounded local Tesseract
OCR when available. Social source evidence is capped at 29,500 characters,
adds at most 5,000 characters of field candidates, excludes leading boilerplate, and balances
selection across the ten social fields. Climate target evidence is capped at 60,000 characters;
financial-target evidence is capped at 45,000 characters. PDF parsing runs through the Green500
project environment even when a shared external model gateway starts the batch.

If a model returns the copied raw number before an explicit unit conversion, the validator derives
the canonical value and records the model value and correction in the evidence sidecar. A proposed
value that matches neither the copied number nor its canonical conversion remains invalid.

Environmental actuals use tCO2e, MWh, m3, metric tonnes, hectares and percentages from zero to one
hundred. Social counts are integers. Injury rates per 200,000 hours and per 1,000,000 hours are
different columns and remain null when the source omits the denominator. Community investment is
included only when US dollars are explicit; no currency conversion is inferred. Environmental and
social target statements do not populate actual-performance fields. Free-form observations remain
outside the machine-learning matrix, and S&P ESG and CSA scores remain separate labels.

The usual successful path makes one model call. A failed schema or local source check may request
one correction from the same original evidence. No additional model review runs. Result directories
retain the original hash, selected blocks, request, raw response, validated fixed data, evidence,
validation summary and receipt so a reviewer can reproduce each accepted primitive.

Authenticated viewers can read one company's category result from
`/api/catalog/{cik}/processed/{category}`. The response includes the common fixed data shape,
field definitions, source document, processing status, model and processing time. Add
`?download=true` to download that category's JSON.

The fixed matrix is available from `/api/features` with `format=csv`, `json`, `metadata` or
`schema`. CSV and JSON expose predictor values. Metadata retains field-level absence and source
context, while schema describes stable names, types and canonical units. These routes use the same
viewer password session as the catalog. They never include S&P ESG or CSA labels as predictors.

## Export nonlinear-model training data

Build a versioned snapshot after the fixed original-report results are available:

```bash
uv run python -m green500.training_data --output-dir data/ml/training
```

The snapshot directory contains `X.csv`, `labels.csv`, `companies.csv`, `metadata.json`,
`schema.json`, `coverage.json` and `manifest.json`. The files are row-aligned: row N describes the
same company in each file. Use `companies.csv` to associate a row with its CIK, ticker and name.
`X.csv` contains only numeric predictors plus `sector`; it has no identifier, status, ESG score or
CSA score. Labels stay in `labels.csv`, and detailed missing reasons, years and source references
stay in `metadata.json`.

Missing values remain empty CSV cells and null JSON values. The exporter does not impute, scale,
encode or select features. Fit those transformations inside each training fold when evaluating an
Explainable Boosting Machine, CatBoost or XGBoost model. The command only prepares compatible
training data; it does not train or tune a model.

`data/ml/training/latest.json` points to the current content-addressed snapshot and binds its
manifest hash. An authenticated viewer can download that exact snapshot as a ZIP archive from
`GET /api/training-data`.

## Configure

Create a dedicated PostgreSQL database and application login. The application must not use a PostgreSQL superuser. PostgreSQL 15 or newer is required.

```bash
cp .env.example .env
chmod 600 .env
# Fill in database credentials and a compatible model API configuration.
uv sync --locked
uv run python -m green500 init-db
uv run python -m green500 collect sp500
GREEN500_RUN_WORKER=false uv run python -m green500 serve --port 19973
```

Install Tesseract separately when fully image-based PDF reports must be read. The normal PDF path
does not require OCR. Install the optional ML packages with `uv sync --locked --extra ml`.

Open `http://127.0.0.1:19973` and sign in with the configured `GREEN500_VIEW_PASSWORD`. Viewer sessions use a persistent signing key and renewable 30-day cookies. Keep `data/.viewer-session-key` private and persistent across web restarts. Administrative API actions require the separate `GREEN500_OPS_TOKEN`; viewer access never grants it. Supply any reverse proxy or public tunnel outside this application.

Run collection in a separate worker when scheduled updates are needed:

```bash
uv run python -m green500 worker
uv run python -m green500 worker --once
```

## Collect sources

Supply a CIK from the current company list and a public source URL:

```bash
uv run python -m green500 collect report --cik <CIK> --url <URL> --year <REPORT_YEAR>
uv run python -m green500 collect directory --cik <CIK> --url <URL> --page-limit 10
uv run python -m green500 collect feed --cik <CIK> --url <RSS_OR_ATOM_OR_SITEMAP_URL>
```

Add `--enqueue` to persist collection work without executing it in the command. Fixed report and annual financial extraction use the manifest-driven processing commands documented above.

The document's supplied report year is a navigation label. Metric reporting years must come from cited source evidence. A report published in one year may describe the previous fiscal year.

## Current structured data and monitoring

`GREEN500_MAX_DOCUMENT_BYTES=0` permits original files of any size and is the default. A positive value sets an optional byte limit; negative values are invalid. Reviewed large originals use streaming file storage. Text parsing retains its separate resource controls, so a large original can be available before its text is extracted. Download success does not establish that every page can be parsed, and it does not dispatch a model call.

```bash
uv run python -m green500 collect financial --cik 0000789019
uv run python -m green500 collect financial
uv run python -m green500 collect targets
uv run python -m green500 monitor
```

Financial collection selects latest annual and standalone-quarter revenue and net income, plus latest instant assets. Targets import the current public SBTi workbooks. Unique-name matches are provisional; unmatched companies retain an explicit source outcome.

`monitor` registers daily constituent/financial checks, weekly SBTi checks, and known company reports weekly or feeds daily. The independent worker admits due work; pause stops admission. Run `monitor` again after registering additional company sources. It preserves existing deadlines. It never queues model extraction automatically.

Authenticated `GET /api/observations` delivers current structured observations with periods, provider records and file hashes. `GET /api/schedules` shows future checks. The reviewed public-data example is at `/reports/microsoft/`; English documents are at `/docs/data-design` and `/docs/data-sources`.

## Model configuration

Set `GREEN500_LLM_BASE_URL`, `GREEN500_LLM_API_KEY` and `GREEN500_LLM_MODEL` for a Chat Completions-compatible endpoint. The client reuses HTTP connections across a batch. Model output is validated locally with Pydantic, units, company identity, reporting year and exact source quotations. A recorded invalid output receives at most one correction request. Raw provider errors and rejected output remain inspectable. Explicit `--retry-failed` processing preserves prior attempts.

## Development and verification

```bash
uv run python -m pytest -q
npm ci
npm run build
```

Tests use the configured dedicated Green500 database and delete only their explicit test records. Never point this configuration at another application's database.

Back up the configured data directory and run `pg_dump -Fc` against the Green500 database. Verify a restore before relying on a backup. No automatic cloud backup or external publication occurs.

## Runtime data and report samples

The viewer's `5 categories with files` filter selects companies with at least one downloaded original in every category. `5 categories latest-checked` requires at least one valid latest-source check per category. Source periods and metric coverage still differ. Collection selects company names in order and completion work fills missing categories; parallel requests can finish in a different order. `data/source_samples/complete_companies.json` is a dated, hash-verified sample manifest.

Original reports, extraction requests, provider responses, validated results, progress files, ML artifacts and credentials are runtime data and are ignored by Git. Keep only small reviewed examples under `data/report_examples/` and stable reference inputs under `data/reference/`.

## Review report freshness

A downloaded file starts as `Latest source unchecked`. An evidence-backed review binds to its exact SHA-256 and expires within at most 30 days. The viewer shows a newer-source notice on historical files when a replacement has been found, and keeps both originals accessible. Verify publication date, reporting period, target status and organizational scope separately.

Daily SEC latest-filing checks retain submissions-list evidence and renew seven-day filing reviews. Airbnb's official sustainability hub is checked daily through its detected Q4 ContentAsset widget to discover new report URLs. A Q4 download does not automatically become a verified-latest report: body and publisher evidence still require review. Other unreviewed publisher sources remain explicitly unchecked.

Freshness remains source metadata and helps order candidates. It does not prevent a saved category report from entering fixed extraction.
