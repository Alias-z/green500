"""Build aligned, immutable inputs for the approved nonlinear model candidates."""

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from green500 import db
from green500.feature_catalog import feature_catalog
from green500.processing.files import save_json
from green500.score_catalog import empty_score, load_scores

VERSION = "green500-training-data-v1"
LABELS = ("esg_score", "csa_score")
CATEGORICAL_FEATURES = ("sector",)
GROUP_BY_CATEGORY = {
    "financial": "financial_resilience",
    "financial_targets": "financial_resilience",
    "environment_report": "environment",
    "climate_targets": "environment",
    "social_employee": "social",
}
SNAPSHOT_FILES = (
    "X.csv",
    "labels.csv",
    "companies.csv",
    "metadata.json",
    "schema.json",
    "coverage.json",
)


def _json_bytes(value):
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


def _csv_bytes(fieldnames, rows):
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


def _field_metadata(feature, value, company_metadata, specification):
    """Describe availability without turning collection failure into missing disclosure."""
    category = specification["category"]
    category_metadata = company_metadata.get(category) or {}
    fields = category_metadata.get("fields") or {}
    details = fields.get(feature) or {}
    category_status = category_metadata.get("status")
    if category == "financial":
        category_status = "completed" if category_metadata else "not_available"
    if details:
        status = details.get("status") or (
            "available" if value is not None else "not_disclosed"
        )
        year = details.get("reporting_year", details.get("fiscal_year"))
        reason = details.get("reason") or details.get("notes")
        qualification = details.get("qualification")
    else:
        status = category_status or "not_started"
        year = category_metadata.get("reporting_year")
        reason = (
            "The category has no validated extraction result."
            if category_status not in {"completed", None}
            else "The validated result does not contain this feature."
        )
        qualification = None
    return {
        "status": status,
        "reporting_year": year,
        "reason": reason,
        "qualification": qualification,
        "source_document_id": category_metadata.get("source_document_id"),
        "processed_at": category_metadata.get("processed_at"),
    }


def _category_metadata(company_metadata, category):
    details = company_metadata.get(category) or {}
    status = details.get("status")
    if category == "financial":
        status = "completed" if details else "not_available"
    return {
        "status": status or "not_started",
        "reporting_year": details.get("reporting_year"),
        "source_document_id": details.get("source_document_id"),
        "processed_at": details.get("processed_at"),
        "boundary": details.get("boundary"),
        "limitations": details.get("limitations") or [],
    }


def _coverage(features, feature_rows, metadata_rows, labels):
    """Summarize values separately from extraction and disclosure statuses."""
    feature_coverage = {}
    for feature, specification in features.items():
        values = [row[feature] for row in feature_rows]
        statuses = Counter(row["features"][feature]["status"] for row in metadata_rows)
        years = Counter(
            str(year)
            for row in metadata_rows
            if (year := row["features"][feature]["reporting_year"]) is not None
        )
        available = sum(value is not None for value in values)
        feature_coverage[feature] = {
            "category": specification["category"],
            "group": GROUP_BY_CATEGORY[specification["category"]],
            "available_count": available,
            "missing_count": len(values) - available,
            "coverage_fraction": available / len(values) if values else 0,
            "status_counts": dict(sorted(statuses.items())),
            "reporting_year_counts": dict(sorted(years.items())),
        }
    category_coverage = {}
    for category, group in GROUP_BY_CATEGORY.items():
        category_features = [
            feature
            for feature, specification in features.items()
            if specification["category"] == category
        ]
        statuses = Counter(row["categories"][category]["status"] for row in metadata_rows)
        years = Counter(
            str(year)
            for row in metadata_rows
            if (year := row["categories"][category]["reporting_year"]) is not None
        )
        non_null_cells = sum(
            row[feature] is not None
            for row in feature_rows
            for feature in category_features
        )
        category_coverage[category] = {
            "group": group,
            "feature_count": len(category_features),
            "available_value_count": non_null_cells,
            "missing_value_count": len(feature_rows) * len(category_features)
            - non_null_cells,
            "company_status_counts": dict(sorted(statuses.items())),
            "reporting_year_counts": dict(sorted(years.items())),
            "companies_with_any_value": sum(
                any(row[feature] is not None for feature in category_features)
                for row in feature_rows
            ),
        }
    return {
        "row_count": len(feature_rows),
        "features": feature_coverage,
        "categories": category_coverage,
        "labels": {
            label: {
                "available_count": sum(row[label] is not None for row in labels),
                "missing_count": sum(row[label] is None for row in labels),
            }
            for label in LABELS
        },
    }


