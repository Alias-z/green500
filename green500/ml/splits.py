"""Create persisted target-specific company or chronological evaluation splits."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any

from sklearn.model_selection import GroupKFold, GroupShuffleSplit

VERSION = "green500-split-assignments-v1"
TARGET_COLUMNS = {"esg": "esg_score", "csa": "csa_score"}


class SplitUnavailable(ValueError):
    """The valid rows cannot support the requested evaluation design."""


def _iso_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO date.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date.") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD.")
    return parsed


def _finite_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        return None
    return score


def _aligned_records(
    dataset: dict[str, Any], target: str
) -> tuple[list[dict], dict[str, int]]:
    if target not in TARGET_COLUMNS:
        raise ValueError(f"Unsupported target: {target!r}.")
    try:
        companies = dataset["companies"]
        labels = dataset["labels"]
        features = dataset["X"]
    except KeyError as error:
        raise ValueError(
            f"Dataset is missing an aligned table: {error.args[0]}"
        ) from error
    if not all(isinstance(rows, list) for rows in (companies, labels, features)):
        raise ValueError("Dataset aligned tables must be lists.")
    if len({len(companies), len(labels), len(features)}) != 1:
        raise ValueError("Dataset aligned tables have different row counts.")

    label_column = TARGET_COLUMNS[target]
    publication_column = f"{target}_label_published_at"
    authorization_column = f"{target}_authorization_reference"
    records = []
    excluded = {"missing_or_invalid_label": 0, "missing_label_provenance": 0}
    seen = set()
    for company, label, feature_row in zip(companies, labels, features, strict=True):
        if not all(isinstance(row, dict) for row in (company, label, feature_row)):
            raise ValueError("Dataset aligned rows must be objects.")
        row_id = company.get("row_id")
        if not isinstance(row_id, str) or not row_id or row_id in seen:
            raise ValueError("Every company row needs one unique row_id.")
        seen.add(row_id)
        if label.get("row_id") != row_id:
            raise ValueError("Label row_id differs from its company row_id.")
        score = _finite_score(label.get(label_column))
        if score is None:
            excluded["missing_or_invalid_label"] += 1
            continue
        publication = label.get(publication_column)
        authorization = label.get(authorization_column)
        try:
            publication_date = _iso_date(publication, publication_column)
            prediction_as_of = _iso_date(
                company.get("prediction_as_of"), "prediction_as_of"
            )
        except (TypeError, ValueError):
            excluded["missing_label_provenance"] += 1
            continue
        if not isinstance(authorization, str) or not authorization.strip():
            excluded["missing_label_provenance"] += 1
            continue
        cik = company.get("company_cik")
        cycle = company.get("assessment_cycle")
        if (
            not isinstance(cik, str)
            or not cik
            or not isinstance(cycle, str)
            or not cycle
        ):
            excluded["missing_label_provenance"] += 1
            continue
        records.append(
            {
                "row_id": row_id,
                "company_cik": cik,
                "assessment_cycle": cycle,
                "prediction_as_of": prediction_as_of,
                "label_published_at": publication_date,
                "score": score,
            }
        )
    return sorted(records, key=lambda row: row["row_id"]), excluded


def _base(
    dataset: dict[str, Any], target: str, records: list[dict], excluded: dict, seed: int
) -> dict:
    snapshot_id = dataset.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("Dataset has no snapshot_id.")
    return {
        "version": VERSION,
        "target": target,
        "target_column": TARGET_COLUMNS[target],
        "dataset_snapshot_id": snapshot_id,
        "seed": seed,
        "group_column": "company_cik",
        "eligible_row_ids": [row["row_id"] for row in records],
        "excluded_row_counts": excluded,
    }


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    stable = json.dumps(
        result, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    result["split_id"] = hashlib.sha256(stable).hexdigest()
    return result


def _snapshot_assignments(
    dataset: dict[str, Any],
    target: str,
    records: list[dict],
    excluded: dict,
    config: dict,
) -> dict[str, Any]:
    seed = config["seed"]
    settings = config["splits"]["snapshot"]
    groups = [row["company_cik"] for row in records]
    unique_groups = set(groups)
    if len(unique_groups) < settings["validation_folds"] + 1:
        raise SplitUnavailable(
            "Snapshot estimation needs at least four eligible company groups."
        )
    splitter = GroupShuffleSplit(
        n_splits=1,
        test_size=settings["final_test_fraction"],
        random_state=seed,
    )
    development_indexes, test_indexes = next(splitter.split(records, groups=groups))
    development = [records[index] for index in development_indexes]
    final_test = [records[index] for index in test_indexes]
    development_groups = [row["company_cik"] for row in development]
    if len(set(development_groups)) < settings["validation_folds"]:
        raise SplitUnavailable(
            "Development data cannot support three company-group folds."
        )
    folds = []
    group_kfold = GroupKFold(n_splits=settings["validation_folds"])
    for fold_index, (train_indexes, validation_indexes) in enumerate(
        group_kfold.split(development, groups=development_groups), start=1
    ):
        folds.append(
            {
                "fold_id": f"fold_{fold_index}",
                "train_row_ids": [
                    development[index]["row_id"] for index in train_indexes
                ],
                "validation_row_ids": [
                    development[index]["row_id"] for index in validation_indexes
                ],
            }
        )
    result = _base(dataset, target, records, excluded, seed)
    result.update(
        {
            "mode": "snapshot_estimation",
            "description": "Held-out-company score estimation.",
            "development_row_ids": [row["row_id"] for row in development],
            "final_test_row_ids": [row["row_id"] for row in final_test],
            "folds": folds,
        }
    )
    return _finish(result)


def _historical_assignments(
    dataset: dict[str, Any],
    target: str,
    records: list[dict],
    excluded: dict,
    config: dict,
) -> dict[str, Any]:
    forecast_records = [
        row for row in records if row["prediction_as_of"] < row["label_published_at"]
    ]
    cycles = sorted(
        {row["assessment_cycle"] for row in forecast_records},
        key=lambda cycle: min(
            row["prediction_as_of"]
            for row in forecast_records
            if row["assessment_cycle"] == cycle
        ),
    )
    if len(cycles) < 3:
        raise SplitUnavailable(
            "Historical forecasting needs at least three dated assessment cycles."
        )
    final_cycle = cycles[-1]
    final_test = [
        row for row in forecast_records if row["assessment_cycle"] == final_cycle
    ]
    final_cutoff = min(row["prediction_as_of"] for row in final_test)
    earlier_cycles = set(cycles[:-1])
    development = [
        row
        for row in forecast_records
        if row["assessment_cycle"] in earlier_cycles
        and row["label_published_at"] <= final_cutoff
    ]
    available_validation_cycles = []
    for cycle_index, cycle in enumerate(cycles[:-1]):
        validation = [row for row in development if row["assessment_cycle"] == cycle]
        if not validation:
            continue
        cutoff = min(row["prediction_as_of"] for row in validation)
        prior_cycles = set(cycles[:cycle_index])
        training = [
            row
            for row in development
            if row["assessment_cycle"] in prior_cycles
            and row["label_published_at"] <= cutoff
        ]
        if training:
            available_validation_cycles.append((cycle, cutoff, training, validation))
    maximum = config["splits"]["historical"]["maximum_validation_folds"]
    selected = available_validation_cycles[-maximum:]
    if not selected:
        raise SplitUnavailable(
            "Historical data has no valid expanding-window validation fold."
        )
    folds = [
        {
            "fold_id": f"fold_{index}",
            "validation_cycle": cycle,
            "cutoff": cutoff.isoformat(),
            "train_row_ids": [row["row_id"] for row in training],
            "validation_row_ids": [row["row_id"] for row in validation],
        }
        for index, (cycle, cutoff, training, validation) in enumerate(selected, start=1)
    ]
    result = _base(dataset, target, forecast_records, excluded, config["seed"])
    result.update(
        {
            "mode": "historical_forecasting",
            "description": "Chronological forecasting with expanding-window validation.",
            "final_test_cycle": final_cycle,
            "final_test_cutoff": final_cutoff.isoformat(),
            "development_row_ids": [row["row_id"] for row in development],
            "final_test_row_ids": [row["row_id"] for row in final_test],
            "folds": folds,
        }
    )
    return _finish(result)


def build_split_assignments(
    dataset: dict[str, Any],
    target: str,
    config: dict[str, Any],
    mode: str | None = None,
) -> dict[str, Any]:
    """Build one split that both model families must consume without modification."""
    records, excluded = _aligned_records(dataset, target)
    if not records:
        raise SplitUnavailable(
            f"No authorized, dated {target.upper()} labels are eligible."
        )
    requested = mode or config["splits"]["mode"]
    if requested not in {"auto", "snapshot", "historical"}:
        raise ValueError("Split mode must be auto, snapshot, or historical.")
    if requested == "auto":
        chronological_cycles = {
            row["assessment_cycle"]
            for row in records
            if row["prediction_as_of"] < row["label_published_at"]
        }
        requested = "historical" if len(chronological_cycles) >= 3 else "snapshot"
    if requested == "historical":
        return _historical_assignments(dataset, target, records, excluded, config)
    return _snapshot_assignments(dataset, target, records, excluded, config)
