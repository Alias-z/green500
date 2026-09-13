# Green500 data

- [../results.json](../results.json): combined company data loaded by the website. Regenerate with `python3 scripts/build_results.py` after updating individual company results.
- [ENVIRONMENT_BATCH.md](ENVIRONMENT_BATCH.md): extraction results for 10 additional companies from PDF and HTML sources, with reproduction steps and known gaps.

- `DATA_SOURCES.md`: source categories, acquisition methods and initial priorities.
- [ENVIRONMENT_EXTRACTION.md](ENVIRONMENT_EXTRACTION.md): live source-catalog audit, PDF library comparison, extraction workflow and tested Airbnb environmental JSON pilot.
- `report_examples/microsoft/`: one human-readable example in Markdown and HTML.
- [report_examples/airbnb/results.json](report_examples/airbnb/results.json): consolidated Airbnb JSON with unverified FY2025 sample financial values, non-GAAP metrics, workforce and geography, plus extracted environmental data in an `environment` section with its own 2022 reporting period and source evidence. Financial demo values are preserved as supplied and have not been independently verified. Monetary values are in USD; percentage fields use whole percentages (for example, `38` means 38%).
- `raw/`: the supplied materiality reference workbook, retained in Git.
- `reference/`: CSV exports of the materiality workbook's four sheets.
- `objects/`: immutable originals and model receipts, named by SHA-256; excluded from Git.
- `exports/`: optional generated delivery files; excluded from Git.

PostgreSQL remains the authority for structured observations and collection status. Raw files must be backed up together with their database references. Source files preserve provider terms; storing a source does not grant redistribution rights.

## Materiality reference workbook

The original `GREEN500_DATA.numbers` workbook and CSV exports of all four
sheets are stored here. The workbook identifies itself as the **VERDEX —
S&P 500 Company Materiality Dataset**, dated **2026-09-12**. Its original
names, descriptions, and method labels are preserved.

## Files

| File | Source sheet | Contents |
| --- | --- | --- |
| [raw/GREEN500_DATA.numbers](raw/GREEN500_DATA.numbers) | Entire workbook | Unmodified original file |
| [reference/company_materiality.csv](reference/company_materiality.csv) | S&P 500 Companies | 503 securities, 22 columns, 11 GICS sectors |
| [reference/workbook_readme.csv](reference/workbook_readme.csv) | README | Original title and 10 metadata entries in two columns |
| [reference/topic_definitions.csv](reference/topic_definitions.csv) | Topic Definitions | 15 topics with definitions and example metrics |
| [reference/sources.csv](reference/sources.csv) | Sources | Three source references, including URLs and their stated uses |

## Meaning and limitations

The 15 topic columns indicate **materiality**, meaning the relevance of a
sustainability topic to a company. They are not measurements of company
performance or final sustainability scores.

The workbook defines the scale as:

- **0:** generally not material
- **1:** low
- **2:** medium
- **3:** high

Its stated method starts with a VERDEX sector baseline and applies
sub-industry overrides. Company-specific evidence should override an
industry prior where appropriate. The workbook explicitly states that
these are **not official S&P, MSCI, GRI, or SASB ratings**.

The workbook's intended pipeline is:

> Company → GICS → Materiality topics → Metrics → Normalization → 6 VERDEX pillars → VERDEX score

The six pillars are referenced but not defined in this workbook. The CSV
export does not add definitions or calculate sustainability scores.

The workbook describes 503 listed securities, including multiple share
classes for some companies. Do not equate the record count with the number
of distinct corporate issuers.

## Provenance

The workbook attributes its constituent list and GICS fields to a public
GitHub snapshot sourced from Wikipedia. It also cites the S&P Dow Jones
Indices GICS overview and the GRI Standards as context. Exact source names,
URLs, and stated uses are preserved in [sources.csv](reference/sources.csv).
The snapshot date and provenance are workbook claims; this conversion
does not independently verify index membership or the materiality method.

## Export details

- Converted with `numbers-parser` version `4.19.0`.
- CSV encoding: UTF-8; delimiter: comma; line endings: LF.
- Original row order, column names, and cell values are retained within each export.
- Entirely blank rows and trailing blank columns are removed. Empty cells
  within retained rows and columns remain empty.
- Whole-number numeric values are written as integers, such as `3` instead
  of `3.0`; fractional values are retained.
- CSV stores cell values, without Numbers styling or workbook behavior.
- These exports consolidate the four `*-Table 1.csv` files previously
  uploaded at the repository root. Their content matches after removing
  empty rows and trailing empty columns.
- `workbook_readme.csv` begins with the workbook title and an empty second
  cell, followed by metadata key/value rows. It has no added header row.
  The other three CSV files retain their source header rows.

Validation confirmed all exported cells round-trip through a CSV reader,
503 unique tickers, 15 matching topic definitions, and topic ratings within
0–3. The copied original matches the supplied file byte-for-byte.

Original workbook SHA-256:

```text
bf072431af2064f4e2def3157098d22601c9874f81ef01fc5b7a3a074055a019
```

Environmental score calculations and limitations: [ENVIRONMENT_SCORING.md](ENVIRONMENT_SCORING.md).
