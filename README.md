# Green500

A data-driven framework to quantify and compare the sustainability
of companies in the S&P 500.

Built for ETHack 2026 — Challenge #1.

## Challenge

Build a data-driven framework to quantify and compare the sustainability
of companies in the S&P 500.

How you approach the problem is up to you. Define what sustainability
means, identify and justify the most relevant indicators, source the
necessary data, and develop a methodology to score, rank, or compare
companies. Your solution may incorporate environmental, financial,
operational, or other dimensions you consider relevant.

## Dataset Requirement

We expect you to choose your own dataset, justify your choice, and show
how it is suitable for the challenge.

## Bonus Question

Tomorrow, the world commits to reaching net-zero emissions as fast as
possible. You manage a $1 billion investment fund. How do you allocate
your portfolio under this new scenario, and why?

## Project Status

The backend reads PDF or HTML reports and uses a model to extract structured
observations, checks the response against a JSON schema and source evidence, then
saves company JSON for the results builder. Scores use extracted observations,
never materiality ratings. The existing example records retain their original
reviewed-mapping provenance; they have not been re-extracted by the model yet.

## Analysis code and prompts

All category instructions are adjacent in [analysis_prompts.py](analysis_prompts.py):
**environmental**, **social**, and **financial**. The response contracts are Pydantic models:
[environmental.py](environmental.py) (`VERDEXEnvironmentalData`),
[social.py](social.py) (`VERDEXSocialData`), and shared metric/evidence types in
[extraction_models.py](extraction_models.py).
[analysis_schema.py](analysis_schema.py) generates the API JSON schemas directly
from these models; responses are parsed with `model_validate()` before evidence
validation. Environmental topics also stay together
inside the environmental prompt.

1. [scripts/read_environment_report.py](scripts/read_environment_report.py) reads all PDF pages or HTML blocks, preserving locators.
2. [scripts/analyze_report.py](scripts/analyze_report.py) sends chunked evidence and the selected prompt to the OpenAI Responses API with strict JSON Schema output.
3. It validates company identity, schema, source quotations, locators and numeric conversions. Conflicting observations remain missing with their candidate values preserved. These checks do not prove semantic correctness or independent assurance.
4. It saves the existing `company` + `environment` / `social` shape. Financial observations are converted and validated against [financial.py](financial.py), with evidence retained separately.
5. [scripts/build_results.py](scripts/build_results.py) combines company records and invokes [scripts/score_environment.py](scripts/score_environment.py). The scoring methodology is documented in [data/ENVIRONMENT_SCORING.md](data/ENVIRONMENT_SCORING.md).

Install dependencies, then export `OPENAI_API_KEY` and `OPENAI_MODEL` in your shell.
Use a model available to your account that supports Structured Outputs. No key is
stored in the repository; `.env.example` lists configuration names, and `.env`
files are not loaded automatically.

```sh
uv pip install --python .venv/bin/python -r requirements-environment.txt
.venv/bin/python scripts/analyze_report.py path/to/report.pdf \
  --category environmental --company "Example Company" --ticker EXAMPLE \
  --source-url https://example.com/report.pdf \
  --output data/report_examples/example/results.json --rebuild-results
```

Use an existing ticker from the company reference list when rebuilding the root
results. Use `--year 2025` to select a period explicitly. The same command accepts
HTML and `--category social` or `--category financial`. The convenience command
`scripts/extract_environment_results.py` defaults to environmental analysis.
No company-specific extraction profile is required.

Model calls are required for extraction. Missing credentials, refusals, incomplete
responses and failed validation produce errors before replacing the company file.
The text reader does not OCR scanned PDFs; documents without readable text fail
explicitly. Partially readable PDFs can have missing observations. Large reports
are processed in multiple calls, including complete HTML text for div-based filings.
Changed/ambiguous boundaries require interpretation; machine validation alone
cannot certify that the model picked the right table column or reporting scope.

For a dry run that only prepares messages and the schema, use
`scripts/prepare_analysis.py`. The historical rule-based extractor remains under
`scripts/replay_environment_mappings.py` solely to reproduce the original examples.
It is not the default analysis path.

