"""Train actual EBM and CatBoost regressors on one frozen Green500 dataset."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import shutil
import time
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, date, datetime
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from interpret.glassbox import ExplainableBoostingRegressor

from green500.ml.artifacts import (
    RUN_MANIFEST_VERSION,
    canonical_json_bytes,
    file_description,
    publish_run_directory,
    save_model_artifact,
    write_json_atomic,
    write_json_lines_atomic,
    write_latest_run_pointer,
    write_run_manifest,
)
from green500.ml.config import load_ml_config, validate_ml_config
from green500.ml.feature_selection import (
    FEATURE_SET_ORDER,
    apply_feature_set,
    choose_feature_set,
    fit_feature_mask,
)
from green500.ml.splits import TARGET_COLUMNS, SplitUnavailable, build_split_assignments

TRAINING_VERSION = "green500-training-v1"
MODEL_FAMILIES = ("ebm", "catboost")
TARGETS = ("esg", "csa")


def _dataset_directory(path: str | Path) -> Path:
    """Resolve a snapshot directory or its parent latest pointer without guessing."""
    path = Path(path)
    if (path / "manifest.json").is_file():
        return path
    latest_path = path / "latest.json"
    if not latest_path.is_file() or latest_path.is_symlink():
        raise FileNotFoundError(
            f"Dataset snapshot manifest does not exist beneath: {path}"
        )
    try:
        latest = json.loads(latest_path.read_bytes())
        relative = latest["snapshot_path"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("Dataset latest pointer is invalid.") from error
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError(
            "Dataset latest pointer must contain a relative snapshot path."
        )
    resolved = (path / relative).resolve()
    try:
        resolved.relative_to(path.resolve())
    except ValueError as error:
        raise ValueError("Dataset latest pointer escapes its directory.") from error
    if not (resolved / "manifest.json").is_file():
        raise FileNotFoundError("Dataset latest pointer names a missing snapshot.")
    return resolved


def _load_dataset(path: str | Path) -> tuple[dict[str, Any], Path]:
    """Use Agent A's public loader and retain the exact hash-bound directory."""
    from green500.ml.dataset import load_dataset_snapshot

    directory = _dataset_directory(path)
    dataset = load_dataset_snapshot(directory)
    if not isinstance(dataset, dict):
        raise TypeError("Dataset loader returned a non-object.")
    if "snapshot_id" not in dataset:
        manifest = dataset.get("manifest")
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("snapshot_id"), str
        ):
            raise ValueError("Dataset loader returned no snapshot identifier.")
        dataset["snapshot_id"] = manifest["snapshot_id"]
    return dataset, directory


def _config(config: str | Path | dict[str, Any]) -> dict[str, Any]:
    return (
        validate_ml_config(config)
        if isinstance(config, dict)
        else load_ml_config(config)
    )


def _package_versions() -> dict[str, str]:
    from green500 import __version__ as green500_version

    return {
        "green500": green500_version,
        **{
            package: package_version(package)
            for package in (
                "interpret",
                "interpret-core",
                "catboost",
                "scikit-learn",
                "numpy",
                "pandas",
            )
        },
    }


def model_configuration_grid(
    config: dict[str, Any], family: str
) -> list[dict[str, Any]]:
    """Return the exact two-by-two search in stable configuration order."""
    if family not in MODEL_FAMILIES:
        raise ValueError(f"Unsupported model family: {family!r}.")
    definition = config["training"]["models"][family]
    search_names = list(definition["search"])
    grid = []
    for index, values in enumerate(
        itertools.product(*(definition["search"][name] for name in search_names)),
        start=1,
    ):
        parameters = {
            **definition["fixed"],
            **dict(zip(search_names, values, strict=True)),
        }
        grid.append(
            {
                "configuration_id": f"{family}_{index}",
                "parameters": parameters,
            }
        )
    if len(grid) != 4:
        raise ValueError(f"{family} search must contain exactly four configurations.")
    return grid


