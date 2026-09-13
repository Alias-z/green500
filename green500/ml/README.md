# Green500 model pipeline

This package builds dated company feature rows, trains Explainable Boosting Machine (EBM) and
CatBoost regressors when authorized labels support valid splits, and loads saved model runs for
prediction. It does not download reports or start extraction work.

The current registry contains 49 report-derived primitive fields, industry, and four derived
ratios. Every predictive field belongs to `financial`, `social`, `environmental`,
`climate_target`, `financial_target`, or `fixed_context`. Company identifiers, source references,
scores, and extraction evidence remain available for joins and audits and are excluded from model
inputs.

Run all commands from the repository root:

```bash
uv sync --extra ml

uv run --extra ml python -m green500.ml audit \
  --config config/ml.yaml \
  --labels data/reference/ml_labels_template.csv \
  --availability-policy public-document-acquisition-fallback \
  --repair-output data/ml/audit/source-metadata-repairs.json

uv run --extra ml python -m green500.ml build-dataset \
  --config config/ml.yaml \
  --labels data/reference/ml_labels_template.csv \
  --availability-policy public-document-acquisition-fallback \
  --output-dir data/ml/datasets

uv run --extra ml python -m green500.ml train \
  --config config/ml.yaml \
  --dataset-dir data/ml/datasets/snapshots/DATASET_ID \
  --output-dir data/ml/runs

uv run --extra ml python -m green500.ml predict 0000320193 \
  --prediction-as-of 2026-09-13 \
  --model-run data/ml/runs/RUN_ID

uv run --extra ml python -m green500.ml scenario 0000320193 \
  --prediction-as-of 2026-09-13 \
  --model-run data/ml/runs/RUN_ID \
  --overrides '{"env_scope_1_tco2e": 1000000}'

uv run --extra ml python -m green500.ml rank \
  --prediction-as-of 2026-09-13 \
  --target esg \
  --model-run data/ml/runs/RUN_ID \
  --weights '{"environmental": 1.5, "social": 1.25}'
```

The audit prints compact coverage, missingness, and provisional feature-eligibility summaries.
`--repair-output` writes the full read-only list of source documents and fields that need a
publication date or a specific null reason. The printed JSON contains only its path and counts, so
the audit and one-command pipeline remain small enough for routine use. The repair file never
changes source documents or extraction results.

The default `publisher-publication-date` policy accepts only a publisher publication date on or
before `prediction_as_of`. Delivery snapshots can explicitly use
`public-document-acquisition-fallback`. When a publisher date is absent, this policy uses the date
Green500 acquired the exact hash-bound public document as a conservative `availability_date`.
Metadata keeps `publication_date` null and records `availability_basis` as
`public_document_acquisition_date`.

`--overrides` and `--weights` also accept `@path/to/input.json`. Scenario overrides accept finite
primitive values or JSON `null`. Derived ratios are recomputed from copied source observations.
The original feature row and its evidence remain unchanged. Scenario differences describe model
sensitivity; they do not guarantee a real score change.

The rank command uses the selected EBM for the requested target. It sums signed local terms from
`eval_terms()` according to `term_features_`. The artifact must use `interactions=0` and an identity
link. A multiplier of `1.0` for all five categories reproduces the raw EBM prediction. Multipliers
range from 0 to 3 and do not need to sum to a fixed total. Industry and other fixed context remain
unweighted. A category with no active feature has a signed contribution of zero and reports
`category_excluded_from_model: true`, along with its saved field-level exclusion reasons and
training-partition support. Output is labeled **Personalized Green500 index**; it is neither an
official ESG/CSA score nor an independently measured environmental impact.

The one-command pipeline audits inputs, writes a content-addressed dataset, and runs training only
when authorized dated labels and valid splits are available:

```bash
uv run --extra ml python -m green500.ml pipeline \
  --config config/ml.yaml \
  --labels data/reference/ml_labels_template.csv \
  --availability-policy public-document-acquisition-fallback \
  --output-dir data/ml
```

When labels or split requirements are missing, the pipeline returns `status: blocked` and creates
no model. It never substitutes synthetic labels or starts report extraction. Synthetic data is
used only by automated tests.

