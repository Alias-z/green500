"""Audit current Green500 feature and authorized-label availability."""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

from green500.feature_catalog import feature_catalog
from green500.ml.config import load_ml_config
from green500.ml.dataset import (
    _build_feature_row,
    build_dataset_from_records,
    load_current_feature_records,
    recompute_derived_features,
)
from green500.ml.feature_registry import (
    derived_feature_names,
    feature_registry,
    predictive_feature_names,
)
from green500.ml.labels import load_authorized_labels
from green500.score_catalog import SCORE_FILE, load_scores

SOURCE_CATEGORY_BY_REGISTRY_CATEGORY = {
    "financial": "financial",
    "social": "social_employee",
    "environmental": "environment_report",
    "climate_target": "climate_targets",
    "financial_target": "financial_targets",
}

SEMANTIC_MISSING_STATUSES = {"not_disclosed", "not_applicable"}
FEATURE_REGISTRY = feature_registry()
PRIMITIVE_FEATURES_BY_CATEGORY = {
    category: [
        name
        for name, definition in FEATURE_REGISTRY.items()
        if definition["category_or_context"] == category
        and not definition["derived_feature_dependencies"]
    ]
    for category in (
        "financial",
        "social",
        "environmental",
        "climate_target",
        "financial_target",
    )
}


def _current_feature_audit(settings) -> tuple[dict, dict, list, list, dict]:
    """Summarize current validated values and retain records for label matching."""
    catalog = feature_catalog(settings)
    registry = FEATURE_REGISTRY
    primitive_names = [
        name
        for name, definition in registry.items()
        if name != "industry" and not definition["derived_feature_dependencies"]
    ]
    feature_missingness = {}
    for name in primitive_names:
        available_count = sum(row[name] is not None for row in catalog["rows"])
        feature_missingness[name] = {
            "category_or_context": registry[name]["category_or_context"],
            "available_count": available_count,
            "missing_count": len(catalog["rows"]) - available_count,
            "missing_fraction": (
                (len(catalog["rows"]) - available_count) / len(catalog["rows"])
                if catalog["rows"]
                else 0
            ),
        }
    category_coverage = {}
    for category in (
        "financial",
        "social",
        "environmental",
        "climate_target",
        "financial_target",
    ):
        names = [
            name
            for name in primitive_names
            if registry[name]["category_or_context"] == category
        ]
        source_category = SOURCE_CATEGORY_BY_REGISTRY_CATEGORY[category]
        value_count = sum(
            row[name] is not None for row in catalog["rows"] for name in names
        )
        company_count = sum(
            any(row[name] is not None for name in names) for row in catalog["rows"]
        )
        reporting_years = Counter()
        statuses = Counter()
        for company_metadata in catalog["metadata"].values():
            details = company_metadata.get(source_category) or {}
            reporting_year = details.get("reporting_year")
            if reporting_year is not None:
                reporting_years[str(reporting_year)] += 1
            status = details.get("status")
            if source_category == "financial":
                status = "completed" if details else "not_available"
            statuses[status or "not_started"] += 1
        category_coverage[category] = {
            "feature_count": len(names),
            "companies_with_any_value": company_count,
            "available_value_count": value_count,
            "missing_value_count": len(catalog["rows"]) * len(names) - value_count,
            "reporting_year_counts": dict(sorted(reporting_years.items())),
            "company_status_counts": dict(sorted(statuses.items())),
        }
    companies, observations = load_current_feature_records(settings)
    return category_coverage, feature_missingness, companies, observations, catalog


def _companies_with_all_five_category_values(feature_rows: list[dict]) -> int:
    """Count companies with at least one raw primitive in every source category."""
    return sum(
        all(
            any(row.get(feature_name) is not None for feature_name in feature_names)
            for feature_names in PRIMITIVE_FEATURES_BY_CATEGORY.values()
        )
        for row in feature_rows
    )


def _category_status(category: str, category_metadata: dict) -> str:
    """Normalize current extraction progress separately from field semantics."""
    if category == "financial":
        return "completed" if category_metadata else "not_extracted"
    status = category_metadata.get("status")
    if status == "not_started" or status == "needs_review" or not status:
        return "not_extracted"
    return status


