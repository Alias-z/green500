Updated: 2026-09-13

# Green500 specification

### Component Index

| Component | Function | Entry point |
|---|---|---|
| Source collection | Fetch public index, report, directory and feed responses through bounded Scrapy HTTP requests | `green500/crawl.py` |
| Agent source discovery | Prepare read-only, evidence-bound issuer report candidates without production writes or bundled browser infrastructure | `.codex/skills/green500-source-discovery/SKILL.md` |
| Evidence storage | Atomically store and verify immutable source bytes by full content hash | `green500/storage.py` |
| PostgreSQL persistence | Record companies, source versions, tasks, attempts, extractions and metrics | `green500/db.py`, `green500/schema.sql` |
| Document parsing | Produce citable HTML text and page-scoped PDF text, with bounded OCR for fully image-based PDFs | `green500/documents.py`, `green500/pdf_reader.py` |
| Model transport | Call any configured OpenAI-compatible Chat Completions endpoint | `green500/processing/chat_completion.py` |
| Task execution | Claim durable work, isolate execution and preserve failed or unknown outcomes | `green500/worker.py` |
| Single-table Ops | Inspect company progress and evidence and enqueue bounded actions | `green500/web.py`, `green500/static/app.ts` |
| Command line | Set up the schema and run collection, extraction, worker or server commands | `green500/__main__.py` |
| Structured sources | Parse current SEC financial facts and SBTi target workbooks | `green500/financial_sources.py`, `green500/target_sources.py` |
| Structured evidence | Store deterministic observations and append source check outcomes | `green500/data_store.py` |
| Source monitoring | Admit due source schedules into the existing queue | `green500/monitor.py` |
| Report catalog | Link reviewed categories to originals and expose read-only coverage | `green500/catalog.py` |
| Source freshness | Bind publisher-list reviews to exact file hashes and expose unknown or newer-source outcomes | `green500/freshness.py` |
| Dynamic issuer listings | Read positively identified Q4 ContentAsset widgets through bounded Scrapy requests | `green500/q4_sources.py` |
| Viewer access | Protect read-only catalog and originals with expiring password sessions | `green500/view_access.py` |
| Report evidence preparation | Normalize saved originals into bounded, line-preserving source blocks | `green500/processing/document_evidence.py` |
| Standard financial extraction | Validate the team's financial schema, call a configured chat endpoint and checkpoint source-checked annual-filing results | `green500/processing/financial_schema.py`, `green500/processing/financial_batch.py` |
| Financial results viewer | Join verified extraction receipts to company annual filings and expose read-only status, metrics and evidence | `green500/processed_financial.py`, `green500/web.py` |
| Fixed comparable features | Define closed primitive predictor columns, canonical units and separate field metadata for environmental, social, climate-target and financial-target reports | `green500/processing/*features.py`, `green500/processing/fixed_schema.py` |
| Fixed original-report extraction | Extract saved category reports into versioned fixed records without reading earlier model output | `green500/processing/fixed_report_batch.py`, `green500/processing/fixed_*_profile.py` |
| Training-data snapshots | Export row-aligned predictors, labels, companies and missingness metadata for nonlinear model work | `green500/training_data.py` |
| Score-model pipeline | Audit dated inputs, build assessment-cycle datasets, train EBM and CatBoost models, and run saved-model predictions and contribution-based rankings | `green500/ml/__main__.py` |

### Data and publication

PostgreSQL owns structured data and task status. Project-local files own immutable original bytes and exact model records. File exports do not replace database authority. Publication verifies that its task is still active and commits related database changes atomically. A source version is separate from its parsed index snapshot. Reprocessing cannot silently reuse rows from a different parsed result. Current company metadata is a cache; historical membership queries use their snapshot fields.

Company identifiers preserve the source CIK as an external registrant identity. Each index snapshot retains all security rows and their original names and classifications. Acquisition time, source update time and effective membership time are distinct; unknown effective dates are not invented.

### Processing and evidence

