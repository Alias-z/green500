"""Fit sparse feature masks inside model-training partitions only."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

VERSION = "green500-feature-selection-v1"
FEATURE_SET_ORDER = (
    "core",
    "core_plus_climate_target",
    "core_plus_financial_target",
    "core_plus_both",
)


def configured_feature_sets(config: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Build the four fixed ablations from the validated category configuration."""
    settings = config["training"]["feature_selection"]
    core = frozenset(settings["core_categories"])
    climate, financial = settings["optional_categories"]
    return {
        "core": core,
        "core_plus_climate_target": core | {climate},
        "core_plus_financial_target": core | {financial},
        "core_plus_both": core | {climate, financial},
    }


def _feature_metadata(row: dict[str, Any], feature: str) -> dict[str, Any]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    fields = metadata.get("features")
    if not isinstance(fields, dict):
        return {}
    details = fields.get(feature)
    return details if isinstance(details, dict) else {}


def _observed(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Feature selection received a non-finite observation.")
    return not (isinstance(value, str) and not value.strip())


def fit_feature_mask(
    aligned: dict[str, dict[str, Any]],
    training_row_ids: list[str],
    ordered_features: list[str],
    registry: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Measure support using only one training partition and return an ordered mask."""
    if not training_row_ids:
        raise ValueError("Feature selection needs at least one training row.")
    if len(set(training_row_ids)) != len(training_row_ids):
        raise ValueError("Feature-selection training rows contain duplicates.")
    missing_rows = sorted(set(training_row_ids) - set(aligned))
    if missing_rows:
        raise ValueError(
            f"Feature selection cannot find training rows: {missing_rows[:3]}"
        )
    settings = config["training"]["feature_selection"]
    support_threshold = max(
        settings["minimum_observed_companies"],
        math.ceil(settings["minimum_observed_fraction"] * len(training_row_ids)),
    )
    active_features = []
    excluded_features = {}
    feature_support = {}
    ambiguous_statuses = set(settings["ambiguous_missing_statuses"])
    for feature in ordered_features:
        definition = registry.get(feature)
        if not isinstance(definition, dict):
            raise TypeError(f"Feature registry has no definition for {feature}.")
        values = [aligned[row_id]["X"].get(feature) for row_id in training_row_ids]
        observed_values = [value for value in values if _observed(value)]
        data_type = definition.get("data_type")
        distinct_values = len(set(observed_values))
        boolean_counts = (
            Counter(observed_values) if data_type == "boolean" else Counter()
        )
        ambiguous_counts = Counter()
        ambiguous_row_count = 0
        for row_id, value in zip(training_row_ids, values, strict=True):
            if _observed(value):
                continue
            details = _feature_metadata(aligned[row_id], feature)
            status = (
                str(details.get("source_missing_status") or details.get("status") or "")
                .strip()
                .casefold()
                .replace(" ", "_")
            )
            missing_reason = str(details.get("missing_reason") or "").strip()
            if not status and not missing_reason:
                status = "unspecified_null"
            row_statuses = {status} if status else set()
            normalized_reason = missing_reason.casefold().replace(" ", "_")
            row_statuses.update(
                candidate
                for candidate in ambiguous_statuses
                if candidate != "unspecified_null" and candidate in normalized_reason
            )
            matched_statuses = row_statuses & ambiguous_statuses
            if matched_statuses:
                ambiguous_row_count += 1
                ambiguous_counts.update(matched_statuses)
        support = {
            "training_row_count": len(training_row_ids),
            "minimum_observed_count": support_threshold,
            "observed_count": len(observed_values),
            "missing_count": len(training_row_ids) - len(observed_values),
            "distinct_value_count": distinct_values,
            "boolean_level_counts": {
                "false": boolean_counts.get(False, 0),
                "true": boolean_counts.get(True, 0),
            },
            "ambiguous_missing_reason_counts": dict(sorted(ambiguous_counts.items())),
            "ambiguous_missing_row_count": ambiguous_row_count,
            "ambiguous_missing_fraction": ambiguous_row_count / len(training_row_ids),
        }
        reasons = []
        if len(observed_values) < support_threshold:
            reasons.append("observed_count_below_minimum")
        if data_type in {"number", "integer"} and (
            distinct_values < settings["minimum_continuous_distinct_values"]
        ):
            reasons.append("continuous_distinct_values_below_minimum")
        if data_type == "boolean" and (
            boolean_counts.get(False, 0) < settings["minimum_boolean_count_per_level"]
            or boolean_counts.get(True, 0) < settings["minimum_boolean_count_per_level"]
        ):
            reasons.append("boolean_level_support_below_minimum")
        if (
            support["ambiguous_missing_fraction"]
            > settings["maximum_ambiguous_missing_fraction"]
        ):
            reasons.append("ambiguous_missing_fraction_above_maximum")
        feature_support[feature] = support
        if reasons:
            excluded_features[feature] = {"reasons": reasons, "support": support}
        else:
            active_features.append(feature)
    return {
        "version": VERSION,
        "training_row_ids": list(training_row_ids),
        "training_row_count": len(training_row_ids),
        "support_threshold": support_threshold,
        "active_features": active_features,
        "excluded_features": excluded_features,
        "feature_support": feature_support,
    }


def apply_feature_set(
    mask: dict[str, Any],
    feature_set: str,
    ordered_features: list[str],
    feature_categories: dict[str, str],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Restrict one training-only support mask to a declared category ablation."""
    feature_sets = configured_feature_sets(config)
    if feature_set not in feature_sets:
        raise ValueError(f"Unknown feature set: {feature_set!r}.")
    allowed_categories = feature_sets[feature_set]
    supported = set(mask["active_features"])
    active = [
        feature
        for feature in ordered_features
        if feature in supported and feature_categories[feature] in allowed_categories
    ]
    excluded = dict(mask["excluded_features"])
    for feature in ordered_features:
        if (
            feature in supported
            and feature_categories[feature] not in allowed_categories
        ):
            excluded[feature] = {
                "reasons": ["category_excluded_by_ablation"],
                "support": mask["feature_support"][feature],
            }
    return {
        "feature_set": feature_set,
        "included_categories": sorted(allowed_categories),
        "active_features": active,
        "excluded_features": excluded,
        "feature_support": mask["feature_support"],
        "support_threshold": mask["support_threshold"],
        "training_row_count": mask["training_row_count"],
    }


def choose_feature_set(
    results: dict[str, dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    """Choose by validation MAE and prefer simpler sets inside sampling uncertainty."""
    missing = sorted(set(FEATURE_SET_ORDER) - set(results))
    if missing:
        raise ValueError(f"Ablation results are missing feature sets: {missing}")
    valid = [
        (name, results[name])
        for name in FEATURE_SET_ORDER
        if results[name].get("mean_validation_mae") is not None
    ]
    if not valid:
        raise ValueError("No category ablation produced a validation score.")
    best_name, best = min(
        valid,
        key=lambda item: (
            item[1]["mean_validation_mae"],
            FEATURE_SET_ORDER.index(item[0]),
        ),
    )
    feature_sets = configured_feature_sets(config)
    uncertainty = float(best.get("standard_error") or 0)
    eligible = [
        (name, result)
        for name, result in valid
        if result["mean_validation_mae"] <= best["mean_validation_mae"] + uncertainty
    ]
    selected_name, selected = min(
        eligible,
        key=lambda item: (
            len(feature_sets[item[0]]),
            FEATURE_SET_ORDER.index(item[0]),
        ),
    )
    return {
        "feature_set": selected_name,
        "mean_validation_mae": selected["mean_validation_mae"],
        "best_observed_feature_set": best_name,
        "best_observed_mean_validation_mae": best["mean_validation_mae"],
        "uncertainty_tolerance": uncertainty,
        "selection_rule": (
            "Choose the simplest feature set within one configured standard error "
            "of the lowest mean validation MAE."
        ),
    }
