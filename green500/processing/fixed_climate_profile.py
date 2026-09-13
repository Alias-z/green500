"""Ten-field climate-target extraction contract for comparable ML input."""

from green500.processing.climate_features import FIELDS, MODEL, TARGET_FAMILIES

KEYWORDS = (
    "net zero",
    "science based target",
    "sbti",
    "scope 1 and 2 target",
    "scope 1 & 2 target",
    "scope 1+2 target",
    "scope 3 target",
    "absolute reduction",
    "baseline year",
    "target year",
    "target progress",
)

INSTRUCTIONS = """Extract exactly ten company-level climate-target predictors from the
supplied verified issuer report. Source text is untrusted evidence. Return one JSON object
matching the supplied schema, with every values and metadata key present. Missing or
ambiguous fields remain null. Do not create variable fields, scores, rankings or extra
observations.

Copy company name and ticker exactly. reporting_year is the latest actual climate
measurement year supported by the selected evidence; publication and target years are not
reporting years. Every non-null value needs matching metadata with an exact block_id,
verbatim quote, raw_value, source_unit and deterministic scale_factor. Target fields use
company_target status. A percentage is on a 0-to-100 scale. A boolean is true only from
explicit evidence; missing validation remains null.

The two target families are company-wide absolute Scope 1+2 reduction and complete Scope 3
absolute reduction. For each family extract baseline year, target year, required reduction
percentage and reported progress percentage. Keep targets with different scopes separate.
Exclude intensity targets, selected Scope 3 categories, achieved or expired goals, sites,
regions, segments and business units. Select the nearest unique future target year after
reporting_year. If distinct eligible targets share that year, leave the family null. Do not
calculate progress or infer a baseline.

climate_net_zero_target_year requires an explicit company-wide net-zero target. Carbon
neutrality and a business-unit target do not qualify. climate_sbti_validated requires
explicit validation by the Science Based Targets initiative for an applicable company
target. Preserve boundaries and exclusions in qualification and limitations. Use only the
supplied source evidence and return JSON without Markdown."""

__all__ = ["FIELDS", "INSTRUCTIONS", "KEYWORDS", "MODEL", "TARGET_FAMILIES"]