def _feature_contract(
    dataset: dict[str, Any],
) -> tuple[list[str], list[str], dict[str, str]]:
    schema = dataset.get("schema")
    if not isinstance(schema, dict):
        raise TypeError("Dataset has no schema object.")
    numeric = schema.get("numeric_features")
    categorical = schema.get("categorical_features")
    definitions = schema.get("feature_definitions")
    if (
        not isinstance(numeric, list)
        or not isinstance(categorical, list)
        or not isinstance(definitions, dict)
    ):
        raise TypeError("Dataset schema has invalid feature declarations.")
    ordered = [*numeric, *categorical]
    if not ordered or len(set(ordered)) != len(ordered):
        raise ValueError("Dataset feature names must be unique and non-empty.")
    categories = {}
    allowed = {
        "financial",
        "social",
        "environmental",
        "climate_target",
        "financial_target",
        "fixed_context",
    }
    for feature in ordered:
        definition = definitions.get(feature)
        if not isinstance(feature, str) or not isinstance(definition, dict):
            raise TypeError(f"Dataset has no definition for feature: {feature!r}.")
        category = definition.get("category_or_context", definition.get("category"))
        if category not in allowed:
            raise ValueError(
                f"Feature {feature} has an unsupported category: {category!r}."
            )
        categories[feature] = category
    return ordered, categorical, categories


def _aligned_rows(
    dataset: dict[str, Any], ordered_features: list[str]
) -> dict[str, dict[str, Any]]:
    try:
        companies = dataset["companies"]
        features = dataset["X"]
        labels = dataset["labels"]
        metadata = dataset["metadata"]
    except KeyError as error:
        raise ValueError(
            f"Dataset is missing aligned content: {error.args[0]}"
        ) from error
    if isinstance(metadata, dict):
        metadata = metadata.get("rows")
    if not all(
        isinstance(rows, list) for rows in (companies, features, labels, metadata)
    ):
        raise ValueError("Dataset aligned content must be lists.")
    if len({len(companies), len(features), len(labels), len(metadata)}) != 1:
        raise ValueError("Dataset aligned content has different row counts.")
    aligned = {}
    for company, feature_row, label_row, metadata_row in zip(
        companies, features, labels, metadata, strict=True
    ):
        row_id = company.get("row_id") if isinstance(company, dict) else None
        if not isinstance(row_id, str) or not row_id or row_id in aligned:
            raise ValueError("Company rows must have unique row identifiers.")
        if not isinstance(label_row, dict) or label_row.get("row_id") != row_id:
            raise ValueError("Label row identity differs from the company row.")
        if not isinstance(feature_row, dict) or set(ordered_features) - set(
            feature_row
        ):
            raise ValueError(f"Feature row is incomplete: {row_id}")
        if not isinstance(metadata_row, dict) or metadata_row.get("row_id") != row_id:
            raise ValueError("Metadata row identity differs from the company row.")
        aligned[row_id] = {
            "company": company,
            "X": feature_row,
            "labels": label_row,
            "metadata": metadata_row,
        }
    return aligned


def _frame(
    row_ids: list[str], aligned: dict, ordered: list[str], categorical: list[str]
) -> pd.DataFrame:
    frame = pd.DataFrame([aligned[row_id]["X"] for row_id in row_ids], columns=ordered)
    categorical_set = set(categorical)
    for feature in ordered:
        if feature in categorical_set:
            frame[feature] = frame[feature].map(
                lambda value: (
                    value.strip()
                    if isinstance(value, str) and value.strip()
                    else "Unknown"
                )
            )
            continue
        converted = pd.to_numeric(frame[feature], errors="coerce")
        invalid = frame[feature].notna() & converted.isna()
        if invalid.any():
            raise ValueError(f"Numeric feature contains non-numeric values: {feature}")
        frame[feature] = converted.astype(float)
    return frame


def _scores(row_ids: list[str], aligned: dict, target: str) -> np.ndarray:
    values = [aligned[row_id]["labels"][TARGET_COLUMNS[target]] for row_id in row_ids]
    scores = np.asarray(values, dtype=float)
    if not np.isfinite(scores).all():
        raise ValueError(
            f"Split contains a missing or non-finite {target.upper()} label."
        )
    return scores


def _new_model(
    family: str, parameters: dict[str, Any], ordered: list[str], categorical: list[str]
):
    if family == "ebm":
        feature_types = [
            "nominal" if feature in set(categorical) else "continuous"
            for feature in ordered
        ]
        return ExplainableBoostingRegressor(
            feature_names=ordered,
            feature_types=feature_types,
            **parameters,
        )
    if family == "catboost":
        return CatBoostRegressor(**parameters)
    raise ValueError(f"Unsupported model family: {family!r}.")


def _fit_model(
    model, family: str, X: pd.DataFrame, y: np.ndarray, categorical: list[str]
) -> None:
    if family == "catboost":
        model.fit(X, y, cat_features=categorical)
    else:
        model.fit(X, y)


