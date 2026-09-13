"""Load verified model runs and predict from dated company feature rows."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

TARGETS = ("esg", "csa")
MODEL_FAMILIES = ("ebm", "catboost")
CATEGORY_ORDER = (
    "financial",
    "social",
    "environmental",
    "climate_target",
    "financial_target",
    "fixed_context",
)
ALLOWED_CATEGORIES = set(CATEGORY_ORDER)


def _prediction_date(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as error:
        raise ValueError("prediction_as_of must use YYYY-MM-DD.") from error


def _manifest(verified_run: dict) -> dict:
    manifest = verified_run.get("manifest", verified_run)
    if not isinstance(manifest, dict):
        raise TypeError("The verified model run has no manifest object.")
    return manifest


def _ordered_features(manifest: dict) -> list[str]:
    features = manifest.get("ordered_features")
    if (
        not isinstance(features, list)
        or not features
        or any(not isinstance(name, str) or not name for name in features)
        or len(features) != len(set(features))
    ):
        raise ValueError("The model run has no valid ordered feature contract.")
    categories = manifest.get("feature_categories")
    if not isinstance(categories, dict) or set(categories) != set(features):
        raise ValueError("The model run feature categories do not match its columns.")
    unknown = set(categories.values()) - ALLOWED_CATEGORIES
    if unknown:
        raise ValueError("The model run contains an unsupported feature category.")
    return features


def _active_features(manifest: dict, target: str) -> list[str]:
    """Validate one target's saved training-only feature mask and exclusions."""
    ordered = _ordered_features(manifest)
    masks = manifest.get("active_features")
    if not isinstance(masks, dict):
        raise TypeError("The model run has no target active_features object.")
    active = masks.get(target)
    if (
        not isinstance(active, list)
        or not active
        or any(not isinstance(name, str) or not name for name in active)
        or len(active) != len(set(active))
    ):
        raise ValueError(f"The model run has no valid active feature mask for {target}.")
    active_set = set(active)
    if [name for name in ordered if name in active_set] != active:
        raise ValueError(
            f"The {target} active feature mask is not an ordered registry subset."
        )
    exclusions_by_target = manifest.get("excluded_features")
    if not isinstance(exclusions_by_target, dict):
        raise TypeError("The model run has no target excluded_features object.")
    exclusions = exclusions_by_target.get(target)
    if not isinstance(exclusions, dict):
        raise TypeError(f"The model run has no excluded feature reasons for {target}.")
    expected_exclusions = set(ordered) - active_set
    if set(exclusions) != expected_exclusions:
        raise ValueError(
            f"The {target} excluded features do not complement its active mask."
        )
    for feature, details in exclusions.items():
        reasons = details.get("reasons") if isinstance(details, dict) else None
        support = details.get("support") if isinstance(details, dict) else None
        if (
            not isinstance(reasons, list)
            or not reasons
            or any(not isinstance(reason, str) or not reason.strip() for reason in reasons)
            or not isinstance(support, dict)
        ):
            raise ValueError(
                f"The {target} exclusion for {feature} lacks reasons or support."
            )
    return active


def _excluded_features(manifest: dict, target: str) -> dict:
    _active_features(manifest, target)
    return manifest["excluded_features"][target]


def _saved_availability_policy(run_dir: Path, manifest: dict) -> str:
    """Read the hash-verified dataset policy copied into the saved model run."""
    import json

    files = manifest.get("files")
    if not isinstance(files, dict) or "dataset_schema.json" not in files:
        raise ValueError("The model run does not hash-bind dataset_schema.json.")
    schema_path = run_dir / "dataset_schema.json"
    try:
        schema = json.loads(schema_path.read_bytes())
        dataset_manifest = json.loads((run_dir / "dataset_manifest.json").read_bytes())
    except json.JSONDecodeError as error:
        raise ValueError("The saved dataset policy artifacts are invalid JSON.") from error
    if not isinstance(schema, dict) or not isinstance(dataset_manifest, dict):
        raise TypeError("The saved dataset policy artifacts must be JSON objects.")
    from green500.ml.dataset import normalize_availability_policy

    policy = normalize_availability_policy(schema.get("availability_policy"))
    if dataset_manifest.get("availability_policy") != policy:
        raise ValueError("The saved dataset schema and manifest availability policies differ.")
    return policy


def _require_row_availability_policy(row: dict, policy: str) -> None:
    if row.get("availability_policy") != policy:
        raise ValueError(
            "The inference row availability policy differs from the saved training policy."
        )


