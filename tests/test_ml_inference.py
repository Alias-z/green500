"""Verify saved-model prediction and immutable what-if scenarios."""

from __future__ import annotations

import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

from green500.ml.inference import predict_company, predict_feature_row
from green500.ml.scenario import predict_scenario

ORDERED_FEATURES = [
    "financial_revenue_usd",
    "financial_operating_income_usd",
    "operating_margin",
    "env_scope_1_tco2e",
    "industry",
]
FEATURE_CATEGORIES = {
    "financial_revenue_usd": "financial",
    "financial_operating_income_usd": "financial",
    "operating_margin": "financial",
    "env_scope_1_tco2e": "environmental",
    "industry": "fixed_context",
}


class SyntheticPredictionModel:
    """Small deterministic predictor used only to exercise saved-model plumbing."""

    def __init__(self, offset, feature_names):
        self.offset = offset
        self.feature_names_in_ = list(feature_names)
        self.seen_columns = []
        self.seen_rows = []

    def predict(self, frame):
        self.seen_columns.append(list(frame.columns))
        self.seen_rows.extend(frame.to_dict(orient="records"))
        results = []
        for _, row in frame.iterrows():
            margin = row.get("operating_margin", 0)
            emissions = row.get("env_scope_1_tco2e", 0)
            results.append(
                self.offset
                + (0 if math.isnan(margin) else margin)
                + (0 if math.isnan(emissions) else emissions / 1_000_000)
            )
        return results


def manifest(models=None):
    active_features = {
        "esg": [
            "financial_revenue_usd",
            "financial_operating_income_usd",
            "operating_margin",
            "industry",
        ],
        "csa": ["operating_margin", "env_scope_1_tco2e"],
    }
    excluded_features = {
        target: {
            feature: {
                "reasons": ["Insufficient training support in this synthetic fixture."],
                "support": {"available_count": 0},
            }
            for feature in ORDERED_FEATURES
            if feature not in active
        }
        for target, active in active_features.items()
    }
    return {
        "run_id": "synthetic-run",
        "dataset_snapshot_id": "synthetic-dataset",
        "ordered_features": ORDERED_FEATURES,
        "categorical_features": ["industry"],
        "feature_categories": FEATURE_CATEGORIES,
        "numeric_training_ranges": {
            "operating_margin": {"min": -1, "max": 1},
            "env_scope_1_tco2e": {"min": 0, "max": 500_000},
        },
        "known_industries": ["Industrials"],
        "active_features": active_features,
        "excluded_features": excluded_features,
        "models": models
        or {
            "esg": {"ebm": {"path": "unused"}, "catboost": {"path": "unused"}},
            "csa": {"ebm": {"path": "unused"}},
        },
    }


def company_row():
    return {
        "row_id": "row-1",
        "company": {"company_cik": "0000000001", "ticker": "SYN"},
        "prediction_as_of": "2025-06-30",
        "assessment_cycle": "2025",
        "features": {
            "financial_revenue_usd": 100.0,
            "financial_operating_income_usd": 20.0,
            "operating_margin": 0.2,
            "env_scope_1_tco2e": 600_000.0,
            "industry": "Synthetic industry",
        },
        "metadata": {
            "financial_revenue_usd": {"evidence": {"quote": "Revenue 100"}},
            "financial_operating_income_usd": {
                "evidence": {"quote": "Operating income 20"}
            },
        },
        "coverage": {"financial": {"available": 3, "total": 3}},
        "warnings": [],
    }


@pytest.fixture
def saved_models(monkeypatch):
    run_manifest = manifest()
    models = {
        ("esg", "ebm"): SyntheticPredictionModel(
            10, run_manifest["active_features"]["esg"]
        ),
        ("esg", "catboost"): SyntheticPredictionModel(
            20, run_manifest["active_features"]["esg"]
        ),
        ("csa", "ebm"): SyntheticPredictionModel(
            30, run_manifest["active_features"]["csa"]
        ),
    }
    monkeypatch.setattr(
        "green500.ml.artifacts.load_verified_run",
        lambda run_dir: {"manifest": run_manifest},
    )
    monkeypatch.setattr(
        "green500.ml.artifacts.load_model_artifact",
        lambda run_dir, target, family: models[(target, family)],
    )
    return models


def test_prediction_preserves_missing_target_independence_and_warns(saved_models, tmp_path):
    row = company_row()
    result = predict_feature_row(row, tmp_path)

    assert result["predictions"]["esg"] == pytest.approx(
        {"ebm": 10.2, "catboost": 20.2}
    )
    assert result["predictions"]["csa"]["ebm"] == pytest.approx(30.8)
    assert result["predictions"]["csa"]["catboost"] is None
    assert any("above the training range" in warning for warning in result["warnings"])
    assert any("not observed" in warning for warning in result["warnings"])
    assert any("No saved catboost model" in warning for warning in result["warnings"])
    assert result["feature_selection"]["esg"]["active_features"] == [
        "financial_revenue_usd",
        "financial_operating_income_usd",
        "operating_margin",
        "industry",
    ]
    assert "env_scope_1_tco2e" in result["feature_selection"]["esg"][
        "excluded_features"
    ]
    assert saved_models[("esg", "ebm")].seen_columns == [
        result["feature_selection"]["esg"]["active_features"]
    ]

    row_with_missing = deepcopy(row)
    row_with_missing["features"]["operating_margin"] = None
    missing_result = predict_feature_row(row_with_missing, tmp_path)
    assert missing_result["predictions"]["esg"]["ebm"] == 10.0
    assert missing_result["model_feature_coverage"]["esg"][
        "missing_active_features"
    ] == ["operating_margin"]
    assert math.isnan(saved_models[("esg", "ebm")].seen_rows[-1]["operating_margin"])


