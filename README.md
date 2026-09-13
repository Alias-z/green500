# Green500

Green500 collects the S&P 500 company list and company environmental reports, extracts source-linked quantities through a configurable model API, and displays progress in one company table.

Scrapy handles public HTTP acquisition. PostgreSQL stores company records, immutable membership snapshots, documents, tasks, model attempts and metrics. Original documents, parsed source lines and model responses are stored under content hashes in an external local data directory. The project does not import granny_data or require its Ops, proxy or browser infrastructure.

The Ops page shows collection, parsing and extraction separately. Expanding a company row reveals original documents, model input and output, cited metrics, errors and retry controls. A source-linked metric has passed automated citation checks; it is not an independent audit of the company's claim. Missing years, units or boundaries remain visible for review.

## Run

See [the setup and operation instructions](USAGE.md). Python dependencies are locked with uv. The browser script is plain TypeScript, compiled to a checked-in JavaScript file; Node is only needed when editing that script.

The prepared score-model pipeline is documented in [green500/ml/README.md](green500/ml/README.md). It audits the current extraction coverage, builds dated company-cycle datasets, and supports EBM and CatBoost training after authorized ESG or CSA labels are imported. Incomplete report processing and missing label dates remain explicit blockers; the pipeline does not start report extraction.

## Evidence and limitations

The initial constituent source is Wikipedia's public S&P 500 table. Each snapshot retains its source bytes, revision and observation time. The source is secondary; current official membership is not independently certified. Security rows and company identifiers are separate, so multiple share classes do not create duplicate companies.

Report and feed URLs are supplied explicitly. A directory collection follows observed PDF links and same-host links once, within the requested page allowance. Feed entry content remains available for extraction even without a linked article. Unread links remain in the task result. The bundled [source-discovery skill](.codex/skills/green500-source-discovery/SKILL.md) helps an agent prepare evidence-bound source candidates. Browser and proxy capabilities come from the operator's environment and are not application dependencies.

PDF processing preserves page and line positions from readable text. Fully image-based PDFs use a bounded local Tesseract OCR fallback when it is installed. Encrypted files and complex tables can still require review. The application never labels omitted pages as complete. Model-generated numbers and units must occur in their cited source blocks. Unit normalization covers a small explicit vocabulary; unsupported units and unsupported context require review.

Local source storage must be backed up together with PostgreSQL. Placement on an existing PostgreSQL server does not add this application's database to that server's backup policy. Credentials stay in the ignored local environment file. The Ops server binds to loopback by default and requires a configured token when exposed elsewhere.

## Origin

The acquisition receipt, bounded PDF parsing, PostgreSQL transaction and immutable source-hash patterns were adapted from granny_data. [MIGRATION.md](MIGRATION.md) records their source files and boundaries. Green500 keeps its own data model, direct model client and one-table interface.
