"""Build dated, row-aligned Green500 machine-learning datasets."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import shutil
import time
from collections import Counter
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path

from green500 import db
from green500.feature_catalog import feature_catalog, feature_schema
from green500.ml.contracts import (
    CompanyRecord,
    FeatureObservation,
    LabelRecord,
    training_row_id,
)
from green500.ml.feature_registry import (
    FEATURE_REGISTRY_VERSION,
    categorical_feature_names,
    derived_feature_names,
    feature_registry,
    numeric_feature_names,
    predictive_feature_names,
)
from green500.ml.labels import load_authorized_labels

DATASET_VERSION = "green500-ml-dataset-v1"
COMPANY_COLUMNS = (
    "row_id",
    "company_cik",
    "ticker",
    "company_name",
    "assessment_cycle",
    "prediction_as_of",
)
LABEL_COLUMNS = (
    "row_id",
    "esg_score",
    "esg_label_published_at",
    "esg_source_name",
    "esg_source_url",
    "esg_source_date",
    "esg_source_sha256",
    "esg_authorization_reference",
    "csa_score",
    "csa_label_published_at",
    "csa_source_name",
    "csa_source_url",
    "csa_source_date",
    "csa_source_sha256",
    "csa_authorization_reference",
)
SNAPSHOT_FILES = (
    "X.csv",
    "labels.csv",
    "companies.csv",
    "metadata.json",
    "schema.json",
    "registry.json",
    "coverage.json",
    "audit.json",
)

_SOURCE_CATEGORIES = {
    "financial": "financial",
    "social": "social_employee",
    "environmental": "environment_report",
    "climate_target": "climate_targets",
    "financial_target": "financial_targets",
}

_DERIVED_UNITS = {
    "operating_margin": ("USD", "USD"),
    "debt_assets": ("USD", "USD"),
    "capex_revenue": ("USD", "USD"),
    "emissions_intensity": ("tCO2e", "USD"),
}


def _json_bytes(value) -> bytes:
    """Serialize one deterministic JSON artifact without non-finite numbers."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _csv_bytes(fieldnames, rows) -> bytes:
    """Serialize aligned CSV rows while preserving missing values as empty cells."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: int(value) if isinstance(value, bool) else value
                for key, value in row.items()
            }
        )
    return stream.getvalue().encode()


def _parse_date(value) -> date | None:
    """Parse only explicit ISO publication dates."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _parse_datetime(value) -> datetime | None:
    """Parse an extraction timestamp for audit ordering only."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _document_publication_dates(settings, document_ids: list[int]) -> dict[int, date]:
    """Read reviewed publication dates bound to exact source-document hashes."""
    if not document_ids:
        return {}
    with db.connect(settings) as connection:
        rows = connection.execute(
            """SELECT d.id,rs.review->>'publication_date' AS publication_date
            FROM documents d
            JOIN report_sources rs
              ON rs.company_cik=d.company_cik
             AND rs.review->>'document_sha256'=d.sha256
            WHERE d.id=ANY(%s)""",
            (sorted(set(document_ids)),),
        ).fetchall()
    publication_dates = {}
    for row in rows:
        publication_date = _parse_date(row["publication_date"])
        if publication_date is not None:
            publication_dates[row["id"]] = publication_date
    return publication_dates


def load_current_feature_records(settings) -> tuple[list[CompanyRecord], list[FeatureObservation]]:
    """Adapt current validated outputs without treating processing time as publication time."""
    catalog = feature_catalog(settings)
    with db.connect(settings) as connection:
        company_rows = connection.execute(
            "SELECT cik,name,symbols,sector FROM companies WHERE is_current ORDER BY name,cik"
        ).fetchall()
    companies = [
        CompanyRecord(
            company_cik=row["cik"],
            ticker=row["symbols"][0] if row["symbols"] else row["cik"],
            company_name=row["name"],
            industry=(row["sector"] or "Unknown").strip() or "Unknown",
        )
        for row in company_rows
    ]
    document_ids = [
        details["source_document_id"]
        for company_metadata in catalog["metadata"].values()
        for details in company_metadata.values()
        if isinstance(details, dict) and isinstance(details.get("source_document_id"), int)
    ]
    publication_dates = _document_publication_dates(settings, document_ids)
    source_definitions = feature_schema()["fields"]
    registry = feature_registry()
    observations = []
    for feature_row in catalog["rows"]:
        company_cik = feature_row["company_cik"]
        company_metadata = catalog["metadata"].get(company_cik, {})
        for feature_name in source_definitions:
            registry_category = registry[feature_name]["category_or_context"]
            source_category = _SOURCE_CATEGORIES[registry_category]
            category_metadata = company_metadata.get(source_category) or {}
            field_metadata = (category_metadata.get("fields") or {}).get(feature_name) or {}
            source_document_id = category_metadata.get("source_document_id")
            value = feature_row.get(feature_name)
            reporting_year = field_metadata.get(
                "reporting_year", field_metadata.get("fiscal_year")
            )
            if reporting_year is None:
                reporting_year = category_metadata.get("reporting_year")
            category_evidence = category_metadata.get("evidence") or {}
            source_url = field_metadata.get("source_url") or category_evidence.get(
                "source_url"
            )
            missing_reason = (
                field_metadata.get("reason")
                or field_metadata.get("notes")
                or field_metadata.get("export_reason")
            )
            if value is None and not missing_reason:
                missing_reason = "The current validated source has no usable value."
            observations.append(
                FeatureObservation(
                    company_cik=company_cik,
                    feature_name=feature_name,
                    value=value,
                    unit=registry[feature_name]["canonical_unit"],
                    reporting_year=reporting_year,
                    publication_date=publication_dates.get(source_document_id),
                    processed_at=_parse_datetime(category_metadata.get("processed_at")),
                    boundary=category_metadata.get("boundary"),
                    status=field_metadata.get("status")
                    or category_metadata.get("status")
                    or ("available" if value is not None else "not_available"),
                    missing_reason=missing_reason,
                    qualification=field_metadata.get("qualification"),
                    confidence=field_metadata.get("confidence"),
                    source_document_id=source_document_id,
                    source_url=source_url,
                    evidence=field_metadata.get("evidence"),
                )
            )
    return companies, observations


def _normalized_source_missing_status(observation: FeatureObservation) -> str:
    """Keep extraction progress separate from semantic missing disclosure."""
    if observation.value is not None:
        return "observed"
    status = observation.status.strip().casefold().replace(" ", "_")
    if status in {
        "processing",
        "not_extracted",
        "unknown",
        "not_disclosed",
        "not_applicable",
        "conflicting",
    }:
        return status
    if status in {"not_started", "not_available", "needs_review", "missing"}:
        return "not_extracted"
    return "unspecified_null"


def _missing_feature_metadata(
    reason: str,
    canonical_unit: str | None,
    source_missing_status: str = "unspecified_null",
) -> dict:
    """Return the common audit shape for one unavailable feature."""
    return {
        "status": "missing",
        "source_missing_status": source_missing_status,
        "missing_reason": reason,
        "reporting_year": None,
        "publication_date": None,
        "processed_at": None,
        "boundary": None,
        "qualification": None,
        "confidence": None,
        "source_document_id": None,
        "source_url": None,
        "evidence": None,
        "canonical_unit": canonical_unit,
    }


def _observation_metadata(observation: FeatureObservation, canonical_unit: str | None) -> dict:
    """Convert one selected observation to JSON-safe audit metadata."""
    return {
        "status": observation.status,
        "source_missing_status": _normalized_source_missing_status(observation),
        "missing_reason": observation.missing_reason,
        "reporting_year": observation.reporting_year,
        "publication_date": (
            observation.publication_date.isoformat()
            if observation.publication_date is not None
            else None
        ),
        "processed_at": (
            observation.processed_at.isoformat()
            if observation.processed_at is not None
            else None
        ),
        "boundary": observation.boundary,
        "qualification": observation.qualification,
        "confidence": observation.confidence,
        "source_document_id": observation.source_document_id,
        "source_url": observation.source_url,
        "evidence": observation.evidence,
        "canonical_unit": canonical_unit,
    }


def _value_matches_type(value, data_type: str) -> bool:
    """Check a primitive value without coercing strings or booleans into numbers."""
    if value is None:
        return True
    if data_type == "boolean":
        return isinstance(value, bool)
    if data_type == "string":
        return isinstance(value, str) and bool(value.strip())
    if data_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _select_observation(
    observations: list[FeatureObservation], definition: dict, prediction_as_of: date
) -> tuple[object, dict, list[dict]]:
    """Select the latest eligible source value and explain every rejected candidate."""
    quarantined = []
    eligible = []
    for observation in observations:
        if observation.publication_date is None:
            quarantined.append(
                {
                    "feature_name": observation.feature_name,
                    "source_document_id": observation.source_document_id,
                    "reason": "The source has no reliable publication_date.",
                }
            )
            continue
        if observation.publication_date > prediction_as_of:
            quarantined.append(
                {
                    "feature_name": observation.feature_name,
                    "source_document_id": observation.source_document_id,
                    "reason": "The source was published after prediction_as_of.",
                }
            )
            continue
        if observation.unit != definition["canonical_unit"]:
            quarantined.append(
                {
                    "feature_name": observation.feature_name,
                    "source_document_id": observation.source_document_id,
                    "reason": "The observation unit does not match the canonical unit.",
                }
            )
            continue
        if not _value_matches_type(observation.value, definition["data_type"]):
            quarantined.append(
                {
                    "feature_name": observation.feature_name,
                    "source_document_id": observation.source_document_id,
                    "reason": "The observation value does not match the registered data type.",
                }
            )
            continue
        eligible.append(observation)
    if not eligible:
        reason = (
            quarantined[-1]["reason"]
            if quarantined
            else "No observation exists for this company and feature."
        )
        source_missing_status = (
            _normalized_source_missing_status(observations[-1])
            if observations
            else "not_extracted"
        )
        return (
            None,
            _missing_feature_metadata(
                reason, definition["canonical_unit"], source_missing_status
            ),
            quarantined,
        )
    eligible.sort(
        key=lambda item: (
            item.publication_date,
            item.reporting_year or 0,
            item.processed_at or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )
    selected = eligible[0]
    same_period = [
        item
        for item in eligible
        if (item.publication_date, item.reporting_year)
        == (selected.publication_date, selected.reporting_year)
    ]
    non_null_values = {json.dumps(item.value, sort_keys=True) for item in same_period if item.value is not None}
    if len(non_null_values) > 1:
        quarantined.extend(
            {
                "feature_name": item.feature_name,
                "source_document_id": item.source_document_id,
                "reason": "Conflicting values share the latest publication date and reporting year.",
            }
            for item in same_period
        )
        return (
            None,
            _missing_feature_metadata(
                "Conflicting latest observations were quarantined.",
                definition["canonical_unit"],
                "conflicting",
            ),
            quarantined,
        )
    return selected.value, _observation_metadata(selected, definition["canonical_unit"]), quarantined


def _dependency_values_are_compatible(
    dependency_names: list[str], features: dict, metadata: dict, expected_units: tuple[str, str]
) -> tuple[bool, str | None]:
    """Check values, denominators, years, units, and stated boundaries for one ratio."""
    numerator_name, denominator_name = dependency_names
    numerator = features.get(numerator_name)
    denominator = features.get(denominator_name)
    if numerator is None or denominator is None:
        return False, "A required derived-feature dependency is missing."
    if not _value_matches_type(numerator, "number") or not _value_matches_type(
        denominator, "number"
    ):
        return False, "A derived-feature dependency is not numeric."
    if float(denominator) <= 0:
        return False, "The derived-feature denominator must be positive."
    dependency_metadata = [metadata.get(name) or {} for name in dependency_names]
    if tuple(item.get("canonical_unit") for item in dependency_metadata) != expected_units:
        return False, "Derived-feature dependency units are incompatible."
    years = [item.get("reporting_year") for item in dependency_metadata]
    if any(year is None for year in years) or len(set(years)) != 1:
        return False, "Derived-feature dependency reporting years are incompatible."
    boundaries = [
        " ".join(str(item.get("boundary") or "").split()).casefold()
        for item in dependency_metadata
        if item.get("boundary")
    ]
    if len(set(boundaries)) > 1:
        return False, "Derived-feature dependency boundaries are incompatible."
    return True, None


def recompute_derived_features(features: dict, metadata: dict) -> tuple[dict, dict]:
    """Return copies with every registered ratio consistently recomputed."""
    updated_features = deepcopy(features)
    updated_metadata = deepcopy(metadata)
    registry = feature_registry()
    for feature_name in derived_feature_names():
        definition = registry[feature_name]
        dependencies = definition["derived_feature_dependencies"]
        is_compatible, reason = _dependency_values_are_compatible(
            dependencies,
            updated_features,
            updated_metadata,
            _DERIVED_UNITS[feature_name],
        )
        if not is_compatible:
            dependency_statuses = [
                (updated_metadata.get(name) or {}).get("source_missing_status")
                for name in dependencies
                if updated_features.get(name) is None
            ]
            source_missing_status = next(
                (
                    status
                    for status in dependency_statuses
                    if status
                    in {
                        "processing",
                        "not_extracted",
                        "unknown",
                        "unspecified_null",
                    }
                ),
                next(
                    (
                        status
                        for status in dependency_statuses
                        if status in {"not_disclosed", "not_applicable"}
                    ),
                    "conflicting"
                    if not dependency_statuses
                    else dependency_statuses[0] or "unspecified_null",
                ),
            )
            updated_features[feature_name] = None
            updated_metadata[feature_name] = _missing_feature_metadata(
                reason or "Derived-feature inputs are incompatible.",
                definition["canonical_unit"],
                source_missing_status,
            )
            continue
        numerator_name, denominator_name = dependencies
        updated_features[feature_name] = float(updated_features[numerator_name]) / float(
            updated_features[denominator_name]
        )
        dependency_metadata = [updated_metadata[name] for name in dependencies]
        publication_dates = [
            item["publication_date"]
            for item in dependency_metadata
            if item.get("publication_date")
        ]
        confidence_values = [
            item["confidence"]
            for item in dependency_metadata
            if item.get("confidence") is not None
        ]
        boundaries = [
            item["boundary"] for item in dependency_metadata if item.get("boundary")
        ]
        updated_metadata[feature_name] = {
            "status": "derived",
            "source_missing_status": "observed",
            "missing_reason": None,
            "reporting_year": dependency_metadata[0]["reporting_year"],
            "publication_date": max(publication_dates) if publication_dates else None,
            "processed_at": None,
            "boundary": boundaries[0] if boundaries else None,
            "qualification": "Computed from canonical source-backed dependencies.",
            "confidence": min(confidence_values) if confidence_values else None,
            "source_document_id": None,
            "source_url": None,
            "evidence": {"dependencies": dependencies},
            "canonical_unit": definition["canonical_unit"],
        }
    return updated_features, updated_metadata


def _coverage_level(fraction: float) -> str:
    """Assign the stable coverage band used for error analysis."""
    if fraction == 0:
        return "none"
    if fraction < 0.33:
        return "low"
    if fraction < 0.67:
        return "medium"
    return "high"


def _row_coverage(features: dict, registry: dict) -> dict:
    """Summarize overall and category availability for one assessment row."""
    predictive = [name for name, value in registry.items() if value["predictive"]]
    available_count = sum(features.get(name) is not None for name in predictive)
    fraction = available_count / len(predictive) if predictive else 0
    categories = {}
    for category in (
        "financial",
        "social",
        "environmental",
        "climate_target",
        "financial_target",
        "fixed_context",
    ):
        names = [
            name
            for name, definition in registry.items()
            if definition["predictive"]
            and definition["category_or_context"] == category
        ]
        category_available = sum(features.get(name) is not None for name in names)
        categories[category] = {
            "available_count": category_available,
            "feature_count": len(names),
            "fraction": category_available / len(names) if names else 0,
        }
    return {
        "overall": {
            "available_count": available_count,
            "feature_count": len(predictive),
            "fraction": fraction,
            "level": _coverage_level(fraction),
        },
        "categories": categories,
    }


def _label_output(label: LabelRecord | None, target_name: str) -> dict:
    """Return the fixed wide label columns for one independently nullable target."""
    prefix = target_name + "_"
    names = (
        "score",
        "label_published_at",
        "source_name",
        "source_url",
        "source_date",
        "source_sha256",
        "authorization_reference",
    )
    if label is None:
        return {prefix + name: None for name in names}
    payload = label.model_dump(mode="json")
    return {prefix + name: payload[name] for name in names}


def _build_feature_row(
    company: CompanyRecord,
    company_observations: list[FeatureObservation],
    prediction_as_of: date,
) -> tuple[dict, dict, dict, list[dict]]:
    """Build one cutoff-safe feature row and its audit metadata."""
    registry = feature_registry()
    features = {name: None for name in predictive_feature_names()}
    metadata = {
        name: _missing_feature_metadata(
            "No eligible observation exists.", definition["canonical_unit"]
        )
        for name, definition in registry.items()
        if definition["predictive"]
    }
    features["industry"] = company.industry
    metadata["industry"] = {
        "status": "fixed_context",
        "source_missing_status": "observed",
        "missing_reason": None,
        "reporting_year": None,
        "publication_date": None,
        "processed_at": None,
        "boundary": None,
        "qualification": "Current company registry context; no historical effective date is asserted.",
        "confidence": None,
        "source_document_id": None,
        "source_url": None,
        "evidence": None,
        "canonical_unit": None,
    }
    by_feature = {}
    for observation in company_observations:
        by_feature.setdefault(observation.feature_name, []).append(observation)
    quarantined = []
    for feature_name, definition in registry.items():
        if feature_name == "industry" or definition["derived_feature_dependencies"]:
            continue
        value, feature_metadata, feature_quarantine = _select_observation(
            by_feature.get(feature_name, []), definition, prediction_as_of
        )
        features[feature_name] = value
        metadata[feature_name] = feature_metadata
        quarantined.extend(feature_quarantine)
    features, metadata = recompute_derived_features(features, metadata)
    return features, metadata, _row_coverage(features, registry), quarantined


def build_dataset_from_records(
    companies: list[CompanyRecord | dict],
    observations: list[FeatureObservation | dict],
    label_records: list[LabelRecord | dict],
) -> dict:
    """Build a dated logical dataset from validated in-memory source records."""
    parsed_companies = [
        item if isinstance(item, CompanyRecord) else CompanyRecord.model_validate(item, strict=False)
        for item in companies
    ]
    parsed_observations = [
        item
        if isinstance(item, FeatureObservation)
        else FeatureObservation.model_validate(item, strict=False)
        for item in observations
    ]
    parsed_labels = [
        item if isinstance(item, LabelRecord) else LabelRecord.model_validate(item, strict=False)
        for item in label_records
    ]
    company_by_cik = {company.company_cik: company for company in parsed_companies}
    if len(company_by_cik) != len(parsed_companies):
        raise ValueError("Company records contain duplicate CIKs.")
    observations_by_company = {}
    for observation in parsed_observations:
        observations_by_company.setdefault(observation.company_cik, []).append(observation)
    labels_by_cycle = {}
    for label in parsed_labels:
        key = (label.company_cik, label.assessment_cycle, label.prediction_as_of)
        targets = labels_by_cycle.setdefault(key, {})
        if label.target_name in targets:
            raise ValueError("Duplicate target label for one company assessment row.")
        targets[label.target_name] = label

    company_rows = []
    feature_rows = []
    label_rows = []
    metadata_rows = []
    unmatched_label_rows = []
    for (company_cik, assessment_cycle, prediction_as_of), labels in sorted(
        labels_by_cycle.items(), key=lambda item: item[0]
    ):
        company = company_by_cik.get(company_cik)
        if company is None:
            unmatched_label_rows.append(
                {
                    "company_cik": company_cik,
                    "assessment_cycle": assessment_cycle,
                    "prediction_as_of": prediction_as_of.isoformat(),
                    "reason": "No current company record matches this authorized label.",
                }
            )
            continue
        row_id = training_row_id(company_cik, assessment_cycle, prediction_as_of)
        features, feature_metadata, row_coverage, quarantined = _build_feature_row(
            company,
            observations_by_company.get(company_cik, []),
            prediction_as_of,
        )
        company_rows.append(
            {
                "row_id": row_id,
                "company_cik": company.company_cik,
                "ticker": company.ticker,
                "company_name": company.company_name,
                "assessment_cycle": assessment_cycle,
                "prediction_as_of": prediction_as_of.isoformat(),
            }
        )
        feature_rows.append(features)
        label_rows.append(
            {
                "row_id": row_id,
                **_label_output(labels.get("esg"), "esg"),
                **_label_output(labels.get("csa"), "csa"),
            }
        )
        metadata_rows.append(
            {
                "row_id": row_id,
                "features": feature_metadata,
                "labels": {
                    target: (
                        labels[target].model_dump(mode="json")
                        if target in labels
                        else None
                    )
                    for target in ("esg", "csa")
                },
                "coverage": row_coverage,
                "quarantined_observations": quarantined,
            }
        )

    registry = feature_registry()
    schema = {
        "version": DATASET_VERSION,
        "feature_registry_version": FEATURE_REGISTRY_VERSION,
        "row_identity": ["company_cik", "assessment_cycle", "prediction_as_of"],
        "row_alignment": (
            "Row N in X, labels, companies, and metadata describes the same company "
            "assessment cycle. X excludes row and company identifiers, labels, and evidence."
        ),
        "numeric_features": numeric_feature_names(),
        "categorical_features": categorical_feature_names(),
        "feature_definitions": registry,
        "feature_groups": {
            category: [
                name
                for name, definition in registry.items()
                if definition["category_or_context"] == category
            ]
            for category in (
                "financial",
                "social",
                "environmental",
                "climate_target",
                "financial_target",
                "fixed_context",
            )
        },
        "labels": {
            "esg": {"column": "esg_score", "range": [0, 100]},
            "csa": {"column": "csa_score", "range": [0, 100]},
        },
        "missing_values": "JSON null and empty CSV cells; a genuine zero remains zero.",
        "timing_policy": "Source publication_date must be on or before prediction_as_of.",
        "preprocessing": "No learned preprocessing is fitted by the dataset builder.",
    }
    coverage = _dataset_coverage(
        company_rows, feature_rows, label_rows, metadata_rows, registry
    )
    audit = {
        "unmatched_authorized_label_rows": unmatched_label_rows,
        "quarantined_observation_count": coverage["quarantined_observation_count"],
        "label_publication_relationship_counts": dict(
            sorted(
                Counter(
                    "published_after_prediction_date"
                    if label.label_published_at > label.prediction_as_of
                    else "published_on_or_before_prediction_date"
                    for label in parsed_labels
                ).items()
            )
        ),
    }
    return {
        "version": DATASET_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "companies": company_rows,
        "X": feature_rows,
        "labels": label_rows,
        "metadata": metadata_rows,
        "schema": schema,
        "registry": registry,
        "coverage": coverage,
        "audit": audit,
    }


def _dataset_coverage(
    company_rows, feature_rows, label_rows, metadata_rows, registry
) -> dict:
    """Summarize feature and label availability without hiding absent categories."""
    feature_coverage = {}
    for feature_name, definition in registry.items():
        values = [row.get(feature_name) for row in feature_rows]
        available_count = sum(value is not None for value in values)
        feature_coverage[feature_name] = {
            "category_or_context": definition["category_or_context"],
            "available_count": available_count,
            "missing_count": len(values) - available_count,
            "coverage_fraction": available_count / len(values) if values else 0,
        }
    category_coverage = {}
    for category in (
        "financial",
        "social",
        "environmental",
        "climate_target",
        "financial_target",
        "fixed_context",
    ):
        names = [
            name
            for name, definition in registry.items()
            if definition["category_or_context"] == category
        ]
        available_value_count = sum(
            row.get(name) is not None for row in feature_rows for name in names
        )
        category_coverage[category] = {
            "feature_count": len(names),
            "available_value_count": available_value_count,
            "missing_value_count": len(feature_rows) * len(names)
            - available_value_count,
            "rows_with_any_value": sum(
                any(row.get(name) is not None for name in names) for row in feature_rows
            ),
        }
    return {
        "row_count": len(feature_rows),
        "company_count": len({row["company_cik"] for row in company_rows}),
        "assessment_cycle_count": len(
            {
                (row["assessment_cycle"], row["prediction_as_of"])
                for row in company_rows
            }
        ),
        "features": feature_coverage,
        "categories": category_coverage,
        "labels": {
            target: {
                "available_count": sum(
                    row[f"{target}_score"] is not None for row in label_rows
                ),
                "missing_count": sum(
                    row[f"{target}_score"] is None for row in label_rows
                ),
            }
            for target in ("esg", "csa")
        },
        "quarantined_observation_count": sum(
            len(row["quarantined_observations"]) for row in metadata_rows
        ),
    }


def build_dataset(settings, labels_path: Path | str) -> dict:
    """Build a logical dated dataset from current sources and authorized labels."""
    companies, observations = load_current_feature_records(settings)
    labels = load_authorized_labels(labels_path)
    dataset = build_dataset_from_records(companies, observations, labels)
    dataset["coverage"]["company_count"] = len(
        {row["company_cik"] for row in dataset["companies"]}
    )
    dataset["coverage"]["assessment_cycle_count"] = len(
        {
            (row["assessment_cycle"], row["prediction_as_of"])
            for row in dataset["companies"]
        }
    )
    return dataset


def build_company_feature_row(
    settings,
    company_id: str,
    prediction_as_of: date | str,
    assessment_cycle: str = "inference",
) -> dict:
    """Build one saved-schema inference row without retraining or label access."""
    prediction_date = _parse_date(prediction_as_of)
    if prediction_date is None:
        raise ValueError("prediction_as_of must be an ISO date.")
    companies, observations = load_current_feature_records(settings)
    normalized_id = company_id.strip().casefold()
    matches = [
        company
        for company in companies
        if company.company_cik == company_id.zfill(10)
        or company.ticker.casefold() == normalized_id
    ]
    if len(matches) != 1:
        raise ValueError("company_id must match exactly one current CIK or ticker.")
    company = matches[0]
    features, metadata, coverage, quarantined = _build_feature_row(
        company,
        [item for item in observations if item.company_cik == company.company_cik],
        prediction_date,
    )
    warnings = sorted({item["reason"] for item in quarantined})
    warnings.append(
        "Industry is current fixed context because no historical effective date is stored."
    )
    return {
        "row_id": training_row_id(
            company.company_cik, assessment_cycle, prediction_date
        ),
        "company": company.model_dump(mode="json"),
        "assessment_cycle": assessment_cycle,
        "prediction_as_of": prediction_date.isoformat(),
        "features": features,
        "metadata": metadata,
        "coverage": coverage,
        "warnings": warnings,
    }


def _snapshot_payloads(dataset: dict) -> dict[str, bytes]:
    """Build every immutable snapshot payload from one logical dataset."""
    return {
        "X.csv": _csv_bytes(predictive_feature_names(), dataset["X"]),
        "labels.csv": _csv_bytes(LABEL_COLUMNS, dataset["labels"]),
        "companies.csv": _csv_bytes(COMPANY_COLUMNS, dataset["companies"]),
        "metadata.json": _json_bytes(
            {
                "version": DATASET_VERSION,
                "row_alignment": dataset["schema"]["row_alignment"],
                "rows": dataset["metadata"],
            }
        ),
        "schema.json": _json_bytes(dataset["schema"]),
        "registry.json": _json_bytes(
            {"version": FEATURE_REGISTRY_VERSION, "features": dataset["registry"]}
        ),
        "coverage.json": _json_bytes(dataset["coverage"]),
        "audit.json": _json_bytes(dataset["audit"]),
    }


def _file_description(body: bytes) -> dict:
    """Return the content identity stored in the snapshot manifest."""
    return {"sha256": hashlib.sha256(body).hexdigest(), "byte_count": len(body)}


def _verify_snapshot_files(directory: Path, manifest: dict) -> None:
    """Fail when any immutable snapshot artifact changed after publication."""
    for name, expected in manifest["files"].items():
        body = (directory / name).read_bytes()
        if _file_description(body) != expected:
            raise ValueError("Dataset snapshot content changed: " + name)


def export_dataset_snapshot(dataset: dict, output_dir: Path | str) -> dict:
    """Atomically publish a content-addressed dated dataset snapshot."""
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ValueError("Dataset output directory must not be a symlink.")
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir = output_dir / "snapshots"
    snapshots_dir.mkdir(exist_ok=True)
    payloads = _snapshot_payloads(dataset)
    file_descriptions = {
        name: _file_description(body) for name, body in payloads.items()
    }
    snapshot_id = hashlib.sha256(_json_bytes(file_descriptions)).hexdigest()
    manifest = {
        "version": DATASET_VERSION,
        "snapshot_id": snapshot_id,
        "dataset_hash": snapshot_id,
        "generated_at": dataset["generated_at"],
        "row_count": len(dataset["X"]),
        "numeric_feature_count": len(numeric_feature_names()),
        "categorical_feature_count": len(categorical_feature_names()),
        "label_available_counts": {
            target: dataset["coverage"]["labels"][target]["available_count"]
            for target in ("esg", "csa")
        },
        "files": file_descriptions,
    }
    manifest_body = _json_bytes(manifest)
    final_directory = snapshots_dir / snapshot_id
    if final_directory.exists():
        existing_manifest = json.loads((final_directory / "manifest.json").read_bytes())
        if existing_manifest.get("snapshot_id") != snapshot_id:
            raise ValueError("Existing dataset snapshot has the wrong identity.")
        _verify_snapshot_files(final_directory, existing_manifest)
        manifest = existing_manifest
        manifest_body = (final_directory / "manifest.json").read_bytes()
    else:
        temporary_directory = snapshots_dir / (
            "." + snapshot_id + f".{os.getpid()}.{time.time_ns()}.tmp"
        )
        temporary_directory.mkdir()
        try:
            for name, body in payloads.items():
                (temporary_directory / name).write_bytes(body)
            (temporary_directory / "manifest.json").write_bytes(manifest_body)
            temporary_directory.replace(final_directory)
        finally:
            if temporary_directory.exists():
                shutil.rmtree(temporary_directory)
    latest = {
        "version": DATASET_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_path": "snapshots/" + snapshot_id,
        "manifest_sha256": hashlib.sha256(manifest_body).hexdigest(),
        "generated_at": manifest["generated_at"],
    }
    temporary_latest = output_dir / f".latest.{os.getpid()}.tmp"
    temporary_latest.write_bytes(_json_bytes(latest))
    temporary_latest.replace(output_dir / "latest.json")
    return {
        "snapshot_dir": str(final_directory),
        "snapshot_id": snapshot_id,
        "row_count": manifest["row_count"],
        "numeric_feature_count": manifest["numeric_feature_count"],
        "categorical_feature_count": manifest["categorical_feature_count"],
        "label_available_counts": manifest["label_available_counts"],
    }


def build_dataset_snapshot(
    settings, labels_path: Path | str, output_dir: Path | str
) -> dict:
    """Build and publish one authorized dated dataset in a single call."""
    return export_dataset_snapshot(build_dataset(settings, labels_path), output_dir)


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read one snapshot CSV as rows after manifest verification."""
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _read_label_csv(path: Path) -> list[dict]:
    """Restore nullable score types and provenance from the aligned label CSV."""
    rows = _read_csv(path)
    parsed_rows = []
    for row in rows:
        parsed = {
            key: (None if value == "" else value) for key, value in row.items()
        }
        for target in ("esg", "csa"):
            score_name = target + "_score"
            if parsed[score_name] is not None:
                parsed[score_name] = float(parsed[score_name])
        parsed_rows.append(parsed)
    return parsed_rows