The API integration follows the [official Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

## Data

- [Company materiality profiles](data/reference/company_materiality.csv)
- [Topic definitions and example metrics](data/reference/topic_definitions.csv)
- [Source references](data/reference/sources.csv)
- [Original workbook notes](data/reference/workbook_readme.csv)
- [Data documentation and original Numbers file](data/README.md)

## Judging Criteria

The following summarizes every criterion on slide 10 of the
[event slides](https://www.canva.com/design/DAHUliIU37Q/KcWRSSQ06m-dqa--jVhhhA/view#10).

| Criterion | What the judges assess |
| --- | --- |
| Impact | The potential societal benefit of the solution and the people who would benefit. |
| Innovation | Originality or a new perspective on an existing problem, demonstrating thoughtful work. |
| Technical Execution | Functioning features and implementation quality, assessed within the two-day time constraint. |
| Feasibility | Whether the project can continue beyond the event, accounting realistically for costs, available data, adoption, and constraints. |
| Presentation | An understandable problem statement, a functioning demonstration, transparent limitations, and completion within the allotted time. |

The slides do not specify criterion weights or a numerical scoring scale.

## Submission Requirements

- Required: PowerPoint presentation in `.ppt` format.
- Presentation duration: 3 minutes.
- Code, if available: GitHub link.
- Website or web app, if available: link.
- Documentation, if available: link.

Presentations submitted in another format will not be integrated into
the final presentation deck.

Submit through the [submission portal](https://www.tinyurl.com/ETHack2026-Submission).
See slides 11 and 15 for the submission instructions.

## Timeline

**Hacking: 12 September 2026 at 12:00 to 13 September 2026 at 11:59.**
This is the hacking window on slide 9; the slides do not separately label
a submission deadline.

Schedule from slide 8:

| Date | Time | Activity |
| --- | --- | --- |
| 12 September 2026 | 10:00–10:30 | Registration |
| 12 September 2026 | 10:30–12:00 | Opening ceremony |
| 12 September 2026 | 12:00–13:30 | Networking lunch |
| 12 September 2026 | 18:00–20:00 | Dinner |
| 12 September 2026 | From 20:00 | Night hacking |
| 13 September 2026 | 08:00–09:00 | Breakfast |
| 13 September 2026 | 12:00–13:00 | Celebration lunch |
| 13 September 2026 | 13:00–14:30 | Judging and networking |
| 13 September 2026 | 14:30–15:30 | Closing ceremony |

## Team Requirements

Slide 14 specifies teams of **4–5 members**. Existing four-person teams may
recruit a fifth member. Mark the **Done** column in the
[team sheet](https://www.tinyurl.com/ETHack2026-Teams) once the team is formed.
Participants without a team can meet teammates during lunch.

## Judging Procedure and Judges

Slide 13 specifies a randomized judging order; follow the volunteers'
directions.

| Judge | Affiliation |
| --- | --- |
| Bianca Morrone | ETH Zurich |
| Jenestin Anthonipillai | ETH Zurich |
| Sherryl Manalo | Julius Bär |
| Maggie Wang | MIT |
| Luca Ferrari | ETH Zurich |

## Prizes

From slide 12:

| Place | Prize |
| --- | --- |
| First | CHF 2,000 |
| Second | CHF 1,000 |
| Third | CHF 500 |

## Venue

From slide 7:

- **ML:** opening and closing ceremonies, and the partner booth.
- **CHN:** hacking, meals, and overnight access.
- Consult the slide's map for weekend entrances, indicated by arrows.

## Organizers and Contact

The organizing team listed on slide 2: Luca Ferrari, Angela Ng,
Michael Wenger, Arnout Devos, Mohid Fayaz Mir, and Jenestin Anthonipillai.

See [slide 18](https://www.canva.com/design/DAHUliIU37Q/KcWRSSQ06m-dqa--jVhhhA/view#18)
for the event's contact information and
[slide 6](https://www.canva.com/design/DAHUliIU37Q/KcWRSSQ06m-dqa--jVhhhA/view#6)
for sponsors and partners.

## Links

- [Challenge and event slides](https://www.canva.com/design/DAHUliIU37Q/KcWRSSQ06m-dqa--jVhhhA/view)
- [Submission portal](https://www.tinyurl.com/ETHack2026-Submission)
- [Team registration](https://www.tinyurl.com/ETHack2026-Teams)

## Standalone Green500 data infrastructure

Green500 collects the S&P 500 company list and company environmental reports, extracts source-linked quantities through a configurable model API, and displays progress in one company table.

Scrapy handles public HTTP acquisition. PostgreSQL stores company records, immutable membership snapshots, documents, tasks, model attempts and metrics. Original documents, parsed source lines and model responses are stored under content hashes in an external local data directory. The project does not import granny_data or require its Ops, proxy or browser infrastructure.

The Ops page shows collection, parsing and extraction separately. Expanding a company row reveals original documents, model input and output, cited metrics, errors and retry controls. A source-linked metric has passed automated citation checks; it is not an independent audit of the company's claim. Missing years, units or boundaries remain visible for review.

### Run

See [the setup and operation instructions](USAGE.md). Python dependencies are locked with uv. The browser script is plain TypeScript, compiled to a checked-in JavaScript file; Node is only needed when editing that script.

The prepared score-model pipeline is documented in [green500/ml/README.md](green500/ml/README.md). It audits the current extraction coverage, builds dated company-cycle datasets, and supports EBM and CatBoost training after authorized ESG or CSA labels are imported. Incomplete report processing and missing label dates remain explicit blockers; the pipeline does not start report extraction.

### Evidence and limitations

The initial constituent source is Wikipedia's public S&P 500 table. Each snapshot retains its source bytes, revision and observation time. The source is secondary; current official membership is not independently certified. Security rows and company identifiers are separate, so multiple share classes do not create duplicate companies.

Report and feed URLs are supplied explicitly. A directory collection follows observed PDF links and same-host links once, within the requested page allowance. Feed entry content remains available for extraction even without a linked article. Unread links remain in the task result. The bundled [source-discovery skill](.codex/skills/green500-source-discovery/SKILL.md) helps an agent prepare evidence-bound source candidates. Browser and proxy capabilities come from the operator's environment and are not application dependencies.

PDF processing preserves page and line positions from readable text. Fully image-based PDFs use a bounded local Tesseract OCR fallback when it is installed. Encrypted files and complex tables can still require review. The application never labels omitted pages as complete. Model-generated numbers and units must occur in their cited source blocks. Unit normalization covers a small explicit vocabulary; unsupported units and unsupported context require review.

Local source storage must be backed up together with PostgreSQL. Placement on an existing PostgreSQL server does not add this application's database to that server's backup policy. Credentials stay in the ignored local environment file. The Ops server binds to loopback by default and requires a configured token when exposed elsewhere.

### Origin

The acquisition receipt, bounded PDF parsing, PostgreSQL transaction and immutable source-hash patterns were adapted from granny_data. [MIGRATION.md](MIGRATION.md) records their source files and boundaries. Green500 keeps its own data model, direct model client and one-table interface.
