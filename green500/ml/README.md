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
  --repair-output data/ml/audit/source-metadata-repairs.json

uv run --extra ml python -m green500.ml build-dataset \
  --config config/ml.yaml \
  --labels data/reference/ml_labels_template.csv \
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