Saved run manifests bind the dataset, complete feature registry order, target-specific active
feature masks, exclusions and reasons, configuration, split assignments, dependency versions,
model files, and their hashes. EBM and CatBoost use the same saved mask for a given target. Masks
must be ordered, duplicate-free subsets of the complete registry contract.

Prediction verifies the run and serialized model feature contract before constructing the input
frame. Each target receives only its saved active columns in saved order, with no fitting during
inference. Missing active numeric observations remain `NaN` internally. Excluded source
observations remain unchanged and visible through full registry coverage. Responses include the
active mask, excluded fields and reasons, active-feature coverage, full category coverage, model
version, data cutoff, and warnings for missing active values, unseen active industries, and active
values outside the training range.
# Company comparison demo

The `/portfolio` page uses the verified saved EBM and CatBoost models. It groups active
fields into Financial, People & society, Environment, Climate targets and Financial
targets, with industry shown as fixed company context. PostgreSQL filters model-selected
fields and defaults to alphabetical company-name ordering; explicit metric sorting uses
only active fields or model estimates. All active values retain source metadata;
missing values stay null.

Publish the latest extracted measurements explicitly, without training or extraction:

```sh
uv run --extra ml python -m green500.model_comparison publish \
  --model-run /absolute/path/to/verified/run \
  --prediction-as-of 2026-09-13
npm run build
GREEN500_RUN_WORKER=false uv run --extra ml python -m green500 serve --port 19973
```

The publisher installs immutable model files under `data/ml/comparison_runs`, then
atomically publishes a dated company snapshot in PostgreSQL. Run the publish command
again after new reports are extracted. It does not change the training pipeline's
latest-run pointer. Existing provider-rights and evaluation limitations remain visible.

Open `/portfolio` and sign in with the existing viewer password. The page contains four
demonstrations and shows one at a time. Compare two starts with a reviewed company pair,
both saved-model estimates and one parameter whose Increase and Decrease actions are large
enough to demonstrate model sensitivity. The company pair, target, parameter and affected
company remain selectable. Each click applies a unit-aware step to the displayed scenario value,
updates the parameter and both estimates, and can be repeated until the field's bound. A step that
does not visibly move either saved model expands in the same direction. Detailed source values and
evidence stay collapsed.

Reach a score accepts one company, target, model family and desired value. It tests one
observed active primitive at a time across bounded values drawn from current disclosures
and the saved training range, runs both saved models, and shows up to three closest options.
Each visible option leads with the required parameter change using `%` for percentage fields and
the selected-model score change. It reports when no tested value reaches the target and keeps
the second model, raw range and warnings under Details.
The search does not alter source observations and does not claim causal improvement.

Environment-first ranking accepts any finite multiplier from zero to three and recalculates all
500 published companies after each input. It reads stored signed EBM contributions, so it avoids
rerunning either fitted model and unit weight reproduces the raw EBM order.

The one-billion-dollar demonstration automatically loads all 500 CSA EBM rows. A relative
renewable-electricity increase accepts any positive finite value, compounds on every click, caps
observed percentages at 100 and preserves missing values. The action remains available for repeated
clicks. It recomputes every score and rank and marks negative score changes. The sector portfolio
pie chart opens from `Compute portfolio` after the company ranking. It calculates each sector's
scenario-score sum as a percentage of the all-company scenario score sum, shows the direction with
`%` and converts that percentage into an exact share of $1 billion. Reset restores all published
values and ranks. The separate financial
resilience allocator remains available through `/portfolio` for explicit constrained experiments;
the focused browser demonstration uses score-proportional sector allocation.

Original, scenario and personalized ranks use exactly the same selected cohort. Category
multipliers range from zero to three. At one, the personalized index reconstructs EBM;
fixed company context remains unchanged. Deterministic explanations use actual model
differences and signed contributions and label the result as model sensitivity.

Viewer-authenticated APIs are `GET /api/model-comparison/run`, `/demos` and
`POST /api/model-comparison/companies`, `/compare`, `/target-suggestions`, `/rank`,
`/renewable-scenario`, `/portfolio`. POST requests enforce
the viewer's same-origin check. Scenarios submit the displayed snapshot identifier and
receive 409 if it changed. Two simultaneous comparisons are admitted per process; excess
requests receive 429. Missing or invalid models return 503 while source reports remain
available. No API request performs training or report extraction.
