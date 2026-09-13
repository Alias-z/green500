"""Verify sparse feature masks use only training-partition evidence."""

from copy import deepcopy

from green500.ml.config import load_ml_config
from green500.ml.feature_selection import (
    FEATURE_SET_ORDER,
    apply_feature_set,
    choose_feature_set,
    fit_feature_mask,
)


def _config():
    config = deepcopy(load_ml_config("config/ml.yaml"))
    selection = config["training"]["feature_selection"]
    selection["minimum_observed_companies"] = 3
    selection["minimum_observed_fraction"] = 0.10
    selection["minimum_continuous_distinct_values"] = 5
    selection["minimum_boolean_count_per_level"] = 5
    return config


def _registry():
    categories = {
        "good_number": "financial",
        "zero_support": "social",
        "low_support": "environmental",
        "low_variation": "environmental",
        "good_boolean": "climate_target",
        "bad_boolean": "financial_target",
        "ambiguous_number": "financial",
        "semantic_missing_number": "social",
        "industry": "fixed_context",
    }
    return {
        name: {
            "data_type": (
                "boolean"
                if name in {"good_boolean", "bad_boolean"}
                else "string"
                if name == "industry"
                else "number"
            ),
            "category_or_context": category,
        }
        for name, category in categories.items()
    }


def _aligned():
    rows = {}
    for index in range(10):
        row_id = f"row-{index}"
        features = {
            "good_number": float(index),
            "zero_support": None,
            "low_support": float(index) if index < 2 else None,
            "low_variation": float(index % 3),
            "good_boolean": index >= 5,
            "bad_boolean": index == 9,
            "ambiguous_number": None if index < 2 else float(index),
            "semantic_missing_number": None if index < 2 else float(index),
            "industry": "A" if index < 5 else "B",
        }
        metadata = {
            "features": {
                "ambiguous_number": {
                    "status": "missing" if index < 2 else "reported",
                    "source_missing_status": (
                        "not_extracted" if index < 2 else "observed"
                    ),
                    "missing_reason": "waiting for extraction" if index < 2 else None,
                },
                "semantic_missing_number": {
                    "status": "not_disclosed" if index < 2 else "reported",
                    "missing_reason": (
                        "The company did not disclose this value."
                        if index < 2
                        else None
                    ),
                },
            }
        }
        rows[row_id] = {"X": features, "metadata": metadata}
    return rows


def test_feature_mask_enforces_support_variation_boolean_and_missing_status_rules():
    aligned = _aligned()
    registry = _registry()
    ordered = list(registry)

    mask = fit_feature_mask(aligned, list(aligned), ordered, registry, _config())

    assert mask["active_features"] == [
        "good_number",
        "good_boolean",
        "semantic_missing_number",
        "industry",
    ]
    assert mask["support_threshold"] == 3
    assert mask["excluded_features"]["zero_support"]["reasons"] == [
        "observed_count_below_minimum",
        "continuous_distinct_values_below_minimum",
        "ambiguous_missing_fraction_above_maximum",
    ]
    assert (
        "continuous_distinct_values_below_minimum"
        in mask["excluded_features"]["low_variation"]["reasons"]
    )
    assert (
        "boolean_level_support_below_minimum"
        in mask["excluded_features"]["bad_boolean"]["reasons"]
    )
    assert (
        "ambiguous_missing_fraction_above_maximum"
        in mask["excluded_features"]["ambiguous_number"]["reasons"]
    )
    assert (
        mask["feature_support"]["ambiguous_number"]["ambiguous_missing_reason_counts"][
            "not_extracted"
        ]
        == 2
    )
    assert (
        mask["feature_support"]["semantic_missing_number"][
            "ambiguous_missing_row_count"
        ]
        == 0
    )


def test_validation_only_values_cannot_enter_the_training_mask():
    aligned = _aligned()
    registry = _registry()
    for row_id in ("row-8", "row-9"):
        aligned[row_id]["X"]["zero_support"] = 100.0

    mask = fit_feature_mask(
        aligned,
        [f"row-{index}" for index in range(8)],
        list(registry),
        registry,
        _config(),
    )

    assert "zero_support" not in mask["active_features"]
    assert mask["feature_support"]["zero_support"]["observed_count"] == 0


def test_dynamic_support_threshold_and_category_ablation_preserve_registry_order():
    config = _config()
    aligned = {
        f"row-{index}": {
            "X": {
                "good_number": float(index),
                "good_boolean": index % 2 == 0,
                "industry": "A",
            },
            "metadata": {"features": {}},
        }
        for index in range(250)
    }
    registry = {
        "good_number": {
            "data_type": "number",
            "category_or_context": "financial",
        },
        "good_boolean": {
            "data_type": "boolean",
            "category_or_context": "climate_target",
        },
        "industry": {"data_type": "string", "category_or_context": "fixed_context"},
    }
    mask = fit_feature_mask(aligned, list(aligned), list(registry), registry, config)

    assert mask["support_threshold"] == 25
    core = apply_feature_set(
        mask,
        "core",
        list(registry),
        {name: value["category_or_context"] for name, value in registry.items()},
        config,
    )
    climate = apply_feature_set(
        mask,
        "core_plus_climate_target",
        list(registry),
        {name: value["category_or_context"] for name, value in registry.items()},
        config,
    )
    assert core["active_features"] == ["good_number", "industry"]
    assert climate["active_features"] == list(registry)


def test_one_standard_error_rule_selects_the_simpler_feature_set():
    results = {
        name: {
            "mean_validation_mae": value,
            "standard_error": 0.20 if name == "core_plus_both" else 0.05,
        }
        for name, value in zip(FEATURE_SET_ORDER, (4.15, 4.10, 4.05, 4.00), strict=True)
    }

    selected = choose_feature_set(results, _config())

    assert selected["best_observed_feature_set"] == "core_plus_both"
    assert selected["feature_set"] == "core"