def _null_status(category_status: str, field_metadata: dict) -> str:
    """Classify a null without mixing extraction progress with semantic absence."""
    if category_status == "processing":
        return "processing"
    field_status = field_metadata.get("status")
    if field_status in SEMANTIC_MISSING_STATUSES:
        return field_status
    if field_status == "conflicting":
        return "conflicting"
    if field_status in {"not_extracted", "processing", "unknown"}:
        return field_status
    if category_status in {"not_extracted", "processing", "unknown"}:
        return category_status
    return "unspecified_null"


def _field_source_metadata(catalog: dict, company_cik: str, feature_name: str) -> dict:
    """Read category and field status from the existing current feature catalog."""
    registry_category = FEATURE_REGISTRY[feature_name]["category_or_context"]
    source_category = SOURCE_CATEGORY_BY_REGISTRY_CATEGORY[registry_category]
    category_metadata = catalog["metadata"].get(company_cik, {}).get(source_category) or {}
    field_metadata = (category_metadata.get("fields") or {}).get(feature_name) or {}
    category_status = _category_status(source_category, category_metadata)
    reason = (
        field_metadata.get("reason")
        or field_metadata.get("notes")
        or field_metadata.get("export_reason")
    )
    return {
        "source_category": source_category,
        "category_status": category_status,
        "field_status": field_metadata.get("status"),
        "null_status": _null_status(category_status, field_metadata),
        "reason": reason,
        "source_document_id": category_metadata.get("source_document_id"),
    }


def _raw_feature_rows(companies, observations) -> tuple[list[dict], list[dict]]:
    """Build current values before date filtering and derive ratios consistently."""
    observations_by_company = {}
    for observation in observations:
        observations_by_company.setdefault(observation.company_cik, {})[
            observation.feature_name
        ] = observation
    registry = FEATURE_REGISTRY
    rows = []
    metadata_rows = []
    for company in companies:
        company_observations = observations_by_company.get(company.company_cik, {})
        values = {name: None for name in predictive_feature_names()}
        metadata = {}
        values["industry"] = company.industry
        metadata["industry"] = {
            "canonical_unit": None,
            "reporting_year": None,
            "publication_date": None,
            "boundary": None,
            "confidence": None,
        }
        for feature_name, definition in registry.items():
            if feature_name == "industry" or definition["derived_feature_dependencies"]:
                continue
            observation = company_observations.get(feature_name)
            if observation is None:
                metadata[feature_name] = {
                    "canonical_unit": definition["canonical_unit"],
                    "reporting_year": None,
                    "publication_date": None,
                    "boundary": None,
                    "confidence": None,
                }
                continue
            values[feature_name] = observation.value
            metadata[feature_name] = {
                "canonical_unit": definition["canonical_unit"],
                "reporting_year": observation.reporting_year,
                "publication_date": (
                    observation.publication_date.isoformat()
                    if observation.publication_date
                    else None
                ),
                "boundary": observation.boundary,
                "confidence": observation.confidence,
            }
        values, metadata = recompute_derived_features(values, metadata)
        rows.append(values)
        metadata_rows.append(metadata)
    return rows, metadata_rows


def _dated_feature_rows(companies, observations, cutoff: date) -> list[dict]:
    """Build current-source rows using the same date checks as model datasets."""
    observations_by_company = {}
    for observation in observations:
        observations_by_company.setdefault(observation.company_cik, []).append(
            observation
        )
    return [
        _build_feature_row(
            company,
            observations_by_company.get(company.company_cik, []),
            cutoff,
        )[0]
        for company in companies
    ]


def _distinct_count(values: list) -> int:
    """Count distinct typed values while keeping booleans separate from numbers."""
    return len({(type(value).__name__, value) for value in values if value is not None})


