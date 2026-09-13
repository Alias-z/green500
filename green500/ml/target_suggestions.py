"""Search one active measurement at a time for a requested model estimate."""

from __future__ import annotations

import math
from collections import defaultdict

from green500.ml.inference import MODEL_FAMILIES, TARGETS, _active_features


def _finite_number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be a finite number.")
    return float(value)


def _sample_values(
    current: float,
    observed_values: list[object],
    lower: float,
    upper: float,
    data_type: str,
    unit: str | None,
) -> list[int | float]:
    """Return bounded, realistic candidate values with adequate nonlinear coverage."""
    observed = sorted(
        {
            float(value)
            for value in observed_values
            if not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and lower <= value <= upper
        }
    )
    sampled: set[float] = {lower, upper}
    if observed:
        denominator = min(20, len(observed) - 1)
        if denominator:
            sampled.update(
                observed[round(index * (len(observed) - 1) / denominator)]
                for index in range(denominator + 1)
            )
        else:
            sampled.update(observed)
    if unit == "percent":
        sampled.update(float(value) for value in range(0, 101, 5))
    elif unit == "year":
        sampled.update(
            float(value) for value in range(math.ceil(lower), math.floor(upper) + 1)
        )
    elif lower >= 0 and upper > 0:
        positive_lower = max(
            lower, min((value for value in observed if value > 0), default=upper)
        )
        if positive_lower > 0 and upper / positive_lower > 1:
            sampled.update(
                math.exp(
                    math.log(positive_lower)
                    + index * (math.log(upper) - math.log(positive_lower)) / 14
                )
                for index in range(15)
            )
    sampled.update(
        current * multiplier
        for multiplier in (0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.1, 1.25, 1.5, 2, 4, 10)
        if lower <= current * multiplier <= upper
    )
    requires_integer = data_type == "integer" or unit == "count"
    normalized: set[int | float] = set()
    for value in sampled:
        bounded = min(upper, max(lower, value))
        normalized.add(round(bounded) if requires_integer else float(bounded))
    normalized.discard(int(current) if requires_integer else float(current))
    return sorted(normalized)


