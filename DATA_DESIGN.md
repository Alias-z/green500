<!-- updated: 2026-09-12 12:00 UTC | linear: DAT-78 -->
# Green500 data design

Guide: [scope](#scope), [collection](#collection), [storage](#storage), [monitoring](#monitoring).

## Scope

Green500 delivers the latest available financial, environmental and climate-target data with evidence. Historical backfill is deferred. The analysis team defines indices, predictions and investment decisions. Missing information remains missing; it never becomes zero.

## Collection

Company list → source checks → immutable originals → normalized observations → company table and analysis exports.

Scrapy fetches stable public endpoints: the constituent list, SEC Company Facts and SBTi workbooks. Structured JSON and spreadsheets use deterministic parsers. A discovery agent locates company reporting hubs, PDFs and feeds; confirmed URLs become recurring Scrapy sources. Discovery runs again when a source disappears or changes structure.

Public company reports fill environmental and social gaps. A direct large language model (LLM) API extracts candidate values from saved text. Exact quotations, units, periods and a separate meaning review determine which candidates enter the analysis view. Incomplete parsing and rejected values remain visible. Browser automation is reserved for sites that require it; no proxy service is required by the initial deployment.

## Storage

PostgreSQL stores companies, source versions, observations, collection checks, schedules and task attempts. The SEC Central Index Key (CIK) identifies registrants. Provider identifiers and matching methods accompany cross-source data. Unique-name SBTi matches remain provisional until identity is confirmed.

Original files and model receipts live in `data/objects/`, addressed by their SHA-256 content hash. Human-readable examples live in `data/report_examples/`; `data/DATA_SOURCES.md` explains source categories. PostgreSQL holds file references, not PDF bodies. Local files and the database require separate backups.

Each observation retains its value, unit, statistical period, publication date where available, acquisition time, provider record and source file. Annual, quarterly and point-in-time financial values remain separate. Emissions retain scope, accounting method and organizational boundary. Current means latest available reporting period, not the acquisition year.

Unchanged records are deduplicated. Changed records are retained from now onward. Failed refreshes preserve earlier successful values and expose a failed check. Structured imports have their own observations view; they do not create model-call records.

## Monitoring

The existing worker queues due checks: daily constituents and financial data, weekly SBTi targets, and configured company reporting sources. Requests are paced, retries bounded and task outcomes durable. Refreshing known URLs collects new versions; interpretation is an explicit action so recurring downloads do not create uncontrolled model charges.

One company table shows actual values, reporting dates, source-check outcomes and extraction status. Expanded rows expose evidence and errors. Refresh, retry and pause use the same task queue. No separate orchestration platform is required.