def load_dataset_snapshot(path: Path | str) -> dict:
    """Load and verify either a snapshot directory or its latest-pointer parent."""
    path = Path(path)
    if (path / "latest.json").exists():
        latest_body = (path / "latest.json").read_bytes()
        latest = json.loads(latest_body)
        path = path / latest["snapshot_path"]
        manifest_body = (path / "manifest.json").read_bytes()
        if hashlib.sha256(manifest_body).hexdigest() != latest["manifest_sha256"]:
            raise ValueError("Dataset latest pointer does not match its manifest.")
    manifest = json.loads((path / "manifest.json").read_bytes())
    _verify_snapshot_files(path, manifest)
    schema = json.loads((path / "schema.json").read_bytes())
    registry_document = json.loads((path / "registry.json").read_bytes())
    raw_features = _read_csv(path / "X.csv")
    features = []
    for row in raw_features:
        parsed = {}
        for name, value in row.items():
            definition = registry_document["features"][name]
            if value == "":
                parsed[name] = None
            elif definition["categorical"]:
                parsed[name] = value
            elif definition["data_type"] == "boolean":
                if value not in {"0", "1"}:
                    raise ValueError("Boolean feature CSV values must use 0 or 1.")
                parsed[name] = value == "1"
            elif definition["data_type"] == "integer":
                parsed[name] = int(value)
            else:
                parsed[name] = float(value)
        features.append(parsed)
    return {
        "snapshot_id": manifest["snapshot_id"],
        "companies": _read_csv(path / "companies.csv"),
        "X": features,
        "labels": _read_label_csv(path / "labels.csv"),
        "metadata": json.loads((path / "metadata.json").read_bytes()),
        "schema": schema,
        "registry": registry_document["features"],
        "coverage": json.loads((path / "coverage.json").read_bytes()),
        "audit": json.loads((path / "audit.json").read_bytes()),
        "manifest": manifest,
    }