Original downloads accept a nonnegative optional byte limit; zero permits any original file size. Reviewed local originals can be copied into immutable storage in chunks, with full content-hash verification and atomic publication. Parsing has independent resource controls. Collection retains originals independently of parsing and interpretation; acquiring a report never implies complete text coverage or an automatic model call.

Project-local `data/objects/` stores original bytes by hash. Structured provider imports use `structured_observations` and `latest_observations`, separately from model-derived metrics. Observation identity includes company, source, reporting period, value and provider record. Rechecks deduplicate unchanged observations. Annual, quarterly and instant values never overwrite one another. Source checks append success, failure and identity uncertainty without deleting previous values. Name-only matches remain provisional. Enabled source schedules enqueue due work through the same bounded worker; report interpretation remains explicit. `collect financial` and `collect targets` expose these adapters through the CLI.

Every admitted task receives an explicit outcome. An expired or interrupted model call may have an unknown provider outcome. Retries preserve earlier records. Successful cached extraction is bound to unchanged source content and extraction configuration. Paid calls preserve their actual provider response and available usage. Slow I/O runs outside the Scrapy event loop and outside database transactions.

Structured model output must match the configured schema. Reported numeric text, units and source quotations must be supported by the cited lines. Deterministic normalization preserves original values and marks unsupported units or context for review. Quotation checks do not independently verify disclosure truth or semantic equivalence. Partial document parsing remains visible and cannot establish complete report coverage.

Each fixed quantity passes deterministic company identity, schema, unit, reporting-year, source
quotation and category-specific semantic checks. One correction call may follow an invalid model
response. No separate model review is required for the fixed feature path. Original attempts and
rejected values remain inspectable. This is automated source interpretation, not an independent
audit.

### Fixed comparable features

Environmental, social and employee, climate-target and financial-target processing uses a
closed feature contract. Each category owns a `FIELDS` mapping that defines every predictor's
stable name, primitive type, canonical unit and meaning. The response has `company`, one common
`reporting_year`, `boundary`, `limitations`, `values` and `metadata`. The `values` and `metadata`
objects contain the same complete required key set. Predictor values are numbers, integers,
booleans or null. Missing disclosure, unsupported units, incompatible periods and conflicts stay
null and never become zero.

Each category contains exactly ten predictor columns. Environment owns current company-wide
emissions, energy, renewable-electricity share, water withdrawal and waste. Social owns employee
count, turnover, workforce representation, employee and contractor safety, supplier audits,
confirmed violations and USD community investment. Climate owns company-wide net-zero and SBTi
signals plus baseline year, target year, reduction and reported progress for absolute Scope 1+2
and complete Scope 3 targets; environmental actuals stay in Environment. Financial targets use
lower and upper percentage endpoints for annual revenue growth and operating margin and long-term
revenue growth, operating margin and EPS growth. A point financial target repeats the same value
in both endpoints; a one-sided bound retains one null endpoint.

Field metadata is separate from predictor values. A populated numeric value requires a reported
or company-estimated status, reporting year, qualification, confidence and source evidence with
the original block, quote, numeric text, source unit and positive conversion scale. A populated
boolean requires quoted source evidence. Deterministic validation checks the company, source
block, numeric text, scale and supported unit conversion. Invalid individual values become null
with a reason. These checks establish source support and schema consistency; they do not establish
that a disclosure is true or semantically comparable beyond the explicit field contract.

When a model identifies the correct block and numeric text but supplies a nonliteral numeric
quotation, validation may replace it with an exact original row selected by a unique field label
and number, or with bounded original context when the number occurs once in the referenced block.
Numeric grouping punctuation may be restored from that grounded row. The raw model response
remains unchanged. An absent or ambiguous number becomes null. Boolean evidence must resolve to a
literal source row. Nonpositive conversion scales are invalid for the current four-category
feature contract.
When the model returns the grounded raw number without applying an explicit source-unit conversion,
validation may derive the canonical value deterministically and records the model value and repair
in the evidence sidecar. A value that matches neither the raw number nor its canonical conversion
is rejected.

