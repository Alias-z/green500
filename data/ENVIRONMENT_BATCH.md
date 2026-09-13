# Environmental batch results

Ten additional companies processed from eight PDFs and two HTML files. All observations retain source hashes and PDF-page or HTML-section references. This is a reviewed extraction, not a universal automatic ESG scoring system.

| Company | Reporting year | Format | Observations | Results |
| --- | ---: | --- | ---: | --- |
| 3M (MMM) | 2025 | PDF | 5 | [JSON](report_examples/3m/results.json) |
| Abbott Laboratories (ABT) | 2025 | PDF | 9 | [JSON](report_examples/abbott/results.json) |
| AbbVie (ABBV) | 2025 | PDF | 5 | [JSON](report_examples/abbvie/results.json) |
| Accenture (ACN) | 2025 | PDF | 8 | [JSON](report_examples/accenture/results.json) |
| Adobe Inc. (ADBE) | 2024 | PDF | 4 | [JSON](report_examples/adobe/results.json) |
| Advanced Micro Devices (AMD) | 2025 | HTML | 3 | [JSON](report_examples/amd/results.json) |
| AES Corporation (AES) | 2025 | PDF | 11 | [JSON](report_examples/aes/results.json) |
| Aflac (AFL) | 2023 | PDF | 4 | [JSON](report_examples/aflac/results.json) |
| Agilent Technologies (A) | 2025 | HTML | 1 | [JSON](report_examples/agilent/results.json) |
| Air Products (APD) | 2025 | PDF | 12 | [JSON](report_examples/air_products/results.json) |

62 observations: nine companies have measured or estimated performance values; Agilent contributes one company target from its 10-K. Its absolute performance metrics remain unextracted. This batch does not add financial sample values.

## What required review

- Aflac: its 2024 report presents 2023 emissions, including an explicit zero for market-based Scope 2. Investment emissions are still unreported.
- Adobe: the 2025 CDP response reports a fiscal year ending November 29, 2024. Purchased-goods emissions are retained as category 1, not total Scope 3.
- AMD: operational emissions are separately labelled from supplier estimates that may be revised.
- AES: the equity-share assurance appendix differs from the year-end portfolio snapshot. The qualified assurance opinion and inventory exclusions are preserved.
- Abbott: figures in thousands of tonnes and megaliters are scaled to tonnes and cubic meters. Rounded components differ from the reported combined emissions by 1,000 tonnes; that rounding difference is retained and checked.
- Air Products: native PDF text drops some decimal points. The rendered table confirms 76.0 TWh, 808.0 million cubic meters withdrawn and 90.2 million cubic meters consumed. Those conversions have explicit reviewed rules.
- 3M: selected reduction and efficiency claims are progress metrics, not absolute inventories. Its report points to a separate nonfinancial-metrics supplement for more detail.

## Reproduce

```sh
uv pip install --python .venv/bin/python -r requirements-environment.txt
.venv/bin/python scripts/read_environment_report.py path/to/report.html --output data/exports/report-evidence.json
.venv/bin/python scripts/replay_environment_mappings.py data/extraction/environment_profiles.json
python3 scripts/build_results.py
.venv/bin/python -m unittest discover -s scripts -p "test_*.py"
```

Originals must first be downloaded from each profile’s source URL or the catalog file download endpoint and stored under `data/objects/<sha256>.pdf` or `.html`. Originals and intermediate evidence are excluded from Git. The reviewed profiles pin the source hash and locator; a changed source or missing/ambiguous match fails instead of reusing stale values. New reports require new reviewed mappings.

## Website data flow

`data/report_examples/*/results.json` + `data/reference/company_materiality.csv` → `python3 scripts/build_results.py` → root `results.json` → browser fetch → dashboard and portfolio views.

The root JSON holds 503 securities and includes the 11 company environmental records (Airbnb plus this batch). The CSV materiality fields remain separate from extracted observations. The existing scoring formula still uses topic relevance ratings; it has not been replaced with a performance-based scoring method.

The Vite development server reads root `results.json` for each request. Production builds emit the same JSON as a separate asset. Company arrays and random example metrics have been removed from frontend source. Loading failures show an error and retry control rather than hardcoded fallback data. Refresh the page after updating root results; rebuild/redeploy for production updates.

Airbnb financial fields are explicitly marked as unverified example values from the requested JSON format. The new ten company files contain no financial demo data.