def _snapshot_id(value):
    stable = {
        key: value[key]
        for key in ("companies", "X", "labels", "metadata", "schema", "coverage")
    }
    return hashlib.sha256(_json_bytes(stable)).hexdigest()


def build_training_bundle(settings):
    """Build one row-aligned bundle without imputation, scaling, or feature selection."""
    catalog = feature_catalog(settings)
    features = catalog["schema"]["fields"]
    with db.connect(settings) as connection:
        sectors = {
            row["cik"]: (
                row["sector"].strip()
                if isinstance(row["sector"], str) and row["sector"].strip()
                else "Unknown"
            )
            for row in connection.execute(
                "SELECT cik,sector FROM companies WHERE is_current"
            ).fetchall()
        }
    scores, score_updated_at = load_scores()
    companies = []
    feature_rows = []
    labels = []
    metadata_rows = []
    for row in catalog["rows"]:
        cik = row["company_cik"]
        company_metadata = catalog["metadata"].get(cik, {})
        companies.append(
            {
                "company_cik": cik,
                "ticker": row["ticker"],
                "company_name": row["company_name"],
            }
        )
        feature_rows.append(
            {
                "sector": sectors.get(cik, "Unknown"),
                **{feature: row[feature] for feature in features},
            }
        )
        score = scores.get(cik, empty_score())
        is_available = score["status"] == "available"
        labels.append(
            {
                "esg_score": score["esg_score"] if is_available else None,
                "csa_score": score["csa_score"] if is_available else None,
            }
        )
        metadata_rows.append(
            {
                "categories": {
                    category: _category_metadata(company_metadata, category)
                    for category in GROUP_BY_CATEGORY
                },
                "features": {
                    feature: _field_metadata(
                        feature, row[feature], company_metadata, specification
                    )
                    for feature, specification in features.items()
                },
                "label": {
                    "status": score["status"],
                    "last_updated": score.get("last_updated"),
                    "assessment_year": score.get("assessment_year"),
                    "source_sha256": score.get("source_sha256"),
                },
            }
        )
    feature_groups = {
        group: [
            feature
            for feature, specification in features.items()
            if GROUP_BY_CATEGORY[specification["category"]] == group
        ]
        for group in ("environment", "social", "financial_resilience")
    }
    schema = {
        "version": VERSION,
        "row_alignment": (
            "Row N in X, labels, companies, and metadata describes the same company. "
            "X contains no company identifier or score-provider field."
        ),
        "numeric_features": list(features),
        "categorical_features": list(CATEGORICAL_FEATURES),
        "feature_definitions": features,
        "feature_groups": feature_groups,
        "labels": {
            "esg_score": {"type": "number", "range": [0, 100]},
            "csa_score": {"type": "number", "range": [0, 100]},
        },
        "missing_values": "JSON null and empty CSV cells. Zero remains a disclosed zero.",
        "preprocessing": (
            "None. Imputation, scaling, encoding, and feature selection must be fitted "
            "inside each training fold."
        ),
        "score_snapshot_updated_at": score_updated_at,
    }
    coverage = _coverage(features, feature_rows, metadata_rows, labels)
    bundle = {
        "version": VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "companies": companies,
        "X": feature_rows,
        "labels": labels,
        "metadata": metadata_rows,
        "schema": schema,
        "coverage": coverage,
    }
    bundle["snapshot_id"] = _snapshot_id(bundle)
    return bundle