def _field_missing_statuses(
    catalog: dict, companies, raw_rows: list[dict], feature_name: str
) -> list[str]:
    """Return one normalized current missing status for every company row."""
    rows_by_cik = {row["company_cik"]: row for row in catalog["rows"]}
    statuses = []
    definition = FEATURE_REGISTRY[feature_name]
    dependencies = definition["derived_feature_dependencies"]
    if feature_name == "industry":
        return ["observed" for _ in companies]
    for company, raw_feature_row in zip(companies, raw_rows, strict=True):
        row = rows_by_cik[company.company_cik]
        if raw_feature_row.get(feature_name) is not None:
            statuses.append("observed")
            continue
        if dependencies:
            dependency_statuses = [
                _field_source_metadata(catalog, company.company_cik, dependency)[
                    "null_status"
                ]
                for dependency in dependencies
                if row.get(dependency) is None
            ]
            status = next(
                (
                    value
                    for value in dependency_statuses
                    if value
                    in {"processing", "not_extracted", "unknown", "unspecified_null"}
                ),
                dependency_statuses[0] if dependency_statuses else "unspecified_null",
            )
            statuses.append(status)
            continue
        statuses.append(
            _field_source_metadata(catalog, company.company_cik, feature_name)[
                "null_status"
            ]
        )
    return statuses


def _publication_exclusion_counts(
    companies, observations, raw_rows: list[dict], dated_rows: list[dict], cutoff: date
) -> dict[str, dict]:
    """Count raw values excluded by absent or future publication dates."""
    observations_by_company = {}
    for observation in observations:
        observations_by_company.setdefault(observation.company_cik, {})[
            observation.feature_name
        ] = observation
    registry = FEATURE_REGISTRY
    result = {}
    for feature_name, definition in registry.items():
        missing_date_count = 0
        after_cutoff_count = 0
        excluded_count = 0
        dependencies = definition["derived_feature_dependencies"]
        for company, raw_row, dated_row in zip(
            companies, raw_rows, dated_rows, strict=True
        ):
            if raw_row.get(feature_name) is None or dated_row.get(feature_name) is not None:
                continue
            excluded_count += 1
            source_names = dependencies or [feature_name]
            source_observations = [
                observations_by_company.get(company.company_cik, {}).get(name)
                for name in source_names
            ]
            if any(
                item is not None
                and item.value is not None
                and item.publication_date is None
                for item in source_observations
            ):
                missing_date_count += 1
            elif any(
                item is not None
                and item.value is not None
                and item.publication_date is not None
                and item.publication_date > cutoff
                for item in source_observations
            ):
                after_cutoff_count += 1
        result[feature_name] = {
            "raw_values_excluded_count": excluded_count,
            "missing_publication_date_count": missing_date_count,
            "published_after_cutoff_count": after_cutoff_count,
        }
    return result


def _provisional_feature_eligibility(
    raw_rows: list[dict],
    dated_rows: list[dict],
    missing_statuses: dict[str, list[str]],
    selection_config: dict,
) -> dict[str, dict]:
    """Apply configured engineering defaults without permanently deleting fields."""
    company_count = len(dated_rows)
    required_observed_count = max(
        selection_config["minimum_observed_companies"],
        math.ceil(selection_config["minimum_observed_fraction"] * company_count),
    )
    ambiguous_statuses = set(selection_config["ambiguous_missing_statuses"])
    registry = FEATURE_REGISTRY
    result = {}
    for feature_name, definition in registry.items():
        raw_values = [row.get(feature_name) for row in raw_rows]
        dated_values = [row.get(feature_name) for row in dated_rows]
        raw_observed = [value for value in raw_values if value is not None]
        dated_observed = [value for value in dated_values if value is not None]
        statuses = missing_statuses[feature_name]
        ambiguous_count = sum(status in ambiguous_statuses for status in statuses)
        ambiguous_fraction = ambiguous_count / company_count if company_count else 0
        boolean_levels = (
            {
                "false": sum(value is False for value in dated_observed),
                "true": sum(value is True for value in dated_observed),
            }
            if definition["data_type"] == "boolean"
            else None
        )
        exclusion_reasons = []
        if len(dated_observed) < required_observed_count:
            exclusion_reasons.append("observed_count_below_minimum")
        if (
            definition["data_type"] in {"number", "integer"}
            and _distinct_count(dated_observed)
            < selection_config["minimum_continuous_distinct_values"]
        ):
            exclusion_reasons.append("continuous_distinct_values_below_minimum")
        if boolean_levels is not None and min(boolean_levels.values()) < selection_config[
            "minimum_boolean_count_per_level"
        ]:
            exclusion_reasons.append("boolean_level_support_below_minimum")
        if ambiguous_fraction > selection_config["maximum_ambiguous_missing_fraction"]:
            exclusion_reasons.append("ambiguous_missing_fraction_above_maximum")
        result[feature_name] = {
            "category_or_context": definition["category_or_context"],
            "raw_support_count": len(raw_observed),
            "raw_support_fraction": len(raw_observed) / company_count
            if company_count
            else 0,
            "raw_distinct_count": _distinct_count(raw_observed),
            "raw_zero_count": sum(
                not isinstance(value, bool) and value == 0 for value in raw_observed
            ),
            "dated_eligible_support_count": len(dated_observed),
            "dated_eligible_support_fraction": len(dated_observed) / company_count
            if company_count
            else 0,
            "dated_eligible_distinct_count": _distinct_count(dated_observed),
            "dated_eligible_zero_count": sum(
                not isinstance(value, bool) and value == 0 for value in dated_observed
            ),
            "boolean_level_counts": boolean_levels,
            "missing_status_counts": dict(sorted(Counter(statuses).items())),
            "ambiguous_missing_count": ambiguous_count,
            "ambiguous_missing_fraction": ambiguous_fraction,
            "required_observed_count": required_observed_count,
            "is_provisionally_eligible": not exclusion_reasons,
            "provisional_exclusion_reasons": exclusion_reasons,
        }
    return result