Canonical environmental units are metric tonnes of carbon-dioxide equivalent, megawatt-hours,
cubic metres, metric tonnes, hectares and percentages from zero to one hundred. Social counts are
integers. Recordable-injury rates with denominators of 200,000 hours and 1,000,000 hours occupy
separate columns; a missing or different denominator populates neither. Community investment
populates the US-dollar field only when the source explicitly establishes US dollars. No
foreign-exchange assumption is allowed. Target-year, target scope and target-type slots remain
separate from actual environmental and social performance.

Fixed extraction starts from the best saved original registered for the requested category.
Current latest-verified sources receive selection priority; an available source with unknown or
older freshness remains eligible when no current verified source exists. The exact source hash and
freshness status remain visible. Earlier free-form environmental or climate output and earlier
model responses are not projection inputs, replay sources or completion evidence. Each fixed run
uses its own versioned namespace and binds the original hash, schema, instructions, model and
endpoint. The normal path makes one extraction call; one schema/source correction may follow.
There is no additional model review. Existing validated annual financial actuals remain unchanged.

Arbitrary additional-observation names never become predictor columns. Feature projection accepts
only a validated fixed result or no result; it does not translate legacy output into the fixed
matrix. Companies without a saved original or valid fixed result still receive the same
columns, with null values and explicit missing metadata. Provider ESG and CSA scores remain labels
outside the predictor matrix.

Fixed result readers accept only versioned receipts beneath declared category namespaces,
including `data/structured_extractions/{category}/text/{provider}/{ticker}/{fingerprint}`. A readable
result must retain its fresh-original marker, exact source hash, fixed schema, source identity
checks and validated output hash. The authenticated processed-category endpoint returns the common
fixed record and field definitions for all four categories. The authenticated feature export
returns CSV values, JSON rows, field metadata or the fixed schema. ESG and CSA labels are excluded
from every predictor export. The source modal exposes original reports and fixed results without
changing the existing annual-financial result contract.

### Nonlinear-model training data

Training data uses one logical row per company and target assessment cycle. Each row has an explicit
`prediction_as_of` date. The strict policy admits observations with a supported publisher publication
date on or before that cutoff. A current-snapshot policy may use an exact public acquisition date as
a conservative upper bound on availability when the publisher date is unknown. The publisher date
remains null, the acquisition basis stays explicit, and this fallback cannot establish earlier
historical availability. Reporting year, publication date, acquisition date and processing time
remain distinct. ESG and CSA labels are independent, so a missing label for one target does not
remove the other target's eligible row.

The predictive matrix contains the fixed primitive features, a categorical industry feature and a
small declared set of ratios whose units, periods, boundaries and denominators pass deterministic
checks. Company identifiers, names, tickers, document identifiers, quotations, source URLs,
processing metadata, current or future provider scores and Green500-calculated scores remain outside
the predictive matrix. Every field belongs to financial, social, environmental, climate target,
financial target or fixed context. Missing values remain null in JSON and empty in CSV; disclosed
zero remains zero. Learned preprocessing and feature selection are fitted within training partitions.

Each immutable dataset snapshot retains aligned predictors, labels, companies, field metadata,
coverage, the feature registry and a manifest with a hash and byte count for every file. Authorized
label imports must state the target, assessment cycle, label publication date, source provenance and
authorization reference. A genuine public score without this dated authorization metadata remains
auditable and ineligible for release training. An explicitly directed prototype may carry an
unverified provider-rights marker through the label, dataset and model manifests. That marker keeps
the artifact restricted and does not establish training or distribution rights.

Historical forecasting reserves the latest fully observed cycle and validates earlier cycles with
expanding windows. Snapshot estimation holds out company groups and uses group-aware development
folds. Split assignments and seed are persisted, and EBM and CatBoost use identical folds for a
target. The final test set never participates in model selection or early stopping. Runs that cannot
form a valid split stop with an explicit blocker.