def _mae(
    actual: list[float] | np.ndarray, predicted: list[float] | np.ndarray
) -> float:
    return float(
        np.mean(
            np.abs(np.asarray(actual, dtype=float) - np.asarray(predicted, dtype=float))
        )
    )


def _rmse(
    actual: list[float] | np.ndarray, predicted: list[float] | np.ndarray
) -> float:
    difference = np.asarray(actual, dtype=float) - np.asarray(predicted, dtype=float)
    return float(np.sqrt(np.mean(np.square(difference))))


def _industry(row_id: str, aligned: dict, categorical: list[str]) -> str:
    industry_feature = "industry" if "industry" in categorical else categorical[0]
    value = aligned[row_id]["X"].get(industry_feature)
    return value.strip() if isinstance(value, str) and value.strip() else "Unknown"


def _previous_score(
    evaluation_row_id: str,
    allowed_training_ids: set[str],
    aligned: dict,
    target: str,
) -> float | None:
    current = aligned[evaluation_row_id]
    company = current["company"]
    prediction_as_of = date.fromisoformat(company["prediction_as_of"])
    candidates = []
    publication_name = f"{target}_label_published_at"
    for row_id in allowed_training_ids:
        row = aligned[row_id]
        if row["company"]["company_cik"] != company["company_cik"]:
            continue
        published = row["labels"].get(publication_name)
        score = row["labels"].get(TARGET_COLUMNS[target])
        if (
            not isinstance(published, str)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
        ):
            continue
        publication_date = date.fromisoformat(published)
        if publication_date <= prediction_as_of:
            candidates.append(
                (publication_date, row["company"]["assessment_cycle"], float(score))
            )
    return max(candidates)[2] if candidates else None


def _baseline_predictions(
    training_ids: list[str],
    evaluation_ids: list[str],
    aligned: dict,
    target: str,
    categorical: list[str],
) -> list[dict[str, Any]]:
    training_scores = _scores(training_ids, aligned, target)
    global_median = float(np.median(training_scores))
    by_industry: dict[str, list[float]] = defaultdict(list)
    for row_id, score in zip(training_ids, training_scores, strict=True):
        by_industry[_industry(row_id, aligned, categorical)].append(float(score))
    industry_medians = {
        name: float(np.median(values)) for name, values in by_industry.items()
    }
    allowed = set(training_ids)
    predictions = []
    for row_id in evaluation_ids:
        industry = _industry(row_id, aligned, categorical)
        actual = float(aligned[row_id]["labels"][TARGET_COLUMNS[target]])
        values = {
            "global_median": global_median,
            "industry_median": industry_medians.get(industry, global_median),
            "last_published_company_score": _previous_score(
                row_id, allowed, aligned, target
            ),
        }
        for baseline, predicted in values.items():
            predictions.append(
                {
                    "target": target,
                    "row_id": row_id,
                    "baseline": baseline,
                    "actual": actual,
                    "predicted": predicted,
                    "industry": industry,
                }
            )
    return predictions


def _prediction_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched = [row for row in rows if row.get("predicted") is not None]
    if not matched:
        return {"matched_count": 0, "mae": None, "rmse": None}
    actual = [row["actual"] for row in matched]
    predicted = [row["predicted"] for row in matched]
    return {
        "matched_count": len(matched),
        "mae": _mae(actual, predicted),
        "rmse": _rmse(actual, predicted),
    }


def _rank_correlations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_industry[row["industry"]].append(row)
    values = []
    excluded = {}
    for industry, members in sorted(by_industry.items()):
        if len(members) < 3:
            excluded[industry] = "fewer than three test rows"
            continue
        actual = pd.Series([row["actual"] for row in members]).rank(method="average")
        predicted = pd.Series([row["predicted"] for row in members]).rank(
            method="average"
        )
        correlation = actual.corr(predicted)
        if pd.isna(correlation):
            excluded[industry] = "constant actual or predicted ranks"
            continue
        values.append(
            {
                "industry": industry,
                "row_count": len(members),
                "spearman": float(correlation),
            }
        )
    total = sum(value["row_count"] for value in values)
    weighted = (
        sum(value["spearman"] * value["row_count"] for value in values) / total
        if total
        else None
    )
    return {
        "weighted_spearman": weighted,
        "matched_count": total,
        "industries": values,
        "excluded": excluded,
    }


def _coverage_fraction(row_id: str, aligned: dict, numeric: list[str]) -> float:
    row = aligned[row_id]["X"]
    return (
        sum(row.get(feature) is not None for feature in numeric) / len(numeric)
        if numeric
        else 1.0
    )