def _completed_null_reason_distribution(catalog: dict) -> dict:
    """Count semantic and extraction null reasons within completed categories only."""
    registry = FEATURE_REGISTRY
    rows_by_cik = {row["company_cik"]: row for row in catalog["rows"]}
    category_counts = {}
    field_counts = {}
    for company_cik, company_metadata in catalog["metadata"].items():
        row = rows_by_cik[company_cik]
        for feature_name, definition in registry.items():
            if feature_name == "industry" or definition["derived_feature_dependencies"]:
                continue
            registry_category = definition["category_or_context"]
            source_category = SOURCE_CATEGORY_BY_REGISTRY_CATEGORY[registry_category]
            category_metadata = company_metadata.get(source_category) or {}
            if _category_status(source_category, category_metadata) != "completed":
                continue
            if row.get(feature_name) is not None:
                continue
            source_metadata = _field_source_metadata(
                catalog, company_cik, feature_name
            )
            status = source_metadata["null_status"]
            category = category_counts.setdefault(
                registry_category,
                Counter(),
            )
            category[status] += 1
            field = field_counts.setdefault(
                feature_name,
                Counter(),
            )
            field[status] += 1
    return {
        "by_category": {
            name: {
                "null_count": sum(value.values()),
                "status_counts": dict(sorted(value.items())),
            }
            for name, value in category_counts.items()
        },
        "by_field": {
            name: {
                "null_count": sum(value.values()),
                "status_counts": dict(sorted(value.items())),
            }
            for name, value in field_counts.items()
        },
    }


def _repair_manifest(
    catalog: dict,
    companies,
    observations,
    cutoff: date,
    ambiguous_missing_statuses: set[str],
) -> list[dict]:
    """List source metadata repairs without mutating any extraction result."""
    registry = FEATURE_REGISTRY
    company_by_cik = {company.company_cik: company for company in companies}
    rows_by_cik = {row["company_cik"]: row for row in catalog["rows"]}
    repairs = []
    for observation in observations:
        if observation.value is None or observation.publication_date is not None:
            continue
        definition = registry[observation.feature_name]
        company = company_by_cik[observation.company_cik]
        repairs.append(
            {
                "issue": "missing_publication_date",
                "source_document_id": observation.source_document_id,
                "company_cik": company.company_cik,
                "ticker": company.ticker,
                "company_name": company.company_name,
                "category": definition["category_or_context"],
                "field": observation.feature_name,
                "current_status": observation.status,
                "current_reason": observation.missing_reason,
                "audit_cutoff": cutoff.isoformat(),
            }
        )
    for company_cik, company_metadata in catalog["metadata"].items():
        row = rows_by_cik[company_cik]
        company = company_by_cik[company_cik]
        for feature_name, definition in registry.items():
            if feature_name == "industry" or definition["derived_feature_dependencies"]:
                continue
            if row.get(feature_name) is not None:
                continue
            source_category = SOURCE_CATEGORY_BY_REGISTRY_CATEGORY[
                definition["category_or_context"]
            ]
            category_metadata = company_metadata.get(source_category) or {}
            if _category_status(source_category, category_metadata) != "completed":
                continue
            source_metadata = _field_source_metadata(catalog, company_cik, feature_name)
            if source_metadata["null_status"] not in ambiguous_missing_statuses:
                continue
            repairs.append(
                {
                    "issue": "ambiguous_null_reason",
                    "source_document_id": source_metadata["source_document_id"],
                    "company_cik": company.company_cik,
                    "ticker": company.ticker,
                    "company_name": company.company_name,
                    "category": definition["category_or_context"],
                    "field": feature_name,
                    "current_status": source_metadata["field_status"],
                    "current_reason": source_metadata["reason"],
                    "audit_cutoff": cutoff.isoformat(),
                }
            )
    repairs.sort(
        key=lambda row: (
            row["issue"],
            row["company_name"],
            row["category"],
            row["field"],
        )
    )
    return repairs