The global registry retains every approved feature even when current coverage is sparse. Each
training partition derives an active feature mask from that partition alone, using declared support,
variation and missing-reason rules. EBM and CatBoost use the same mask within a target fold. Model
selection may compare the core features with climate-target and financial-target category additions
on the same development folds. Released artifacts retain per-fold support decisions, the final
development-data mask, excluded-feature reasons and category-ablation results. A new disclosure can
enter a later model version; it never changes the input contract of an already published model.

Training uses InterpretML's Explainable Boosting Machine and CatBoost directly. Each model run binds
the dataset and split hashes, ordered feature contract, configuration, dependency versions,
validation and final-test metrics, predictions, baselines and serialized models. Saved-model
prediction rebuilds the feature row at the requested cutoff without fitting. Scenario prediction
copies the original observations, validates changes and recomputes dependent ratios. Personalized
ranking scales signed additive EBM category contributions while keeping fixed context unchanged;
unit multipliers reproduce the raw EBM prediction. A category with no active model feature has zero
model contribution and an explicit exclusion marker.

### Operations

The viewer catalog separates financial reports, environmental reports, social and employee disclosures, financial targets and climate targets. Categories come from reviewed source profiles; a discovered URL does not count as a downloaded report. SEC Company Facts and SBTi datasets remain separately labeled. Viewer sessions grant read access to registered originals only; administrative actions and model receipts require separate credentials. Acquired HTML opens as inert text, while PDFs can open inline. The single FastAPI origin serves the page, API and original-file routes behind the configured reverse proxy.

The browser uses one responsive company table with five report-category columns and a source modal. Details expose raw evidence and prior attempts. Actions enqueue work; they do not embed scraping or model calls in HTTP requests. Pause affects new work. Credentials remain server-side, acquired HTML is downloaded as inert bytes, and source requests reject private destinations. Direct HTTP, local source storage and an independently configured model API are the default deployment boundary.

The password-protected company catalog can attach official S&P Global scores from the current validated provider snapshot by exact CIK. This read-only provider snapshot is separate from PostgreSQL report coverage and model-derived metrics. ESG and Corporate Sustainability Assessment (CSA) values remain separate, zero is valid, and unavailable values remain null. Score details retain the verified provider company name, CID, industry, source URL, review status, publisher update date, CSA survey participation and acquisition time. Participation comes from company-specific source wording; a generic methodology legend cannot establish participation. The publisher update date never implies an assessment year. Invalid or absent score snapshots cannot prevent access to report coverage. The viewer refreshes score data with its existing catalog polling and displays both scores in a compact cell with a source-details dialog, without assigning additional ratings.

Collection resolves current companies in company-name order with CIK as a tie-breaker. Parallel requests can finish in a different order. Company-completion work reuses downloaded originals and prioritizes missing categories; unavailable disclosures remain explicit gaps. The viewer can filter companies with at least one downloaded original in each of its five report categories. This filter establishes source availability, not complete metric coverage or a shared reporting period.

### Explicit processing experiments

Document preparation retains the original hash and source locations. HTML blocks preserve empty cells, spans, generated row IDs and nearby headings; PDF blocks preserve page and line references. Nested tables require review and are excluded from automatic numeric selection. Selection is task-specific and never claims exhaustive coverage. Missing text layers remain visible.

The financial example contract is an experiment, separate from production metric definitions. Financial values use base currency units, annual and instant periods remain distinct, and unsupported values are null. Canonical evidence maps field paths to original source rows and program-hydrated quotations. Unique bare field names may be canonicalized; ambiguity or duplicate paths fail. Fiscal year is verified against the full-source cover and requested year, with deterministic metadata evidence. Validation checks schema, references, scale and sign support; semantic accuracy still requires independent source checks.

Model experiments preserve requests, provider responses, token counters, latency and dated pricing evidence. Current experimental calls use the existing shared Coding or Agent Plan gateway and admission; Packy and normal pay-go fallback are disabled. Historical token-priced costs remain estimates until reconciled with invoices. Subscription costs are allocations against a declared package price and quota; they do not establish marginal invoice cost. Green500 model events use `ai_function=green500`; experiment and company identifiers remain event metadata. Collection never dispatches these experiments automatically.

