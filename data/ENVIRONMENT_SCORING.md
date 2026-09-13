# Environmental indicator score v1

`python3 scripts/build_results.py` computes `companies[].scores.environmental` in
root `results.json` from the extracted company records. The website loads these
scores. Materiality and sample financial values never enter the calculation.

This is a **project-defined, provisional index**, not an externally validated ESG
rating, sector benchmark, or assessment of compliance with SBTi. Its thresholds
and equal topic weights are explicit implementation choices. They have not been
empirically calibrated. A score of 100 can describe one narrow indicator; it does
not mean that the company is fully sustainable.

## Calculation

- **Climate:** Scope 1 + market-based Scope 2, using the latest mapped prior year
  in the same report, or the mapped older baseline when no prior year is available.
  Annualized reduction `r = 100 × (1 − (current / baseline)^(1 / years))`.
  Indicator score `clamp(50 + 5 × r, 0, 100)`. Thus unchanged emissions score 50,
  a reduction of 10% or more per year scores 100, and an increase of 10% or more
  scores 0. 3M uses its reported percentage reduction from 2019 to reconstruct the
  relative current-to-baseline ratio. These are changes in absolute emissions,
  not efficiency, and business contraction/divestments can affect them.
- **Energy:** reported renewable electricity percentage, or renewable energy
  divided by total energy in identical units, multiplied by 100. The exact
  denominator appears in each component. Electricity certificate matching is
  explicitly labelled as such, not confused with on-site generation or total
  energy. The energy measures have differing boundaries across companies.
- **Water:** AES's reported 2024–2025 withdrawal reduction uses the same trend
  scale. This does not evaluate local water stress or discharge quality.
- **Environmental:** equal arithmetic average of the available scored topics.
  Missing topics remain null and are excluded from the denominator. Scope 3,
  target commitments, raw absolute inventories without a baseline, and offsets
  are not scored in v1. Other extracted measurements remain visible for review.
- **Overall:** available category scores use weights environmental 50, social 25,
  financial 25, renormalized to available categories. Currently only environment
  exists, so overall equals environmental and is labelled environmental-only.

The UI shows topic coverage out of six, reporting year, each input with its source
page, formula, and limitations. Scores based on different topics and years are
not like-for-like comparisons; ranks order this provisional index, not proven
relative environmental impact. Missing coverage is not rewarded or penalized.

## Current coverage

Ten records have numeric scores: Airbnb and nine of the ten-company extraction
batch. Agilent's record is target-only and remains null. All 503 rows have an
ordinal rank: scored rows first in descending order, then unscored rows ordered
by company name and ticker. Equal scores are ordered by name and ticker.
Sector filters recompute ranks within the sector; searching and sorting preserve
the assigned rank. Rank after the scored group is only a list position.

Air Products explicitly changed its GHG organizational boundary in FY2025 and
states that prior emissions are not comparable (PDF page 53). Its score therefore
uses its 5% active renewable electricity share, not a fabricated emissions trend.
Aflac's observed zero market-based Scope 2 is retained; combined emissions rose
from 2,633 to 3,499 tonnes in 2023. Its resulting zero trend score is a real value,
not a missing result. Airbnb's 2022 score covers office certificate matching only.

## Reproduction and checks

```sh
.venv/bin/python scripts/replay_environment_mappings.py data/extraction/environment_profiles.json
python3 scripts/build_results.py
.venv/bin/python -m unittest discover -s scripts -p 'test_environment*.py'
```

The nine added baseline/share observations use the same source hashes and
reviewed mappings as the original batch. PDF pages were rendered and visually
checked; AMD's baseline was checked in its HTML evidence. The generated score
stores all contributing input paths, values, units, evidence and periods.
