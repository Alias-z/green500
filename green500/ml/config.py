"""Load and validate the fixed Green500 model search configuration."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

CONFIG_VERSION = "green500-ml-config-v1"
SPLIT_MODES = {"auto", "snapshot", "historical"}


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a YAML mapping.")
    return value


def _exact_keys(value: dict, expected: set[str], name: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ValueError(f"{name} keys differ; missing={missing}, extra={extra}.")


def _finite_number(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric.")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{name} is outside its supported range.")
    return number


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _two_values(value: object, name: str) -> list[int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must contain exactly two values.")
    values = [_positive_integer(item, name) for item in value]
    if len(set(values)) != 2:
        raise ValueError(f"{name} values must be distinct.")
    return values


def validate_ml_config(config: object) -> dict[str, Any]:
    """Reject settings that could silently change the required experiment design."""
    root = _mapping(config, "ML configuration")
    _exact_keys(
        root, {"version", "seed", "splits", "training", "artifacts"}, "ML configuration"
    )
    if root["version"] != CONFIG_VERSION:
        raise ValueError(f"Unsupported ML configuration version: {root['version']!r}.")
    if root["seed"] != 42:
        raise ValueError("The persisted experiment seed must be 42.")

    splits = _mapping(root["splits"], "splits")
    _exact_keys(splits, {"mode", "snapshot", "historical"}, "splits")
    if splits["mode"] not in SPLIT_MODES:
        raise ValueError("splits.mode must be auto, snapshot, or historical.")
    snapshot = _mapping(splits["snapshot"], "splits.snapshot")
    _exact_keys(
        snapshot, {"final_test_fraction", "validation_folds"}, "splits.snapshot"
    )
    fraction = _finite_number(
        snapshot["final_test_fraction"], "final_test_fraction", minimum=0
    )
    if not 0 < fraction < 1:
        raise ValueError("final_test_fraction must be between zero and one.")
    if snapshot["validation_folds"] != 3:
        raise ValueError("Snapshot estimation requires three validation folds.")
    historical = _mapping(splits["historical"], "splits.historical")
    _exact_keys(historical, {"maximum_validation_folds"}, "splits.historical")
    maximum_folds = _positive_integer(
        historical["maximum_validation_folds"], "maximum_validation_folds"
    )
    if maximum_folds > 3:
        raise ValueError("Historical validation supports at most three folds.")

    training = _mapping(root["training"], "training")
    _exact_keys(
        training,
        {"minimum_training_rows", "coverage_bins", "feature_selection", "models"},
        "training",
    )
    _positive_integer(training["minimum_training_rows"], "minimum_training_rows")
    coverage_bins = training["coverage_bins"]
    if not isinstance(coverage_bins, list) or len(coverage_bins) < 2:
        raise ValueError("coverage_bins must contain at least two boundaries.")
    normalized_bins = [
        _finite_number(value, "coverage_bins", minimum=0) for value in coverage_bins
    ]
    if (
        normalized_bins != sorted(set(normalized_bins))
        or normalized_bins[0] != 0
        or normalized_bins[-1] != 1
    ):
        raise ValueError("coverage_bins must be unique, sorted, and span zero to one.")

    selection = _mapping(training["feature_selection"], "training.feature_selection")
    _exact_keys(
        selection,
        {
            "minimum_observed_companies",
            "minimum_observed_fraction",
            "minimum_continuous_distinct_values",
            "minimum_boolean_count_per_level",
            "maximum_ambiguous_missing_fraction",
            "ambiguous_missing_statuses",
            "core_categories",
            "optional_categories",
            "use_one_standard_error_rule",
        },
        "training.feature_selection",
    )
    _positive_integer(
        selection["minimum_observed_companies"], "minimum_observed_companies"
    )
    observed_fraction = _finite_number(
        selection["minimum_observed_fraction"],
        "minimum_observed_fraction",
        minimum=0,
    )
    if not 0 < observed_fraction <= 1:
        raise ValueError(
            "minimum_observed_fraction must be above zero and at most one."
        )
    _positive_integer(
        selection["minimum_continuous_distinct_values"],
        "minimum_continuous_distinct_values",
    )
    _positive_integer(
        selection["minimum_boolean_count_per_level"],
        "minimum_boolean_count_per_level",
    )
    ambiguous_fraction = _finite_number(
        selection["maximum_ambiguous_missing_fraction"],
        "maximum_ambiguous_missing_fraction",
        minimum=0,
    )
    if ambiguous_fraction > 1:
        raise ValueError("maximum_ambiguous_missing_fraction cannot exceed one.")
    reason_terms = selection["ambiguous_missing_statuses"]
    if (
        not isinstance(reason_terms, list)
        or not reason_terms
        or any(
            not isinstance(term, str) or not term.strip() or term != term.casefold()
            for term in reason_terms
        )
        or len(set(reason_terms)) != len(reason_terms)
    ):
        raise ValueError(
            "ambiguous_missing_statuses must be unique non-empty lowercase text."
        )
    approved_categories = {
        "financial",
        "social",
        "environmental",
        "climate_target",
        "financial_target",
        "fixed_context",
    }
    core_categories = selection["core_categories"]
    optional_categories = selection["optional_categories"]
    if (
        not isinstance(core_categories, list)
        or set(core_categories)
        != {"financial", "social", "environmental", "fixed_context"}
        or len(core_categories) != 4
    ):
        raise ValueError("core_categories must contain the four fixed core categories.")
    if not isinstance(optional_categories, list) or optional_categories != [
        "climate_target",
        "financial_target",
    ]:
        raise ValueError(
            "optional_categories must be climate_target followed by financial_target."
        )
    if (set(core_categories) | set(optional_categories)) != approved_categories:
        raise ValueError(
            "Feature-selection categories do not cover the approved registry."
        )
    if selection["use_one_standard_error_rule"] is not True:
        raise ValueError("use_one_standard_error_rule must remain true.")

    models = _mapping(training["models"], "training.models")
    _exact_keys(models, {"ebm", "catboost"}, "training.models")
    ebm = _mapping(models["ebm"], "training.models.ebm")
    catboost = _mapping(models["catboost"], "training.models.catboost")
    for name, model in (("ebm", ebm), ("catboost", catboost)):
        _exact_keys(model, {"fixed", "search"}, f"training.models.{name}")
        _mapping(model["fixed"], f"training.models.{name}.fixed")
        _mapping(model["search"], f"training.models.{name}.search")

    ebm_fixed = ebm["fixed"]
    _exact_keys(
        ebm_fixed,
        {
            "objective",
            "learning_rate",
            "interactions",
            "min_samples_leaf",
            "validation_size",
            "outer_bags",
            "early_stopping_rounds",
            "random_state",
            "n_jobs",
        },
        "training.models.ebm.fixed",
    )
    required_ebm = {
        "objective": "rmse",
        "learning_rate": 0.04,
        "interactions": 0,
        "min_samples_leaf": 10,
        "validation_size": 0,
        "outer_bags": 1,
        "early_stopping_rounds": 0,
        "random_state": 42,
    }
    for key, expected in required_ebm.items():
        if ebm_fixed[key] != expected:
            raise ValueError(f"training.models.ebm.fixed.{key} must be {expected!r}.")
    _two_values(ebm["search"].get("max_bins"), "max_bins")
    _two_values(ebm["search"].get("max_rounds"), "max_rounds")
    _exact_keys(ebm["search"], {"max_bins", "max_rounds"}, "training.models.ebm.search")

    cat_fixed = catboost["fixed"]
    _exact_keys(
        cat_fixed,
        {
            "loss_function",
            "learning_rate",
            "l2_leaf_reg",
            "random_seed",
            "allow_writing_files",
            "verbose",
            "thread_count",
        },
        "training.models.catboost.fixed",
    )
    required_catboost = {
        "loss_function": "RMSE",
        "learning_rate": 0.05,
        "l2_leaf_reg": 10,
        "random_seed": 42,
        "allow_writing_files": False,
        "verbose": False,
    }
    for key, expected in required_catboost.items():
        if cat_fixed[key] != expected:
            raise ValueError(
                f"training.models.catboost.fixed.{key} must be {expected!r}."
            )
    _two_values(catboost["search"].get("depth"), "depth")
    _two_values(catboost["search"].get("iterations"), "iterations")
    _exact_keys(
        catboost["search"], {"depth", "iterations"}, "training.models.catboost.search"
    )

    artifacts = _mapping(root["artifacts"], "artifacts")
    _exact_keys(artifacts, {"output_directory"}, "artifacts")
    if (
        not isinstance(artifacts["output_directory"], str)
        or not artifacts["output_directory"].strip()
    ):
        raise ValueError("artifacts.output_directory must be a non-empty path.")
    return root


def load_ml_config(path: str | Path) -> dict[str, Any]:
    """Load one YAML file through safe parsing and enforce the training contract."""
    path = Path(path)
    try:
        config = yaml.safe_load(path.read_text())
    except OSError as error:
        raise FileNotFoundError(f"ML configuration does not exist: {path}") from error
    except yaml.YAMLError as error:
        raise ValueError(f"ML configuration is invalid YAML: {path}") from error
    return validate_ml_config(config)