def suggest_target_adjustments(
    service,
    row: dict,
    target: str,
    desired_score: float,
    model_family: str,
    candidate_values: dict[str, list[object]],
    *,
    max_suggestions: int = 5,
) -> dict:
    """Return measured one-field scenarios closest to a desired saved-model estimate."""
    if target not in TARGETS:
        raise ValueError("target must be esg or csa.")
    if model_family not in MODEL_FAMILIES:
        raise ValueError("model_family must be ebm or catboost.")
    desired_score = _finite_number(desired_score, "desired_score")
    if not 0 <= desired_score <= 100:
        raise ValueError("desired_score must be from 0 to 100.")
    if (
        isinstance(max_suggestions, bool)
        or not isinstance(max_suggestions, int)
        or not 1 <= max_suggestions <= 10
    ):
        raise ValueError("max_suggestions must be an integer from 1 to 10.")
    baseline = service.evaluate_rows([row], target)[0]
    original_score = baseline["predictions"][model_family]
    requested_change = desired_score - original_score
    if math.isclose(requested_change, 0, abs_tol=0.005):
        return {
            "company": baseline["company"],
            "target": target,
            "model_family": model_family,
            "original_predictions": baseline["predictions"],
            "desired_score": desired_score,
            "desired_direction": "unchanged",
            "suggestions": [],
            "warnings": [
                "The selected model estimate is already at the requested score."
            ],
            "disclosure": "These are one-field model sensitivities, not causal recommendations or guaranteed rating changes.",
        }

    active = _active_features(service.manifest, target)
    field_labels = {
        field["feature_name"]: field["label"]
        for field in service.describe()["targets"][target]["fields"]
    }
    ranges = service.manifest.get("numeric_training_ranges") or {}
    scenario_rows: list[dict] = []
    descriptions: list[dict] = []
    for feature_name in active:
        definition = service.registry[feature_name]
        if (
            definition.get("category_or_context") == "fixed_context"
            or definition.get("derived_feature_dependencies")
            or definition.get("data_type") not in {"integer", "number"}
        ):
            continue
        current = row.get("features", {}).get(feature_name)
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(current)
        ):
            continue
        limits = ranges.get(feature_name)
        if not isinstance(limits, dict):
            continue
        lower = _finite_number(limits.get("min"), f"{feature_name} training minimum")
        upper = _finite_number(limits.get("max"), f"{feature_name} training maximum")
        if lower > upper:
            raise ValueError(f"{feature_name} has an invalid saved training range.")
        values = _sample_values(
            float(current),
            candidate_values.get(feature_name, []),
            lower,
            upper,
            str(definition.get("data_type")),
            definition.get("canonical_unit"),
        )
        for value in values:
            try:
                changed_rows, changes = service.apply_scenario(
                    [row],
                    target,
                    [
                        {
                            "feature_name": feature_name,
                            "operation": "set",
                            "value": value,
                        }
                    ],
                )
            except ValueError:
                continue
            if not changes or changes[0]["status"] != "applied":
                continue
            scenario_rows.append(changed_rows[0])
            descriptions.append(
                {
                    "feature_name": feature_name,
                    "current_value": current,
                    "suggested_value": value,
                    "lower": lower,
                    "upper": upper,
                    "definition": definition,
                }
            )
    if not scenario_rows:
        raise ValueError(
            "No observed editable field has a valid search range for this company."
        )

    predicted = service.predict_rows_only(scenario_rows, target)
    by_feature: dict[str, list[dict]] = defaultdict(list)
    original_gap = abs(desired_score - original_score)
    desired_direction = 1 if requested_change > 0 else -1
    for description, predictions in zip(descriptions, predicted, strict=True):
        projected_score = predictions[model_family]
        improvement = original_gap - abs(desired_score - projected_score)
        movement = projected_score - original_score
        if improvement <= 1e-9 or movement * desired_direction <= 0:
            continue
        by_feature[description["feature_name"]].append(
            {
                **description,
                "predictions": predictions,
                "projected_score": projected_score,
                "remaining_gap": desired_score - projected_score,
                "improvement": improvement,
            }
        )

    suggestions = []
    for feature_name, choices in by_feature.items():
        choice = min(
            choices,
            key=lambda item: (
                abs(item["remaining_gap"]),
                abs(item["suggested_value"] - item["current_value"])
                / max(abs(item["current_value"]), 1),
            ),
        )
        definition = choice["definition"]
        suggested = choice["suggested_value"]
        current = choice["current_value"]
        absolute_change = suggested - current
        relative_change_pct = (
            absolute_change / abs(current) * 100 if current != 0 else None
        )
        selected_change = choice["projected_score"] - original_score
        other_family = "catboost" if model_family == "ebm" else "ebm"
        other_change = (
            choice["predictions"][other_family] - baseline["predictions"][other_family]
        )
        reaches = choice["remaining_gap"] * desired_direction <= 0
        warnings = []
        if math.isclose(suggested, choice["lower"], abs_tol=1e-12) or math.isclose(
            suggested, choice["upper"], abs_tol=1e-12
        ):
            warnings.append(
                "The closest tested value is at the saved training boundary."
            )
        if other_change * selected_change < 0:
            warnings.append(
                "EBM and CatBoost disagree on the direction of this one-field sensitivity."
            )
        models_agree = other_change * selected_change >= 0
        suggestions.append(
            {
                "feature_name": feature_name,
                "label": field_labels[feature_name],
                "category": definition["category_or_context"],
                "unit": definition.get("canonical_unit"),
                "current_value": current,
                "suggested_value": suggested,
                "absolute_change": absolute_change,
                "relative_change_pct": relative_change_pct,
                "percentage_point_change": (
                    absolute_change
                    if definition.get("canonical_unit") == "percent"
                    else None
                ),
                "operation": "set",
                "projected_predictions": choice["predictions"],
                "selected_model_change": selected_change,
                "remaining_gap": choice["remaining_gap"],
                "reaches_target": reaches,
                "models_agree": models_agree,
                "range_position": (
                    (suggested - choice["lower"]) / (choice["upper"] - choice["lower"])
                    if choice["upper"] > choice["lower"]
                    else 0
                ),
                "warning": " ".join(warnings) or None,
            }
        )
    suggestions.sort(
        key=lambda item: (
            not item["models_agree"],
            not item["reaches_target"],
            abs(item["remaining_gap"]),
            item["relative_change_pct"] is None,
            abs(item["relative_change_pct"] or math.inf),
            item["label"],
        )
    )
    warnings = []
    if not suggestions:
        warnings.append(
            "No tested one-field change moved the selected model closer to the requested score."
        )
    elif not any(item["reaches_target"] for item in suggestions):
        warnings.append(
            "No tested one-field change reached the requested score; the closest measured sensitivities are shown."
        )
    return {
        "company": baseline["company"],
        "target": target,
        "model_family": model_family,
        "original_predictions": baseline["predictions"],
        "desired_score": desired_score,
        "desired_direction": "increase" if desired_direction > 0 else "decrease",
        "suggestions": suggestions[:max_suggestions],
        "warnings": warnings,
        "disclosure": "These are one-field model sensitivities within the saved training ranges, not causal recommendations or guaranteed rating changes.",
    }