def resolve_model_run(settings: Any, model_run: str | Path | None) -> Path:
    """Resolve an explicit run or the integrity-checked latest-run pointer."""
    if model_run is not None:
        return Path(model_run).resolve()
    runs_dir = Path(settings.data_dir) / "ml" / "runs"
    latest_path = runs_dir / "latest.json"
    if not latest_path.is_file():
        raise FileNotFoundError(
            "No model run was supplied and data/ml/runs/latest.json does not exist."
        )
    import json

    latest = json.loads(latest_path.read_text())
    expected_keys = {
        "version",
        "run_id",
        "run_path",
        "run_manifest_sha256",
    }
    if not isinstance(latest, dict) or set(latest) != expected_keys:
        raise ValueError("The latest model run pointer has an invalid contract.")
    from green500.ml.artifacts import RUN_MANIFEST_VERSION, sha256_file

    if latest["version"] != RUN_MANIFEST_VERSION:
        raise ValueError("The latest model run pointer has an unsupported version.")
    run_id = latest["run_id"]
    relative = latest["run_path"]
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(relative, str)
        or Path(relative).is_absolute()
    ):
        raise ValueError("The latest model run pointer has an invalid run path.")
    run_dir = (runs_dir / relative).resolve()
    try:
        run_dir.relative_to(runs_dir.resolve())
    except ValueError as error:
        raise ValueError("The latest model run pointer escapes its runs directory.") from error
    if run_dir.name != run_id:
        raise ValueError("The latest model run path does not match its run_id.")
    manifest_path = run_dir / "run_manifest.json"
    if sha256_file(manifest_path) != latest["run_manifest_sha256"]:
        raise ValueError("The latest model run pointer does not match its manifest.")
    return run_dir


def _feature_frame(
    features: dict, manifest: dict, ordered_features: list[str] | None = None
):
    """Build one ordered frame without fitting or changing missing values."""
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError("pandas is required for saved-model inference.") from error

    ordered = ordered_features or _ordered_features(manifest)
    missing = [name for name in ordered if name not in features]
    if missing:
        raise ValueError(
            "The company row is missing model features: " + ", ".join(missing)
        )
    declared_categorical = set(manifest.get("categorical_features") or [])
    if not declared_categorical.issubset(_ordered_features(manifest)):
        raise ValueError("Categorical features are outside the registry feature contract.")
    categorical = declared_categorical.intersection(ordered)
    frame = pd.DataFrame(
        [{name: features[name] for name in ordered}], columns=ordered
    )
    for feature in ordered:
        if feature in categorical:
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
            raise ValueError(f"Numeric feature contains a non-numeric value: {feature}")
        present = converted.dropna()
        if any(not math.isfinite(float(value)) for value in present):
            raise ValueError(f"Numeric feature contains a non-finite value: {feature}")
        frame[feature] = converted.astype(float)
    return frame


def _validate_model_features(model: Any, active_features: list[str]) -> None:
    """Bind a deserialized estimator to the exact saved active feature order."""
    model_features = getattr(model, "feature_names_in_", None)
    if model_features is None:
        model_features = getattr(model, "feature_names_", None)
    if model_features is not None:
        if list(model_features) != active_features:
            raise ValueError("The saved model feature order differs from active_features.")
        return
    feature_count = getattr(model, "n_features_in_", None)
    if feature_count != len(active_features):
        raise ValueError("The saved model has no matching active feature contract.")


def _finite_prediction(model: Any, frame: Any) -> float:
    values = model.predict(frame)
    try:
        value = float(values[0])
    except (IndexError, TypeError, ValueError) as error:
        raise ValueError("The saved model did not return one numeric prediction.") from error
    if not math.isfinite(value):
        raise ValueError("The saved model returned a non-finite prediction.")
    return value


def _range_warnings(
    features: dict, manifest: dict, active_features: list[str] | None = None
) -> list[str]:
    warnings = []
    ranges = manifest.get("numeric_training_ranges") or {}
    if not isinstance(ranges, dict):
        raise TypeError("numeric_training_ranges must be an object.")
    active_set = set(active_features or _ordered_features(manifest))
    for name, bounds in ranges.items():
        if name not in active_set:
            continue
        value = features.get(name)
        if value is None or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number or null.")
        if bounds is None:
            warnings.append(f"{name} had no non-missing values in the training data.")
            continue
        if not isinstance(bounds, dict):
            raise TypeError(f"Training range for {name} must be an object.")
        minimum, maximum = bounds.get("min"), bounds.get("max")
        if minimum is not None and value < minimum:
            warnings.append(f"{name} is below the training range.")
        if maximum is not None and value > maximum:
            warnings.append(f"{name} is above the training range.")
    return warnings


def _industry_warnings(features: dict, manifest: dict) -> list[str]:
    known = manifest.get("known_industries") or []
    industry = features.get("industry")
    if not isinstance(industry, str) or not industry.strip():
        return ["Industry is missing and was normalized to the Unknown category."]
    industry = industry.strip()
    if known and industry not in known:
        return [
            f"Industry {industry!r} was not observed in this model run's training data."
        ]
    return []


def load_prediction_run(model_run: str | Path) -> tuple[Path, dict, dict]:
    """Verify a model run before any model file is deserialized."""
    from green500.ml.artifacts import load_verified_run

    run_dir = Path(model_run).resolve()
    verified = load_verified_run(run_dir)
    manifest = _manifest(verified)
    _ordered_features(manifest)
    _saved_availability_policy(run_dir, manifest)
    for target in (manifest.get("models") or {}):
        _active_features(manifest, target)
    return run_dir, verified, manifest