def _repair_manifest_summary(
    repair_rows: list[dict], cutoff: date, output_path: Path | None
) -> dict:
    """Optionally write full repair rows and return a compact read-only summary."""
    issue_counts = dict(sorted(Counter(row["issue"] for row in repair_rows).items()))
    summary = {
        "read_only": True,
        "row_count": len(repair_rows),
        "issue_counts": issue_counts,
        "output_path": str(output_path) if output_path else None,
    }
    if output_path is None:
        return summary
    output_path = Path(output_path)
    if output_path.is_symlink():
        raise ValueError("Repair manifest output must not be a symlink.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "read_only": True,
        "audit_cutoff": cutoff.isoformat(),
        "row_count": len(repair_rows),
        "issue_counts": issue_counts,
        "rows": repair_rows,
    }
    temporary_path = output_path.with_name(
        f".{output_path.name}.{os.getpid()}.tmp"
    )
    temporary_path.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    )
    temporary_path.replace(output_path)
    return summary


def _official_score_audit() -> dict:
    """Describe genuine current labels separately from authorized training input."""
    scores, generated_at = load_scores()
    available = [value for value in scores.values() if value["status"] == "available"]
    statuses = Counter(value["status"] for value in scores.values())
    return {
        "source_name": "S&P Global Sustainable1 public company result pages",
        "source_path": str(SCORE_FILE),
        "snapshot_generated_at": generated_at,
        "company_count": len(scores),
        "status_counts": dict(sorted(statuses.items())),
        "esg_available_count": sum(value["esg_score"] is not None for value in available),
        "csa_available_count": sum(value["csa_score"] is not None for value in available),
        "assessment_cycle_available_count": sum(
            value["assessment_year"] is not None for value in available
        ),
        "source_date_min": min(
            (value["last_updated"] for value in available), default=None
        ),
        "source_date_max": max(
            (value["last_updated"] for value in available), default=None
        ),
        "authorized_for_training": False,
        "training_blockers": [
            "The repository contains no authorization reference for model training.",
            "The public score pages do not identify an assessment cycle.",
            "Current score records are not automatically imported into the ML label template.",
        ],
    }