def _coverage_label(value: float, boundaries: list[float]) -> str:
    for lower, upper in itertools.pairwise(boundaries):
        if lower <= value < upper or (upper == 1 and value == 1):
            return f"{lower:.2f}-{upper:.2f}"
    raise ValueError("Coverage fraction is outside configured boundaries.")


def _coverage_errors(
    rows: list[dict[str, Any]], aligned: dict, numeric: list[str], bins: list[float]
) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[
            _coverage_label(_coverage_fraction(row["row_id"], aligned, numeric), bins)
        ].append(row)
    return [
        {"coverage_level": level, **_prediction_metrics(members)}
        for level, members in sorted(grouped.items())
    ]


def _training_ranges(
    row_ids: set[str], aligned: dict, numeric: list[str]
) -> dict[str, dict[str, float] | None]:
    result = {}
    for feature in numeric:
        values = []
        for row_id in row_ids:
            value = aligned[row_id]["X"].get(feature)
            if isinstance(value, bool) or (
                isinstance(value, (int, float)) and math.isfinite(float(value))
            ):
                values.append(float(value))
        result[feature] = {"min": min(values), "max": max(values)} if values else None
    return result


def _standard_error(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return float(
        np.std(np.asarray(values, dtype=float), ddof=1) / math.sqrt(len(values))
    )


def _fold_feature_masks(
    aligned: dict,
    split: dict[str, Any],
    ordered: list[str],
    registry: dict[str, dict[str, Any]],
    feature_categories: dict[str, str],
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Fit one support mask per outer training fold without reading scoring rows."""
    results = {}
    for fold in split["folds"]:
        support_mask = fit_feature_mask(
            aligned, fold["train_row_ids"], ordered, registry, config
        )
        results[fold["fold_id"]] = {
            "support_mask": support_mask,
            "feature_sets": {
                feature_set: apply_feature_set(
                    support_mask,
                    feature_set,
                    ordered,
                    feature_categories,
                    config,
                )
                for feature_set in FEATURE_SET_ORDER
            },
        }
    return results


def _evaluate_feature_set(
    aligned: dict,
    target: str,
    split: dict[str, Any],
    fold_masks: dict[str, dict[str, Any]],
    feature_set: str,
    config: dict[str, Any],
    categorical: list[str],
) -> dict[str, Any]:
    """Evaluate both model families on one ablation with shared fold-local masks."""
    cv_rows = []
    family_results = {}
    for family in MODEL_FAMILIES:
        configuration_results = []
        for configuration in model_configuration_grid(config, family):
            fold_results = []
            for fold in split["folds"]:
                active = fold_masks[fold["fold_id"]]["feature_sets"][feature_set][
                    "active_features"
                ]
                if not active:
                    raise SplitUnavailable(
                        f"{target.upper()} {fold['fold_id']} {feature_set} has no eligible features."
                    )
                active_categorical = [name for name in categorical if name in active]
                X_train = _frame(
                    fold["train_row_ids"], aligned, active, active_categorical
                )
                X_validation = _frame(
                    fold["validation_row_ids"], aligned, active, active_categorical
                )
                y_train = _scores(fold["train_row_ids"], aligned, target)
                y_validation = _scores(fold["validation_row_ids"], aligned, target)
                model = _new_model(
                    family, configuration["parameters"], active, active_categorical
                )
                _fit_model(model, family, X_train, y_train, active_categorical)
                predictions = np.asarray(model.predict(X_validation), dtype=float)
                fold_mae = _mae(y_validation, predictions)
                fold_results.append(
                    {
                        "fold_id": fold["fold_id"],
                        "mae": fold_mae,
                        "row_count": len(fold["validation_row_ids"]),
                        "active_feature_count": len(active),
                    }
                )
                for row_id, actual, predicted in zip(
                    fold["validation_row_ids"], y_validation, predictions, strict=True
                ):
                    cv_rows.append(
                        {
                            "target": target,
                            "feature_set": feature_set,
                            "family": family,
                            "configuration_id": configuration["configuration_id"],
                            "fold_id": fold["fold_id"],
                            "row_id": row_id,
                            "actual": float(actual),
                            "predicted": float(predicted),
                            "absolute_error": abs(float(actual) - float(predicted)),
                        }
                    )
            configuration_results.append(
                {
                    **configuration,
                    "folds": fold_results,
                    "mean_validation_mae": float(
                        np.mean([result["mae"] for result in fold_results])
                    ),
                }
            )
        family_results[family] = configuration_results
    return {"cv_predictions": cv_rows, "families": family_results}


def _target_training(
    aligned: dict,
    target: str,
    split: dict[str, Any],
    config: dict[str, Any],
    ordered: list[str],
    numeric: list[str],
    categorical: list[str],
    registry: dict[str, dict[str, Any]],
    feature_categories: dict[str, str],
) -> dict[str, Any]:
    minimum = config["training"]["minimum_training_rows"]
    if len(split["development_row_ids"]) < minimum:
        raise SplitUnavailable(
            f"{target.upper()} development data has {len(split['development_row_ids'])} rows; minimum is {minimum}."
        )
    for fold in split["folds"]:
        if len(fold["train_row_ids"]) < minimum:
            raise SplitUnavailable(
                f"{target.upper()} {fold['fold_id']} has {len(fold['train_row_ids'])} training rows; minimum is {minimum}."
            )

    baseline_rows = []
    validation_baseline_metrics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fold in split["folds"]:
        fold_baselines = _baseline_predictions(
            fold["train_row_ids"],
            fold["validation_row_ids"],
            aligned,
            target,
            categorical,
        )
        for row in fold_baselines:
            row["subset"] = "validation"
            row["fold_id"] = fold["fold_id"]
        baseline_rows.extend(fold_baselines)
        for baseline in (
            "global_median",
            "industry_median",
            "last_published_company_score",
        ):
            members = [row for row in fold_baselines if row["baseline"] == baseline]
            validation_baseline_metrics[baseline].append(
                {"fold_id": fold["fold_id"], **_prediction_metrics(members)}
            )

    fold_masks = _fold_feature_masks(
        aligned, split, ordered, registry, feature_categories, config
    )
    evaluations = {}
    evaluation_cache = {}
    for feature_set in FEATURE_SET_ORDER:
        signature = tuple(
            tuple(
                fold_masks[fold["fold_id"]]["feature_sets"][feature_set][
                    "active_features"
                ]
            )
            for fold in split["folds"]
        )
        if not all(signature):
            evaluations[feature_set] = {
                "status": "unavailable",
                "reason": "At least one validation fold has no eligible features.",
            }
            continue
        if signature not in evaluation_cache:
            evaluation_cache[signature] = _evaluate_feature_set(
                aligned,
                target,
                split,
                fold_masks,
                feature_set,
                config,
                categorical,
            )
        evaluation = deepcopy(evaluation_cache[signature])
        for row in evaluation["cv_predictions"]:
            row["feature_set"] = feature_set
        evaluations[feature_set] = {"status": "evaluated", **evaluation}

    ablation_results = {}
    selected_by_feature_set = {}
    cv_rows = []
    for feature_set in FEATURE_SET_ORDER:
        evaluation = evaluations[feature_set]
        if evaluation["status"] != "evaluated":
            ablation_results[feature_set] = {
                "mean_validation_mae": None,
                "standard_error": None,
                "reason": evaluation["reason"],
            }
            continue
        cv_rows.extend(evaluation["cv_predictions"])
        selected_by_feature_set[feature_set] = {}
        for family in MODEL_FAMILIES:
            selected_by_feature_set[feature_set][family] = min(
                evaluation["families"][family],
                key=lambda row: (
                    row["mean_validation_mae"],
                    row["configuration_id"],
                ),
            )
        combined_fold_mae = []
        for fold in split["folds"]:
            values = [
                next(
                    result["mae"]
                    for result in selected_by_feature_set[feature_set][family]["folds"]
                    if result["fold_id"] == fold["fold_id"]
                )
                for family in MODEL_FAMILIES
            ]
            combined_fold_mae.append(float(np.mean(values)))
        ablation_results[feature_set] = {
            "mean_validation_mae": float(np.mean(combined_fold_mae)),
            "standard_error": _standard_error(combined_fold_mae),
            "fold_mae": [
                {"fold_id": fold["fold_id"], "mae": value}
                for fold, value in zip(split["folds"], combined_fold_mae, strict=True)
            ],
            "family_selected_configurations": {
                family: selected_by_feature_set[feature_set][family]["configuration_id"]
                for family in MODEL_FAMILIES
            },
        }
    feature_set_selection = choose_feature_set(ablation_results, config)
    selected_feature_set = feature_set_selection["feature_set"]
    selected = selected_by_feature_set[selected_feature_set]

    development_ids = split["development_row_ids"]
    final_support_mask = fit_feature_mask(
        aligned, development_ids, ordered, registry, config
    )
    final_mask = apply_feature_set(
        final_support_mask,
        selected_feature_set,
        ordered,
        feature_categories,
        config,
    )
    active = final_mask["active_features"]
    if not active:
        raise SplitUnavailable(
            f"{target.upper()} development data has no eligible features."
        )
    active_categorical = [name for name in categorical if name in active]
    fitted_models = {}
    for family in MODEL_FAMILIES:
        model = _new_model(
            family, selected[family]["parameters"], active, active_categorical
        )
        _fit_model(
            model,
            family,
            _frame(development_ids, aligned, active, active_categorical),
            _scores(development_ids, aligned, target),
            active_categorical,
        )
        fitted_models[family] = model

    test_ids = split["final_test_row_ids"]
    if not test_ids:
        raise SplitUnavailable(f"{target.upper()} split has no final-test rows.")
    X_test = _frame(test_ids, aligned, active, active_categorical)
    y_test = _scores(test_ids, aligned, target)
    test_rows = []
    metrics = {"validation": {}, "final_test": {"models": {}, "baselines": {}}}
    coverage = {}
    metrics["validation"]["category_ablation"] = ablation_results
    metrics["validation"]["selected_feature_set"] = feature_set_selection
    metrics["validation"]["baselines"] = {
        baseline: {
            "folds": fold_metrics,
            "mean_validation_mae": (
                float(
                    np.mean(
                        [
                            fold["mae"]
                            for fold in fold_metrics
                            if fold["mae"] is not None
                        ]
                    )
                )
                if any(fold["mae"] is not None for fold in fold_metrics)
                else None
            ),
            "matched_count": sum(fold["matched_count"] for fold in fold_metrics),
        }
        for baseline, fold_metrics in validation_baseline_metrics.items()
    }
    for family in MODEL_FAMILIES:
        metrics["validation"][family] = {
            "configurations": [
                {
                    "configuration_id": result["configuration_id"],
                    "mean_validation_mae": result["mean_validation_mae"],
                    "folds": result["folds"],
                }
                for result in model_configuration_results(
                    cv_rows,
                    target,
                    family,
                    selected[family],
                    config,
                    selected_feature_set,
                )
            ],
            "selected_configuration_id": selected[family]["configuration_id"],
        }
        predictions = np.asarray(fitted_models[family].predict(X_test), dtype=float)
        family_test_rows = []
        for row_id, actual, predicted in zip(
            test_ids, y_test, predictions, strict=True
        ):
            row = {
                "target": target,
                "family": family,
                "row_id": row_id,
                "actual": float(actual),
                "predicted": float(predicted),
                "absolute_error": abs(float(actual) - float(predicted)),
                "industry": _industry(row_id, aligned, categorical),
            }
            test_rows.append(row)
            family_test_rows.append(row)
        metrics["final_test"]["models"][family] = {
            **_prediction_metrics(family_test_rows),
            "within_industry_rank_correlation": _rank_correlations(family_test_rows),
        }
        coverage[family] = _coverage_errors(
            family_test_rows, aligned, numeric, config["training"]["coverage_bins"]
        )

    final_baselines = _baseline_predictions(
        split["development_row_ids"], test_ids, aligned, target, categorical
    )
    for row in final_baselines:
        row["subset"] = "final_test"
        row["fold_id"] = None
    baseline_rows.extend(final_baselines)
    for baseline in (
        "global_median",
        "industry_median",
        "last_published_company_score",
    ):
        rows = [row for row in final_baselines if row["baseline"] == baseline]
        metrics["final_test"]["baselines"][baseline] = _prediction_metrics(rows)
        coverage[baseline] = _coverage_errors(
            [row for row in rows if row["predicted"] is not None],
            aligned,
            numeric,
            config["training"]["coverage_bins"],
        )
    return {
        "selected": selected,
        "models": fitted_models,
        "cv_predictions": cv_rows,
        "test_predictions": test_rows,
        "baseline_predictions": baseline_rows,
        "metrics": metrics,
        "coverage_errors": coverage,
        "feature_selection": {
            "target": target,
            "folds": {
                fold_id: {
                    "support_mask": details["support_mask"],
                    "feature_sets": {
                        feature_set: {
                            "active_features": result["active_features"],
                            "excluded_features": result["excluded_features"],
                        }
                        for feature_set, result in details["feature_sets"].items()
                    },
                }
                for fold_id, details in fold_masks.items()
            },
            "development_support_mask": final_support_mask,
            "final_mask": final_mask,
            "selection": feature_set_selection,
            "ablation_results": ablation_results,
        },
    }


def model_configuration_results(
    cv_rows: list[dict],
    target: str,
    family: str,
    selected: dict,
    config: dict,
    feature_set: str,
) -> list[dict]:
    """Reconstruct compact configuration metrics from saved fold predictions."""
    results = []
    parameters_by_id = {
        row["configuration_id"]: row["parameters"]
        for row in model_configuration_grid(config, family)
    }
    for configuration_id, parameters in parameters_by_id.items():
        rows = [
            row
            for row in cv_rows
            if row["target"] == target
            and row["family"] == family
            and row["feature_set"] == feature_set
            and row["configuration_id"] == configuration_id
        ]
        folds = []
        for fold_id in sorted({row["fold_id"] for row in rows}):
            members = [row for row in rows if row["fold_id"] == fold_id]
            folds.append(
                {
                    "fold_id": fold_id,
                    "mae": float(np.mean([row["absolute_error"] for row in members])),
                    "row_count": len(members),
                }
            )
        results.append(
            {
                "configuration_id": configuration_id,
                "parameters": parameters,
                "folds": folds,
                "mean_validation_mae": float(np.mean([fold["mae"] for fold in folds])),
                "selected": configuration_id == selected["configuration_id"],
            }
        )
    return results


def train_models(
    dataset_dir: str | Path,
    config_path: str | Path | dict[str, Any],
    output_dir: str | Path | None = None,
    split_mode: str | None = None,
) -> dict[str, Any]:
    """Tune four required models, or return explicit blockers without fabricating results."""
    config = _config(config_path)
    dataset, dataset_path = _load_dataset(dataset_dir)
    snapshot_id = dataset.get("snapshot_id")
    ordered, categorical, feature_categories = _feature_contract(dataset)
    registry = dataset.get("registry", dataset["schema"]["feature_definitions"])
    if not isinstance(registry, dict):
        raise TypeError("Dataset feature registry must be an object.")
    numeric = [feature for feature in ordered if feature not in set(categorical)]
    aligned = _aligned_rows(dataset, ordered)

    splits = {}
    blockers = []
    target_status = {}
    for target in TARGETS:
        try:
            splits[target] = build_split_assignments(
                dataset, target, config, split_mode
            )
        except SplitUnavailable as error:
            reason = str(error)
            blockers.append(reason)
            target_status[target] = {"status": "blocked", "reason": reason}
    if not splits:
        return {
            "status": "blocked",
            "run_id": None,
            "run_dir": None,
            "dataset_snapshot_id": snapshot_id,
            "targets": target_status,
            "blockers": blockers,
        }

    target_results = {}
    for target, split in splits.items():
        try:
            target_results[target] = _target_training(
                aligned,
                target,
                split,
                config,
                ordered,
                numeric,
                categorical,
                registry,
                feature_categories,
            )
            target_status[target] = {
                "status": "trained",
                "development_count": len(split["development_row_ids"]),
                "final_test_count": len(split["final_test_row_ids"]),
                "split_mode": split["mode"],
                "selected_feature_set": target_results[target]["feature_selection"][
                    "selection"
                ]["feature_set"],
                "active_feature_count": len(
                    target_results[target]["feature_selection"]["final_mask"][
                        "active_features"
                    ]
                ),
            }
        except SplitUnavailable as error:
            reason = str(error)
            blockers.append(reason)
            target_status[target] = {"status": "blocked", "reason": reason}
    if not target_results:
        return {
            "status": "blocked",
            "run_id": None,
            "run_dir": None,
            "dataset_snapshot_id": snapshot_id,
            "targets": target_status,
            "blockers": blockers,
        }

    config_sha256 = hashlib.sha256(canonical_json_bytes(config)).hexdigest()
    split_sha256 = {
        target: hashlib.sha256(canonical_json_bytes(splits[target])).hexdigest()
        for target in target_results
    }
    dependency_versions = _package_versions()
    run_identity = {
        "version": TRAINING_VERSION,
        "dataset_snapshot_id": snapshot_id,
        "config_sha256": config_sha256,
        "split_sha256": split_sha256,
        "dependency_versions": dependency_versions,
    }
    run_id = hashlib.sha256(canonical_json_bytes(run_identity)).hexdigest()
    output = Path(output_dir or config["artifacts"]["output_directory"])
    output.mkdir(parents=True, exist_ok=True)
    final_directory = output / run_id
    if final_directory.exists():
        from green500.ml.artifacts import load_verified_run

        manifest = load_verified_run(final_directory)
        write_latest_run_pointer(output, final_directory)
        return {
            "status": manifest["status"],
            "run_id": run_id,
            "run_dir": str(final_directory),
            "dataset_snapshot_id": snapshot_id,
            "targets": manifest["targets"],
            "blockers": manifest["blockers"],
            "reused": True,
        }
    temporary = output / f".{run_id}.{os.getpid()}.{time.time_ns()}.tmp"
    temporary.mkdir()
    try:
        write_json_atomic(temporary / "config.json", config)
        shutil.copyfile(
            dataset_path / "registry.json", temporary / "feature_registry.json"
        )
        shutil.copyfile(dataset_path / "schema.json", temporary / "dataset_schema.json")
        shutil.copyfile(
            dataset_path / "manifest.json", temporary / "dataset_manifest.json"
        )
        write_json_atomic(
            temporary / "selected_configs.json",
            {
                target: {
                    "feature_set": result["feature_selection"]["selection"],
                    "models": {
                        family: result["selected"][family] for family in MODEL_FAMILIES
                    },
                }
                for target, result in target_results.items()
            },
        )
        for target in target_results:
            write_json_atomic(
                temporary / "split_assignments" / f"{target}.json", splits[target]
            )
        cv_predictions = [
            row
            for result in target_results.values()
            for row in result["cv_predictions"]
        ]
        test_predictions = [
            row
            for result in target_results.values()
            for row in result["test_predictions"]
        ]
        baseline_predictions = [
            row
            for result in target_results.values()
            for row in result["baseline_predictions"]
        ]
        write_json_lines_atomic(temporary / "oof_predictions.jsonl", cv_predictions)
        write_json_lines_atomic(
            temporary / "final_test_predictions.jsonl", test_predictions
        )
        write_json_lines_atomic(
            temporary / "baseline_predictions.jsonl", baseline_predictions
        )
        write_json_atomic(
            temporary / "metrics.json",
            {target: result["metrics"] for target, result in target_results.items()},
        )
        write_json_atomic(
            temporary / "coverage_errors.json",
            {
                target: result["coverage_errors"]
                for target, result in target_results.items()
            },
        )
        write_json_atomic(
            temporary / "feature_selection.json",
            {
                target: result["feature_selection"]
                for target, result in target_results.items()
            },
        )
        models = {}
        for target, result in target_results.items():
            models[target] = {
                family: save_model_artifact(
                    temporary, target, family, result["models"][family]
                )
                for family in MODEL_FAMILIES
            }
        training_row_ids = {
            row_id
            for target in target_results
            for row_id in splits[target]["development_row_ids"]
        }
        known_industries = sorted(
            {_industry(row_id, aligned, categorical) for row_id in training_row_ids}
        )
        registry_path = dataset_path / "registry.json"
        dataset_manifest_path = dataset_path / "manifest.json"
        files = {}
        for path in sorted(temporary.rglob("*")):
            if path.is_file():
                files[path.relative_to(temporary).as_posix()] = file_description(path)
        manifest = {
            "version": RUN_MANIFEST_VERSION,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "status": "completed" if len(target_results) == len(TARGETS) else "partial",
            "dataset_snapshot_id": snapshot_id,
            "dataset_manifest_sha256": file_description(dataset_manifest_path)[
                "sha256"
            ],
            "ordered_features": ordered,
            "categorical_features": categorical,
            "feature_categories": feature_categories,
            "active_features": {
                target: result["feature_selection"]["final_mask"]["active_features"]
                for target, result in target_results.items()
            },
            "excluded_features": {
                target: result["feature_selection"]["final_mask"]["excluded_features"]
                for target, result in target_results.items()
            },
            "ablation_results": {
                target: result["feature_selection"]["ablation_results"]
                for target, result in target_results.items()
            },
            "numeric_training_ranges": _training_ranges(
                training_row_ids, aligned, numeric
            ),
            "known_industries": known_industries,
            "models": models,
            "registry_sha256": file_description(registry_path)["sha256"],
            "config_sha256": config_sha256,
            "split_sha256": split_sha256,
            "dependency_versions": dependency_versions,
            "files": files,
            "targets": target_status,
            "blockers": blockers,
        }
        write_run_manifest(temporary, manifest)
        publish_run_directory(temporary, final_directory)
        write_latest_run_pointer(output, final_directory)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "status": manifest["status"],
        "run_id": run_id,
        "run_dir": str(final_directory),
        "dataset_snapshot_id": snapshot_id,
        "targets": target_status,
        "blockers": blockers,
        "reused": False,
    }