def _snapshot_payloads(bundle):
    feature_names = bundle["schema"]["numeric_features"]
    return {
        "X.csv": _csv_bytes([*CATEGORICAL_FEATURES, *feature_names], bundle["X"]),
        "labels.csv": _csv_bytes(LABELS, bundle["labels"]),
        "companies.csv": _csv_bytes(
            ("company_cik", "ticker", "company_name"), bundle["companies"]
        ),
        "metadata.json": _json_bytes(
            {
                "row_alignment": bundle["schema"]["row_alignment"],
                "rows": bundle["metadata"],
            }
        ),
        "schema.json": _json_bytes(bundle["schema"]),
        "coverage.json": _json_bytes(bundle["coverage"]),
    }


def _file_description(body):
    return {"sha256": hashlib.sha256(body).hexdigest(), "byte_count": len(body)}


def _verify_existing_snapshot(directory, manifest):
    for name, expected in manifest["files"].items():
        path = directory / name
        body = path.read_bytes()
        if _file_description(body) != expected:
            raise ValueError("Existing training snapshot content changed: " + name)


def export_training_bundle(settings, directory):
    """Atomically publish one content-addressed snapshot and a small latest pointer."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Training export directory must not be a symlink.")
    directory.mkdir(parents=True, exist_ok=True)
    snapshots = directory / "snapshots"
    snapshots.mkdir(exist_ok=True)
    bundle = build_training_bundle(settings)
    payloads = _snapshot_payloads(bundle)
    manifest = {
        "version": VERSION,
        "snapshot_id": bundle["snapshot_id"],
        "generated_at": bundle["generated_at"],
        "row_count": len(bundle["X"]),
        "numeric_feature_count": len(bundle["schema"]["numeric_features"]),
        "categorical_feature_count": len(CATEGORICAL_FEATURES),
        "label_available_counts": {
            label: bundle["coverage"]["labels"][label]["available_count"]
            for label in LABELS
        },
        "files": {name: _file_description(body) for name, body in payloads.items()},
    }
    manifest_body = _json_bytes(manifest)
    final_directory = snapshots / bundle["snapshot_id"]
    if final_directory.exists():
        existing = json.loads((final_directory / "manifest.json").read_bytes())
        if existing.get("snapshot_id") != bundle["snapshot_id"]:
            raise ValueError("Existing training snapshot has the wrong identity.")
        _verify_existing_snapshot(final_directory, existing)
        manifest = existing
        manifest_body = (final_directory / "manifest.json").read_bytes()
    else:
        temporary = snapshots / (
            "." + bundle["snapshot_id"] + f".{os.getpid()}.{time.time_ns()}.tmp"
        )
        temporary.mkdir()
        try:
            for name, body in payloads.items():
                (temporary / name).write_bytes(body)
            (temporary / "manifest.json").write_bytes(manifest_body)
            try:
                temporary.replace(final_directory)
            except OSError:
                if not final_directory.exists():
                    raise
                existing = json.loads(
                    (final_directory / "manifest.json").read_bytes()
                )
                if existing.get("snapshot_id") != bundle["snapshot_id"]:
                    raise ValueError(
                        "Concurrent training snapshot has the wrong identity."
                    )
                _verify_existing_snapshot(final_directory, existing)
                manifest = existing
                manifest_body = (final_directory / "manifest.json").read_bytes()
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    latest = {
        "version": VERSION,
        "snapshot_id": bundle["snapshot_id"],
        "snapshot_path": "snapshots/" + bundle["snapshot_id"],
        "manifest_sha256": hashlib.sha256(manifest_body).hexdigest(),
        "generated_at": manifest["generated_at"],
    }
    save_json(directory / "latest.json", latest)
    return {
        "snapshot_dir": str(final_directory),
        "snapshot_id": bundle["snapshot_id"],
        "row_count": manifest["row_count"],
        "numeric_feature_count": manifest["numeric_feature_count"],
        "categorical_feature_count": manifest["categorical_feature_count"],
        "label_available_counts": manifest["label_available_counts"],
    }


def main():
    from green500.config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    settings = load_settings()
    result = export_training_bundle(
        settings, args.output_dir or settings.data_dir / "ml" / "training"
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