def audit_data(
    settings,
    labels_path: Path | None = None,
    config_path: Path | None = None,
    repair_output_path: Path | None = None,
) -> dict:
    """Report current data coverage and independently usable target counts."""
    category_coverage, feature_missingness, companies, observations, catalog = (
        _current_feature_audit(settings)
    )
    resolved_config_path = config_path or Path(__file__).resolve().parents[2] / "config/ml.yaml"
    config = load_ml_config(resolved_config_path)
    selection_config = config["training"]["feature_selection"]
    audit_cutoff = datetime.now(UTC).date()
    raw_rows, _ = _raw_feature_rows(companies, observations)
    dated_rows = _dated_feature_rows(companies, observations, audit_cutoff)
    missing_statuses = {
        feature_name: _field_missing_statuses(
            catalog, companies, raw_rows, feature_name
        )
        for feature_name in predictive_feature_names()
    }
    readiness = _provisional_feature_eligibility(
        raw_rows, dated_rows, missing_statuses, selection_config
    )
    publication_exclusions = _publication_exclusion_counts(
        companies, observations, raw_rows, dated_rows, audit_cutoff
    )
    for feature_name, exclusions in publication_exclusions.items():
        readiness[feature_name]["publication_date_exclusions"] = exclusions
    completed_null_reasons = _completed_null_reason_distribution(catalog)
    repair_rows = _repair_manifest(
        catalog,
        companies,
        observations,
        audit_cutoff,
        set(selection_config["ambiguous_missing_statuses"]),
    )
    repair_summary = _repair_manifest_summary(
        repair_rows, audit_cutoff, repair_output_path
    )
    authorized_labels = load_authorized_labels(labels_path) if labels_path else []
    dataset = build_dataset_from_records(companies, observations, authorized_labels)
    usable_counts = {}
    numeric_names = [
        name
        for name, definition in dataset["registry"].items()
        if definition["predictive"] and not definition["categorical"]
    ]
    for target in ("esg", "csa"):
        usable_counts[target] = sum(
            labels[f"{target}_score"] is not None
            and any(features[name] is not None for name in numeric_names)
            for features, labels in zip(dataset["X"], dataset["labels"], strict=True)
        )
    label_counts = Counter(label.target_name for label in authorized_labels)
    companies_with_all_five_category_values = _companies_with_all_five_category_values(
        catalog["rows"]
    )
    reporting_years = sorted(
        {
            year
            for value in category_coverage.values()
            for year in value["reporting_year_counts"]
        }
    )
    limitations = [
        "Current category processing is incomplete.",
        "An undated source value is quarantined from every dated feature row.",
        "Synthetic fixtures are allowed for tests only and are excluded from this audit.",
    ]
    if companies_with_all_five_category_values == 0:
        limitations.append(
            "Zero current companies have a populated primitive in all five categories."
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "distinct_company_count": len(companies),
        "reporting_years": reporting_years,
        "primitive_feature_count": len(feature_missingness),
        "derived_feature_count": len(derived_feature_names()),
        "categorical_feature_count": 1,
        "category_coverage": category_coverage,
        "companies_with_any_value_in_all_five_categories": (
            companies_with_all_five_category_values
        ),
        "feature_missingness": feature_missingness,
        "feature_readiness": readiness,
        "provisional_eligibility": {
            "description": (
                "Current-snapshot engineering estimate. Training recomputes selection "
                "inside each target and training partition; this audit does not permanently "
                "delete registry fields."
            ),
            "threshold_role": (
                "These configured thresholds are engineering defaults for provisional "
                "run eligibility. They are not claims about permanent feature quality."
            ),
            "audit_cutoff": audit_cutoff.isoformat(),
            "configured_thresholds": selection_config,
            "eligible_feature_count": sum(
                value["is_provisionally_eligible"] for value in readiness.values()
            ),
            "excluded_feature_count": sum(
                not value["is_provisionally_eligible"] for value in readiness.values()
            ),
            "eligible_features": [
                name
                for name, value in readiness.items()
                if value["is_provisionally_eligible"]
            ],
            "excluded_features": {
                name: value["provisional_exclusion_reasons"]
                for name, value in readiness.items()
                if not value["is_provisionally_eligible"]
            },
        },
        "completed_category_null_reasons": completed_null_reasons,
        "publication_date_exclusion_counts": {
            "raw_values_excluded_count": sum(
                value["raw_values_excluded_count"]
                for value in publication_exclusions.values()
            ),
            "missing_publication_date_count": sum(
                value["missing_publication_date_count"]
                for value in publication_exclusions.values()
            ),
            "published_after_cutoff_count": sum(
                value["published_after_cutoff_count"]
                for value in publication_exclusions.values()
            ),
        },
        "repair_manifest": repair_summary,
        "official_score_snapshot": _official_score_audit(),
        "authorized_label_import": {
            "path": str(labels_path) if labels_path else None,
            "label_count": len(authorized_labels),
            "target_counts": {
                target: label_counts.get(target, 0) for target in ("esg", "csa")
            },
            "distinct_company_count": len(
                {label.company_cik for label in authorized_labels}
            ),
            "assessment_cycle_count": len(
                {
                    (label.assessment_cycle, label.prediction_as_of)
                    for label in authorized_labels
                }
            ),
        },
        "usable_company_cycle_counts": usable_counts,
        "limitations": limitations,
    }