def test_excluded_values_do_not_enter_target_frame_or_range_warnings(
    saved_models, tmp_path
):
    row = company_row()
    result = predict_feature_row(row, tmp_path, targets=("esg",))

    assert "env_scope_1_tco2e" not in saved_models[("esg", "ebm")].seen_columns[-1]
    assert not any("above the training range" in item for item in result["warnings"])
    excluded = result["feature_selection"]["esg"]["excluded_features"]
    assert excluded["env_scope_1_tco2e"]["reasons"]
    assert excluded["env_scope_1_tco2e"]["support"] == {"available_count": 0}


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["active_features"].__setitem__(
            "esg", ["industry", "financial_revenue_usd"]
        ),
        lambda value: value["excluded_features"]["esg"].pop(
            "env_scope_1_tco2e"
        ),
    ],
)
def test_invalid_active_mask_or_exclusion_contract_fails(
    monkeypatch, tmp_path, change
):
    run_manifest = manifest()
    change(run_manifest)
    monkeypatch.setattr(
        "green500.ml.artifacts.load_verified_run", lambda run_dir: run_manifest
    )
    with pytest.raises(ValueError):
        predict_feature_row(company_row(), tmp_path, targets=("esg",))


def test_saved_model_feature_order_must_match_active_mask(monkeypatch, tmp_path):
    run_manifest = manifest(
        models={"esg": {"ebm": {"path": "unused"}}}
    )
    model = SyntheticPredictionModel(
        10, list(reversed(run_manifest["active_features"]["esg"]))
    )
    monkeypatch.setattr(
        "green500.ml.artifacts.load_verified_run", lambda run_dir: run_manifest
    )
    monkeypatch.setattr(
        "green500.ml.artifacts.load_model_artifact",
        lambda run_dir, target, family: model,
    )
    with pytest.raises(ValueError, match="feature order"):
        predict_feature_row(company_row(), tmp_path, targets=("esg",))


def test_predict_company_uses_exact_as_of_row_without_refitting(saved_models, tmp_path):
    calls = []

    def build_row(settings, company_id, prediction_as_of, assessment_cycle):
        calls.append((company_id, prediction_as_of, assessment_cycle))
        return company_row()

    result = predict_company(
        "0000000001",
        "2025-06-30",
        settings=SimpleNamespace(data_dir=tmp_path),
        model_run=tmp_path,
        assessment_cycle="2025",
        row_builder=build_row,
    )
    assert calls == [("0000000001", "2025-06-30", "2025")]
    assert result["model_version"] == "synthetic-run"
    assert result["data_cutoff"] == "2025-06-30"


def test_scenario_copies_observations_and_recomputes_dependencies(
    saved_models, monkeypatch, tmp_path
):
    original = company_row()
    preserved = deepcopy(original)
    registry = {
        "financial_revenue_usd": {
            "feature_name": "financial_revenue_usd",
            "data_type": "number",
            "category_or_context": "financial",
            "validation_rules": {"minimum": 0},
            "derived_feature_dependencies": [],
            "predictive": True,
        },
        "operating_margin": {
            "feature_name": "operating_margin",
            "data_type": "number",
            "category_or_context": "financial",
            "validation_rules": {},
            "derived_feature_dependencies": [
                "financial_operating_income_usd",
                "financial_revenue_usd",
            ],
            "predictive": True,
        },
    }

    def recompute(features, metadata):
        values, details = deepcopy(features), deepcopy(metadata)
        values["operating_margin"] = (
            values["financial_operating_income_usd"]
            / values["financial_revenue_usd"]
        )
        details["operating_margin"] = {"status": "derived_for_scenario"}
        return values, details

    monkeypatch.setattr("green500.ml.feature_registry.feature_registry", lambda: registry)
    monkeypatch.setattr(
        "green500.ml.dataset.recompute_derived_features", recompute
    )
    result = predict_scenario(
        "0000000001",
        {"financial_revenue_usd": 200.0},
        "2025-06-30",
        settings=SimpleNamespace(data_dir=tmp_path),
        model_run=tmp_path,
        row_builder=lambda *args, **kwargs: original,
    )

    assert original == preserved
    assert result["original"]["features"]["financial_revenue_usd"] == 100.0
    assert result["scenario"]["features"]["financial_revenue_usd"] == 200.0
    assert result["scenario"]["features"]["operating_margin"] == 0.1
    assert result["differences"]["esg"]["ebm"] == pytest.approx(-0.1)
    assert result["label"] == "Model sensitivity"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"unknown": 1}, "Unknown scenario"),
        ({"financial_revenue_usd": float("inf")}, "finite"),
        ({"operating_margin": 0.5}, "derived"),
    ],
)
def test_scenario_rejects_unknown_nonfinite_and_derived_overrides(
    saved_models, monkeypatch, tmp_path, overrides, message
):
    monkeypatch.setattr(
        "green500.ml.feature_registry.feature_registry",
        lambda: {
            "financial_revenue_usd": {
                "data_type": "number",
                "category_or_context": "financial",
                "validation_rules": {"minimum": 0},
                "derived_feature_dependencies": [],
                "predictive": True,
            },
            "operating_margin": {
                "data_type": "number",
                "category_or_context": "financial",
                "validation_rules": {},
                "derived_feature_dependencies": ["financial_revenue_usd"],
                "predictive": True,
            },
        },
    )
    with pytest.raises(ValueError, match=message):
        predict_scenario(
            "0000000001",
            overrides,
            "2025-06-30",
            settings=SimpleNamespace(data_dir=tmp_path),
            model_run=tmp_path,
            row_builder=lambda *args, **kwargs: company_row(),
        )