### Standard financial extraction

The financial schema belongs to `green500.processing.financial_schema` and has no runtime dependency on a root-level reference file. It retains the team's nine financial/context metrics and their reported, calculated, not-disclosed, not-applicable or conflicting status. Money uses base amounts with the actual currency code as its unit; employee headcount uses `employees`. Total capital expenditure never establishes green transition expenditure. Metric years must agree with the requested fiscal year.

Catalog-driven financial extraction selects each current company's latest saved SEC annual filing by filing date, using forms 10-K, 20-F or 40-F. Quarterly filings, proxy statements and earnings releases are excluded. Companies with no saved annual filing retain an explicit gap. The fiscal-year statement in the source must support the selected year. A caller-supplied source manifest allows standalone use without PostgreSQL. An annual financial attachment may carry its filing wrapper as additional year evidence. Both original hashes, the same SEC company/accession, an exact wrapper link and the wrapper cover year must agree; filenames and incidental reporting prose cannot establish that year. Result identity and source provenance retain both documents.

The shipped transport accepts an OpenAI-compatible Chat Completions base URL, API key and model, independently of any provider subscription or Granny installation. This deployment may supply an external adapter using existing Granny admission and observability. API keys remain outside prompts, receipts and logs. Provider options cannot replace the model or source messages.

Each extraction retains the original hash, selected blocks, exact requests and responses, usage and timing. Local checks validate schema, source references, years and numeric scales. A second model review of period, scope and meaning is optional and disabled by default; the request setting and whether review ran are recorded explicitly. Unproved quantities remain null, with notes limiting absence claims to the selected source evidence. Only output passing local checks and any requested model review enters `data/financial_extractions/`; intermediate candidates and failures remain inspectable. Durable per-document checkpoints allow concurrent independent work and reuse successful results by source, schema, prompt, model, endpoint and requested review setting. Unknown prior provider outcomes require explicit reconciliation before retry. No model work starts merely because a report was downloaded.

The viewer labels financial processing as Not started, Processing, Extracted or Retry. Extracted results require a successful receipt, exact result-file hash, valid financial schema, local source checks and a matching registered company annual-filing hash. Model judgments are required only when model review was requested; an explicit skipped review never counts as a completed model review. Legacy reviewed results retain their complete model judgments. Benchmark outputs and unverified candidates are excluded. The latest attempt and last valid result are distinct; an earlier result is labeled when a newer attempt needs a retry. The existing password session protects result details and JSON downloads. The financial source modal switches between original reports and AI results, including all nine metrics, disclosure statuses, fiscal years and exact source references. Viewer responses omit provider credentials, account identities, requests and local storage paths.

### Source freshness and current evidence

Source availability and verified freshness are separate. `report_sources.review` stores a bounded review of an exact document hash, publisher evidence URLs, check time, expiry, report family, scope and independently supported dates. Old rows begin unreviewed. A changed file, invalid review, future check time or expired check cannot retain a verified-latest claim. Review windows cannot exceed thirty days. Download time, URL years, HTTP modification time and target years do not establish report publication or reporting period.

The catalog exposes downloaded counts and per-category latest-verified, newer-available and unknown counts. A newer unavailable or blocked candidate does not erase the earlier original. Categories refer to available disclosure sources; category assessments separately record scope and whether a target is current, achieved, retired or unknown. A source review applies only to its declared categories. SEC annual/quarterly checks establish financial-filing freshness; proxy checks establish employee-disclosure freshness when content was confirmed. They cannot establish freshness of environmental or climate categories attached to the same filing. Target categories require a current target assessment before contributing to latest-verified counts.

SEC collection retains validated current-submissions JSON and updates reviews for both reused and newly acquired filings, preserving curated classifications. Detected Q4 download widgets are read through their public ContentAsset listing, with asset dates kept separate from report periods. Structured-list failures remain visible before static-link fallback. Publisher listings must be monitored to discover new URLs; refreshing an old PDF alone does not establish that it is the latest edition.
