"""Apply validated feature overrides without changing source observations."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from green500.ml.inference import (
    _prediction_date,
    _saved_availability_policy,
    load_prediction_run,
    predict_feature_row,
    resolve_model_run,
)


def _limit(rules: object, *names: str):
    if not isinstance(rules, dict):
        return None
    for name in names:
        if name in rules:
            return rules[name]
    return None


def _validate_override(name: str, value: object, specification: dict) -> None:
    if specification.get("category_or_context") == "fixed_context":
        raise ValueError(f"{name} is fixed context and cannot be a scenario override.")
    if specification.get("derived_feature_dependencies"):
        raise ValueError(
            f"{name} is derived; override its source observations instead."
        )
    if not specification.get("predictive", True):
        raise ValueError(f"{name} is not a predictive scenario feature.")
    if value is None:
        return
    kind = specification.get("data_type")
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean or null.")
        return
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer or null.")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number or null.")
    else:
        raise ValueError(f"{name} has an unsupported scenario data type.")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite or null.")
    rules = specification.get("validation_rules")
    minimum = _limit(rules, "minimum", "min", "ge")
    maximum = _limit(rules, "maximum", "max", "le")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} is below its allowed minimum.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} is above its allowed maximum.")


def _prediction_differences(original: dict, scenario: dict) -> dict:
    differences = {}
    for target, original_models in original.items():
        differences[target] = {}
        for family, original_value in original_models.items():
            scenario_value = scenario.get(target, {}).get(family)
            differences[target][family] = (
                scenario_value - original_value
                if original_value is not None and scenario_value is not None
                else None
            )
    return differences


def _scenario_coverage(features: dict, registry: dict) -> dict:
    """Recount availability after overrides and derived-feature recomputation."""
    predictive = [name for name, value in registry.items() if value["predictive"]]
    available_count = sum(features.get(name) is not None for name in predictive)
    fraction = available_count / len(predictive) if predictive else 0
    if fraction == 0:
        level = "none"
    elif fraction < 0.33:
        level = "low"
    elif fraction < 0.67:
        level = "medium"
    else:
        level = "high"
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
            "level": level,
        },
        "categories": categories,
    }


def predict_scenario(
    company_id: str,
    overrides: dict[str, object],
    prediction_as_of: str | date,
    *,
    settings: Any | None = None,
    model_run: str | Path | None = None,
    assessment_cycle: str = "inference",
    row_builder: Callable[..., dict] | None = None,
) -> dict:
    """Compare one source-backed row with a copied, recomputed scenario row."""
    if not isinstance(overrides, dict) or not overrides:
        raise ValueError("Scenario overrides must be a non-empty object.")
    if settings is None:
        from green500.config import load_settings

        settings = load_settings()
    if row_builder is None:
        from green500.ml.dataset import build_company_feature_row

        row_builder = build_company_feature_row
    from green500.ml.dataset import recompute_derived_features
    from green500.ml.feature_registry import feature_registry

    registry = feature_registry()
    unknown = sorted(set(overrides) - set(registry))
    if unknown:
        raise ValueError("Unknown scenario features: " + ", ".join(unknown))
    for name, value in overrides.items():
        _validate_override(name, value, registry[name])

    as_of = _prediction_date(prediction_as_of)
    run_dir = resolve_model_run(settings, model_run)
    _, _, manifest = load_prediction_run(run_dir)
    availability_policy = _saved_availability_policy(run_dir, manifest)
    original_row = row_builder(
        settings,
        company_id,
        as_of,
        assessment_cycle=assessment_cycle,
        availability_policy=availability_policy,
    )
    scenario_row = copy.deepcopy(original_row)
    original_features = copy.deepcopy(original_row.get("features") or {})
    original_metadata = copy.deepcopy(original_row.get("metadata") or {})
    scenario_features = copy.deepcopy(original_features)
    scenario_metadata = copy.deepcopy(original_metadata)
    for name, value in overrides.items():
        prior = copy.deepcopy(scenario_metadata.get(name) or {})
        scenario_features[name] = value
        scenario_metadata[name] = {
            **prior,
            "scenario_override": {
                "original_value": original_features.get(name),
                "scenario_value": value,
            },
        }
    scenario_features, scenario_metadata = recompute_derived_features(
        scenario_features, scenario_metadata
    )
    scenario_row["features"] = scenario_features
    scenario_row["metadata"] = scenario_metadata
    scenario_row["coverage"] = _scenario_coverage(scenario_features, registry)
    scenario_row["warnings"] = [
        *list(scenario_row.get("warnings") or []),
        "Scenario results describe model sensitivity, not a guaranteed score change.",
    ]

    original_result = predict_feature_row(original_row, run_dir)
    scenario_result = predict_feature_row(scenario_row, run_dir)
    return {
        "company": original_row.get("company"),
        "prediction_as_of": as_of,
        "assessment_cycle": assessment_cycle,
        "model_version": original_result["model_version"],
        "availability_policy": availability_policy,
        "label": "Model sensitivity",
        "overrides": copy.deepcopy(overrides),
        "original": {
            "features": original_features,
            "metadata": original_metadata,
            "predictions": original_result["predictions"],
            "coverage": original_result["coverage"],
        },
        "scenario": {
            "features": scenario_features,
            "metadata": scenario_metadata,
            "predictions": scenario_result["predictions"],
            "coverage": scenario_result["coverage"],
        },
        "differences": _prediction_differences(
            original_result["predictions"], scenario_result["predictions"]
        ),
        "warnings": list(
            dict.fromkeys(
                [*original_result["warnings"], *scenario_result["warnings"]]
            )
        ),
    }