def _active_coverage(row: dict, manifest: dict, target: str) -> dict:
    """Report model-input availability without replacing full registry coverage."""
    active = _active_features(manifest, target)
    features = row["features"]
    categories = manifest["feature_categories"]
    exclusions = _excluded_features(manifest, target)
    result = {}
    for category in CATEGORY_ORDER:
        category_active = [name for name in active if categories[name] == category]
        category_excluded = {
            name: details
            for name, details in exclusions.items()
            if categories[name] == category
        }
        result[category] = {
            "active_feature_count": len(category_active),
            "available_active_feature_count": sum(
                features.get(name) is not None for name in category_active
            ),
            "missing_active_features": [
                name for name in category_active if features.get(name) is None
            ],
            "excluded_features": category_excluded,
            "category_excluded_from_model": not category_active,
        }
    missing = [name for name in active if features.get(name) is None]
    return {
        "active_feature_count": len(active),
        "available_active_feature_count": len(active) - len(missing),
        "missing_active_features": missing,
        "excluded_feature_count": len(exclusions),
        "categories": result,
    }


def predict_feature_row(
    row: dict,
    model_run: str | Path,
    *,
    targets: tuple[str, ...] = TARGETS,
) -> dict:
    """Predict an already-built dated row with every available saved model."""
    run_dir, _, manifest = load_prediction_run(model_run)
    availability_policy = _saved_availability_policy(run_dir, manifest)
    _require_row_availability_policy(row, availability_policy)
    features = row.get("features")
    if not isinstance(features, dict):
        raise TypeError("The inference row has no features object.")
    warnings = list(row.get("warnings") or [])
    available_models = manifest.get("models") or {}
    predictions: dict[str, dict[str, float | None]] = {}
    feature_selection = {}
    model_feature_coverage = {}
    for target in targets:
        if target not in TARGETS:
            raise ValueError("Unsupported prediction target: " + target)
        predictions[target] = {}
        if not available_models.get(target):
            feature_selection[target] = None
            model_feature_coverage[target] = None
            for family in MODEL_FAMILIES:
                predictions[target][family] = None
                warnings.append(f"No saved {family} model is available for {target}.")
            continue
        active = _active_features(manifest, target)
        exclusions = _excluded_features(manifest, target)
        frame = _feature_frame(features, manifest, active)
        active_coverage = _active_coverage(row, manifest, target)
        model_feature_coverage[target] = active_coverage
        feature_selection[target] = {
            "active_features": active,
            "excluded_features": exclusions,
        }
        warnings.extend(
            f"{target.upper()}: {warning}"
            for warning in _range_warnings(features, manifest, active)
        )
        if "industry" in active:
            warnings.extend(
                f"{target.upper()}: {warning}"
                for warning in _industry_warnings(features, manifest)
            )
        if active_coverage["missing_active_features"]:
            warnings.append(
                f"{target.upper()}: {len(active_coverage['missing_active_features'])} active model inputs are missing; saved missing-value behavior was used."
            )
        for family in MODEL_FAMILIES:
            entry = (available_models.get(target) or {}).get(family)
            if not entry:
                predictions[target][family] = None
                warnings.append(f"No saved {family} model is available for {target}.")
                continue
            from green500.ml.artifacts import load_model_artifact

            model = load_model_artifact(run_dir, target, family)
            _validate_model_features(model, active)
            predictions[target][family] = _finite_prediction(model, frame)
    return {
        "company": row.get("company"),
        "row_id": row.get("row_id"),
        "prediction_as_of": row.get("prediction_as_of"),
        "assessment_cycle": row.get("assessment_cycle"),
        "model_version": manifest.get("run_id", run_dir.name),
        "dataset_snapshot_id": manifest.get("dataset_snapshot_id"),
        "data_cutoff": row.get("prediction_as_of"),
        "availability_policy": availability_policy,
        "predictions": predictions,
        "coverage": row.get("coverage") or {},
        "model_feature_coverage": model_feature_coverage,
        "feature_selection": feature_selection,
        "warnings": list(dict.fromkeys(warnings)),
    }


def predict_company(
    company_id: str,
    prediction_as_of: str | date,
    *,
    settings: Any | None = None,
    model_run: str | Path | None = None,
    assessment_cycle: str = "inference",
    row_builder: Callable[..., dict] | None = None,
) -> dict:
    """Build one dated company row and predict without training or refitting."""
    if settings is None:
        from green500.config import load_settings

        settings = load_settings()
    if row_builder is None:
        from green500.ml.dataset import build_company_feature_row

        row_builder = build_company_feature_row
    as_of = _prediction_date(prediction_as_of)
    run_dir = resolve_model_run(settings, model_run)
    _, _, manifest = load_prediction_run(run_dir)
    availability_policy = _saved_availability_policy(run_dir, manifest)
    row = row_builder(
        settings,
        company_id,
        as_of,
        assessment_cycle=assessment_cycle,
        availability_policy=availability_policy,
    )
    if row.get("prediction_as_of") != as_of:
        raise ValueError("The company feature row does not match prediction_as_of.")
    if row.get("assessment_cycle") != assessment_cycle:
        raise ValueError("The company feature row does not match assessment_cycle.")
    return predict_feature_row(row, run_dir)
