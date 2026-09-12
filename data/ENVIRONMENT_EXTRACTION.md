# Environmental extraction: site audit and Airbnb pilot

Inspected 12 September 2026. This is an environmental source audit and a tested
Airbnb extraction pilot, not a completed extraction for all 500 companies.

## What the source library provides

[Green500](https://green500.hongyuanzhang.com/) is a source catalog with stored
originals, company identities, collection status, content types, document IDs,
SHA-256 hashes, and source URLs. Its frontend exposes these read-only routes:

- `/api/catalog`: company inventory and coverage.
- `/api/catalog/{cik}`: company sources and provider datasets.
- `/api/catalog/files/{document_id}/download`: stored original.
- `/api/catalog/files/{document_id}/text`: decoded preview with completeness and warnings.

These were read using the existing authenticated browser session. A future
collector needs authorized access; do not embed browser credentials in code.
The preview is useful for discovery, but it may be shortened and is not a
substitute for page-aware extraction from the original.

The saved [catalog snapshot](reference/environment_catalog_snapshot.json) contains
500 companies and 1,785 available files across all categories. Counts changed
during inspection, so this saved snapshot is the reference for the following:

| Environmental coverage | Companies |
| --- | ---: |
| At least one available environmental source | 204 |
| Failed source collection and no available environmental source | 47 |
| No environmental coverage recorded | 249 |

There are 210 available environmental source entries. Six of the 204 covered
companies also have failed entries. These are collection counts, not evidence
that each company has extractable or current environmental metrics. Deduplicate
by file hash before parsing because reports can serve several categories.

## Library decision

| Library | Relevant strengths | Role in this project |
| --- | --- | --- |
| [Docling](https://github.com/docling-project/docling) | Layout, reading order, table structure, OCR, structured JSON and local execution | Recommended candidate for complex report conversion; benchmark before making it the universal default |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | Native text, tables, character coordinates, bounding boxes and visual debugging | Implemented first pass and evidence inspection for digital PDFs |
| [PyMuPDF / PyMuPDF4LLM](https://pymupdf.readthedocs.io/en/latest/about.html) | Rendering, text and table extraction; advanced layout support through PyMuPDF4LLM | Alternative worth benchmarking; licensing is AGPL or commercial |

Docling is the strongest fit on documented features for varied sustainability
report layouts. That is a suitability assessment, not an accuracy benchmark.
Its code is MIT licensed; model licenses are separate. pdfplumber explicitly
works best on machine-generated PDFs and does not supply OCR. The Airbnb pilot
needed no OCR, so only pdfplumber was installed. No Docling or PyMuPDF performance
claim has been tested on this corpus.

## Tested pilot

[Airbnb's catalog entry](report_examples/airbnb/source_catalog.json) points to a
51-page 2023 sustainability update, mainly reporting 2022 environmental data.
The downloaded 13,522,953-byte original matches its catalog SHA-256. It is kept
locally in `data/objects/` and excluded from Git.

The `environment` section of [Airbnb results](report_examples/airbnb/results.json) records
13 GHG metrics, a qualified electricity matching claim, evidence pages, source
hash, reporting period, organizational boundary, assurance and remaining gaps.
The 2022 summary contains:

| Metric | Value |
| --- | ---: |
| Scope 1, tCO2e | 1,375 |
| Scope 2 location-based, tCO2e | 5,384 |
| Scope 2 market-based, tCO2e | 1 |
| Scope 3, reported categories, tCO2e | 326,764 |
| Combined using market-based Scope 2, tCO2e | 328,140 |
| Scope 3 intensity, tCO2e per USD million gross profit | 47 |

Source: [Airbnb Sustainability and Community Update](https://airbnb2020ipo.q4web.com/files/doc_downloads/governance_doc_updated/2023/Airbnb-SustainabilityandCommunityUpdate-2023-111623.pdf),
PDF page 12, printed page 9, with category detail on PDF pages 41-43.

The main table and category tables were visually checked. Both the Scope 3
category sum and the combined emissions sum reconcile exactly. The report
identifies limited assurance for the marked metrics, with PwC's statement in
Appendix B; this does not apply to every environmental claim.

The extraction retained table headers and 2022 values correctly. Plain text
interleaves the side-by-side appendix pages, demonstrating a real need for
layout review. The generic inspector found 28 candidate pages in 51 pages;
candidate status is not semantic approval. It does not yet detect all layout
failures automatically.

## Extraction approach

1. Inventory available environmental originals and deduplicate by SHA-256.
   Preserve failed and missing collection states. Check report dates against
   the reporting hub before asserting that a report is the latest available.
2. Prefer structured company data tables or spreadsheets when present. Inspect
   MIME type and bytes; route HTML, spreadsheets and PDFs to their own parsers.
3. For PDFs, extract native text and locate data appendices, GRI/SASB indices,
   units, reporting years and assurance statements. Retain adjacent pages and
   footnotes; keyword matches are only candidate selection.
4. Use pdfplumber tables when they preserve headers and row relationships.
   Route scans, scrambled text, complex tables and chart-only values to
   Docling/OCR or visual review. Benchmark these routes on a stratified sample
   of reports before scaling to the full inventory.
5. Interpret only candidate evidence into typed observations. Each needs a
   value or explicit missing state, unit, period, boundary, accounting method,
   original label, page/table location, source hash and review status. A model
   may propose mappings; parsing text successfully is not validation.
6. Validate units, year columns, totals, intensity denominators, assurance
   scope and whether the statement is an actual, estimate or target. Keep
   both Scope 2 variants. Never add both to a total, subtract offsets from a
   gross inventory, or infer omitted Scope 3 categories as zero.
7. Publish reviewed observations separately from candidate extraction. Cache
   parser outputs by source hash, parser version and configuration so repeated
   collection does not repeat expensive interpretation.

For Airbnb, preserve 2022 environmental data separately from FY2025 financial
data. Its inventory includes only specified corporate Scope 3 categories.
Electricity matching does not mean all energy use is renewable. Waste-treatment
emissions are not waste tonnage. Changes in 2022 methods and boundaries limit
trend comparisons. Targets belong in a separate record, and the report's
historical SBTi statement does not resolve today's unmatched catalog status.

Water, physical waste quantities, biodiversity, pollution and total energy
remain `not_extracted` in the pilot, not zero or proven absent. The topic routing
uses the six environmental topics in the existing materiality reference.

## Reproduce the first pass

Requires Python 3.11 or later. The tested environment uses Python 3.12 and
pdfplumber 0.11.10.

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-environment.txt
.venv/bin/python scripts/inspect_environment_pdf.py \
  data/objects/087cf27af5a5c13262961da728c094a4e1027c85f4e3dce5183880f84251e5ad.pdf \
  --output data/exports/airbnb-environment-evidence.json
```

The inspector works on any local PDF and emits page-level candidate text and
tables. The normalized Airbnb demo additionally incorporates manual semantic
and visual review; the generic inspector does not recreate that review.
Originals, full extracted text and page images stay outside Git. The source
inventory and reviewed demo are tracked artifacts. No server data was changed.
