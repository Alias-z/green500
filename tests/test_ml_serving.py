"""Verify the cached saved-model service and its scenario audit trail."""

from __future__ import annotations

import json
import math
import pickle
from copy import deepcopy

import pytest

from green500.ml.serving import SavedModelService

ORDERED_FEATURES = [
    "financial_revenue_usd",
    "financial_operating_income_usd",
    "env_scope_1_tco2e",
    "env_renewable_electricity_percent",
    "social_employees_count",
    "climate_net_zero_target_year",
    "climate_target_scope_1_2_absolute_reduction_baseline_year",
    "climate_target_scope_1_2_absolute_reduction_target_year",
    "climate_target_scope_1_2_absolute_reduction_reduction_pct",
    "operating_margin",
    "industry",
]

FEATURE_CATEGORIES = {
    "financial_revenue_usd": "financial",
    "financial_operating_income_usd": "financial",
    "env_scope_1_tco2e": "environmental",
    "env_renewable_electricity_percent": "environmental",
    "social_employees_count": "social",
    "climate_net_zero_target_year": "climate_target",
    "climate_target_scope_1_2_absolute_reduction_baseline_year": "climate_target",
    "climate_target_scope_1_2_absolute_reduction_target_year": "climate_target",
    "climate_target_scope_1_2_absolute_reduction_reduction_pct": "climate_target",
    "operating_margin": "financial",
    "industry": "fixed_context",
}

ESG_FEATURES = [
    "financial_revenue_usd",
    "financial_operating_income_usd",
    "env_scope_1_tco2e",
    "env_renewable_electricity_percent",
    "social_employees_count",
    "operating_margin",
    "industry",
]

CSA_FEATURES = list(ORDERED_FEATURES)

FEATURE_UNITS = {
    "financial_revenue_usd": "USD",
    "financial_operating_income_usd": "USD",
    "env_scope_1_tco2e": "tCO2e",
    "env_renewable_electricity_percent": "percent",
    "social_employees_count": "count",
    "climate_net_zero_target_year": "year",
    "climate_target_scope_1_2_absolute_reduction_baseline_year": "year",
    "climate_target_scope_1_2_absolute_reduction_target_year": "year",
    "climate_target_scope_1_2_absolute_reduction_reduction_pct": "percent",
    "operating_margin": "ratio",
    "industry": None,
}


class SyntheticEbm:
    """Small identity-link additive model with observable prediction calls."""

    def __init__(self, features):
        self.feature_names_in_ = list(features)
        self.term_features_ = [(index,) for index in range(len(features))]
        self.intercept_ = [10.0]
        self.link_ = "identity"
        self.predict_calls = 0

    def get_params(self, deep=False):
        return {"interactions": 0}

    def eval_terms(self, frame):
        results = []
        for _, row in frame.iterrows():
            terms = []
            for feature_name in self.feature_names_in_:
                value = row[feature_name]
                if feature_name == "industry":
                    terms.append(2.0 if value == "Industrials" else -1.0)
                elif math.isnan(value):
                    terms.append(0.0)
                else:
                    terms.append(float(value) * 0.001)
            results.append(terms)
        return results

    def predict(self, frame):
        self.predict_calls += 1
        return [10.0 + sum(terms) for terms in self.eval_terms(frame)]


class SyntheticCatBoost:
    """Predictor that records the serving thread limit."""

    def __init__(self, features):
        self.feature_names_ = list(features)
        self.thread_counts = []

    def predict(self, frame, thread_count=None):
        self.thread_counts.append(thread_count)
        return [20.0 + index for index in range(len(frame))]


def _manifest():
    active = {"esg": ESG_FEATURES, "csa": CSA_FEATURES}
    return {
        "run_id": "synthetic-serving-run",
        "dataset_snapshot_id": "synthetic-dataset",
        "ordered_features": ORDERED_FEATURES,
        "categorical_features": ["industry"],
        "feature_categories": FEATURE_CATEGORIES,
        "active_features": active,
        "excluded_features": {
            target: {
                name: {
                    "reasons": ["Excluded by the synthetic training partition."],
                    "support": {"available_count": 0},
                }
                for name in ORDERED_FEATURES
                if name not in features
            }
            for target, features in active.items()
        },
        "numeric_training_ranges": {
            "env_scope_1_tco2e": {"min": 0, "max": 500},
            "env_renewable_electricity_percent": {"min": 0, "max": 100},
        },
        "known_industries": ["Industrials"],
        "models": {
            target: {
                family: {"path": f"models/{target}/{family}/model.pkl"}
                for family in ("ebm", "catboost")
            }
            for target in ("esg", "csa")
        },
        "files": {
            "dataset_schema.json": {"sha256": "verified-by-fixture", "byte_count": 1}
        },
        "targets": {
            "esg": {"status": "trained", "active_feature_count": len(ESG_FEATURES)},
            "csa": {"status": "trained", "active_feature_count": len(CSA_FEATURES)},
        },
        "limitations": ["Synthetic test artifacts do not measure model quality."],
    }


def _registry():
    features = {}
    for feature_name in ORDERED_FEATURES:
        data_type = "number"
        if (
            FEATURE_UNITS[feature_name] == "year"
            or feature_name == "social_employees_count"
        ):
            data_type = "integer"
        elif feature_name == "industry":
            data_type = "string"
        features[feature_name] = {
            "feature_name": feature_name,
            "source_JSON_path": f"$.{feature_name}",
            "data_type": data_type,
            "canonical_unit": FEATURE_UNITS[feature_name],
            "category_or_context": FEATURE_CATEGORIES[feature_name],
            "validation_rules": ["Use a finite value in the declared canonical unit."],
            "derived_feature_dependencies": (
                ["financial_operating_income_usd", "financial_revenue_usd"]
                if feature_name == "operating_margin"
                else []
            ),
            "categorical": feature_name == "industry",
            "predictive": True,
        }
    return {"version": "synthetic-registry", "features": features}


def _row(company_cik="0000000002", company_name="Second Company"):
    features = {
        "financial_revenue_usd": 100.0,
        "financial_operating_income_usd": 20.0,
        "env_scope_1_tco2e": 600.0,
        "env_renewable_electricity_percent": 40.0,
        "social_employees_count": 100,
        "climate_net_zero_target_year": 2050,
        "climate_target_scope_1_2_absolute_reduction_baseline_year": 2020,
        "climate_target_scope_1_2_absolute_reduction_target_year": 2030,
        "climate_target_scope_1_2_absolute_reduction_reduction_pct": 50.0,
        "operating_margin": 0.2,
        "industry": "Industrials",
    }
    return {
        "row_id": "row-" + company_cik,
        "company": {
            "company_cik": company_cik,
            "ticker": "SYN",
            "company_name": company_name,
        },
        "prediction_as_of": "2026-09-13",
        "availability_policy": "public_document_acquisition_fallback",
        "features": features,
        "metadata": {
            feature_name: {
                "canonical_unit": FEATURE_UNITS[feature_name],
                "reporting_year": 2025,
                "publication_date": "2026-03-01",
                "availability_date": "2026-03-01",
                "availability_basis": "publisher_publication_date",
                "public_document_acquired_at": "2026-03-02T00:00:00Z",
                "boundary": "Consolidated company",
                "confidence": 0.9,
                "evidence": {"quote": feature_name + " source"},
            }
            for feature_name in features
        },
        "coverage": {"overall": {"available_count": len(features)}},
        "warnings": ["Source snapshot warning."],
    }


@pytest.fixture
def saved_service(monkeypatch, tmp_path):
    run_manifest = _manifest()
    verification_calls = []
    monkeypatch.setattr(
        "green500.ml.serving.load_verified_run",
        lambda run_dir: verification_calls.append(run_dir) or run_manifest,
    )
    (tmp_path / "dataset_schema.json").write_text(
        json.dumps({"availability_policy": "public_document_acquisition_fallback"})
    )
    (tmp_path / "dataset_manifest.json").write_text(
        json.dumps({"availability_policy": "public_document_acquisition_fallback"})
    )
    (tmp_path / "feature_registry.json").write_text(json.dumps(_registry()))
    (tmp_path / "metrics.json").write_text(
        json.dumps(
            {
                "esg": {"final_test": {"models": {"ebm": {"mae": 7.0}}}},
                "csa": {"final_test": {"models": {"ebm": {"mae": 8.0}}}},
            }
        )
    )
    for target, features in (("esg", ESG_FEATURES), ("csa", CSA_FEATURES)):
        models = {
            "ebm": SyntheticEbm(features),
            "catboost": SyntheticCatBoost(features),
        }
        for family, model in models.items():
            path = tmp_path / "models" / target / family / "model.pkl"
            path.parent.mkdir(parents=True)
            path.write_bytes(pickle.dumps(model))
    service = SavedModelService(tmp_path)
    return service, verification_calls


def test_service_verifies_and_loads_once(saved_service, monkeypatch):
    service, verification_calls = saved_service
    assert verification_calls == [service.run_dir]
    assert len(service._models) == 4

    monkeypatch.setattr(
        "green500.ml.serving._load_saved_model",
        lambda path: pytest.fail("An evaluation tried to reload a model."),
    )
    service.evaluate_rows([_row()], "esg")
    service.evaluate_rows([_row()], "esg")

    catboost = service._models["esg", "catboost"]
    assert catboost.thread_counts == [1, 1]


def test_describe_uses_saved_active_contract_and_human_labels(saved_service):
    service, _ = saved_service
    description = service.describe()

    assert description["run_id"] == "synthetic-serving-run"
    assert description["targets"]["esg"]["active_features"] == ESG_FEATURES
    fields = {
        field["feature_name"]: field
        for field in description["targets"]["esg"]["fields"]
    }
    assert fields["financial_revenue_usd"] == {
        "feature_name": "financial_revenue_usd",
        "label": "Revenue",
        "canonical_unit": "USD",
        "category_or_context": "financial",
        "editable": True,
    }
    assert fields["operating_margin"]["editable"] is False
    assert fields["industry"]["editable"] is False
    assert (
        description["targets"]["esg"]["metrics"]["final_test"]["models"]["ebm"]["mae"]
        == 7.0
    )
    assert description["evaluation"]["dataset_snapshot_id"] == "synthetic-dataset"
    assert description["limitations"]
    assert description["categories"][0] == {
        "name": "financial",
        "label": "Financial",
    }


def test_evaluate_rows_preserves_order_filters_fields_and_reconstructs_ebm(
    saved_service,
):
    service, _ = saved_service
    rows = [
        _row("0000000002", "Second Company"),
        _row("0000000001", "First Company"),
    ]
    rows[1]["features"]["env_scope_1_tco2e"] = None
    source_rows = deepcopy(rows)

    results = service.evaluate_rows(rows, "esg", {"environmental": 2.0, "social": 0.5})

    assert rows == source_rows
    assert [item["company"]["company_cik"] for item in results] == [
        "0000000002",
        "0000000001",
    ]
    assert results[0]["company"]["name"] == "Second Company"
    assert results[0]["company"]["company_name"] == "Second Company"
    assert list(results[0]["features"]) == ESG_FEATURES
    assert list(results[0]["metadata"]) == ESG_FEATURES
    assert results[0]["predictions"]["catboost"] == 20.0
    assert results[1]["predictions"]["catboost"] == 21.0
    assert results[0]["predictions"]["ebm"] == pytest.approx(
        results[0]["intercept"]
        + results[0]["fixed_context_contribution"]
        + sum(results[0]["category_contributions"].values())
    )
    expected_personalized = (
        results[0]["intercept"]
        + results[0]["fixed_context_contribution"]
        + results[0]["category_contributions"]["financial"]
        + 0.5 * results[0]["category_contributions"]["social"]
        + 2.0 * results[0]["category_contributions"]["environmental"]
    )
    assert results[0]["personalized_index"] == pytest.approx(expected_personalized)
    assert (
        results[1]["coverage"]["available_active_feature_count"]
        == len(ESG_FEATURES) - 1
    )
    assert any("above the training range" in item for item in results[0]["warnings"])
    assert any(
        "active model inputs are missing" in item for item in results[1]["warnings"]
    )


def test_default_weights_reproduce_the_ebm_prediction(saved_service):
    service, _ = saved_service
    result = service.evaluate_rows([_row()], "esg")[0]
    assert result["personalized_index"] == pytest.approx(result["predictions"]["ebm"])


def test_prediction_only_batch_matches_full_saved_model_predictions(saved_service):
    service, _ = saved_service
    rows = [_row("0000000001", "First"), _row("0000000002", "Second")]
    expected = [result["predictions"] for result in service.evaluate_rows(rows, "esg")]
    assert service.predict_rows_only(rows, "esg") == expected
    assert service.predict_rows_only([], "esg") == []


def test_scenario_copies_rows_audits_changes_and_recomputes_derived_values(
    saved_service,
):
    service, _ = saved_service
    original = _row()
    original["features"]["env_scope_1_tco2e"] = None
    preserved = deepcopy(original)

    scenario_rows, changes = service.apply_scenario(
        [original],
        "esg",
        [
            {"feature_name": "financial_revenue_usd", "operation": "add", "value": 100},
            {"feature_name": "env_scope_1_tco2e", "operation": "percent", "value": -10},
        ],
    )

    assert original == preserved
    scenario = scenario_rows[0]
    assert scenario["features"]["financial_revenue_usd"] == 200.0
    assert scenario["features"]["operating_margin"] == pytest.approx(0.1)
    assert scenario["features"]["env_scope_1_tco2e"] is None
    assert changes == [
        {
            "company_cik": "0000000002",
            "feature_name": "financial_revenue_usd",
            "original_value": 100.0,
            "scenario_value": 200.0,
            "operation": "add",
            "amount": 100.0,
            "status": "applied",
        },
        {
            "company_cik": "0000000002",
            "feature_name": "env_scope_1_tco2e",
            "original_value": None,
            "scenario_value": None,
            "operation": "percent",
            "amount": -10.0,
            "status": "missing_original_value",
        },
    ]
    revenue_evidence = scenario["metadata"]["financial_revenue_usd"]["evidence"]
    assert revenue_evidence["quote"] == "financial_revenue_usd source"
    assert revenue_evidence["scenario"] is True
    assert scenario["metadata"]["financial_revenue_usd"]["status"] == "scenario"
    assert (
        scenario["metadata"]["financial_revenue_usd"]["source_missing_status"]
        == "hypothetical"
    )
    assert "model sensitivity" in scenario["warnings"][-1].casefold()


def test_set_can_populate_a_hypothetical_missing_value(saved_service, monkeypatch):
    service, _ = saved_service
    row = _row()
    row["features"]["env_scope_1_tco2e"] = None
    row["metadata"]["env_scope_1_tco2e"]["evidence"] = None
    monkeypatch.setattr(
        "green500.ml.dataset.recompute_derived_features",
        lambda features, metadata: (deepcopy(features), deepcopy(metadata)),
    )

    scenario_rows, changes = service.apply_scenario(
        [row],
        "esg",
        [{"feature_name": "env_scope_1_tco2e", "operation": "set", "value": 25}],
    )

    assert scenario_rows[0]["features"]["env_scope_1_tco2e"] == 25.0
    assert scenario_rows[0]["metadata"]["env_scope_1_tco2e"]["evidence"] == {
        "scenario": True
    }
    assert scenario_rows[0]["metadata"]["env_scope_1_tco2e"]["status"] == "scenario"
    assert (
        scenario_rows[0]["metadata"]["env_scope_1_tco2e"]["source_missing_status"]
        == "hypothetical"
    )
    assert changes[0]["status"] == "applied"


def test_scenario_does_not_derive_a_ratio_across_reporting_years(saved_service):
    service, _ = saved_service
    row = _row()
    row["metadata"]["financial_operating_income_usd"]["reporting_year"] = 2024

    scenario_rows, _ = service.apply_scenario(
        [row],
        "esg",
        [
            {
                "feature_name": "financial_revenue_usd",
                "operation": "set",
                "value": 200,
            }
        ],
    )

    assert scenario_rows[0]["features"]["operating_margin"] is None
    assert (
        "reporting years"
        in scenario_rows[0]["metadata"]["operating_margin"]["missing_reason"]
    )


def test_empty_scenario_returns_an_unchanged_deep_copy(saved_service):
    service, _ = saved_service
    row = _row()

    scenario_rows, changes = service.apply_scenario([row], "esg", [])

    assert scenario_rows == [row]
    assert scenario_rows[0] is not row
    assert scenario_rows[0]["features"] is not row["features"]
    assert changes == []


@pytest.mark.parametrize(
    ("target", "adjustments", "message"),
    [
        (
            "esg",
            [{"feature_name": "industry", "operation": "set", "value": 1}],
            "fixed context",
        ),
        (
            "esg",
            [{"feature_name": "operating_margin", "operation": "set", "value": 0.3}],
            "derived",
        ),
        (
            "esg",
            [
                {
                    "feature_name": "climate_net_zero_target_year",
                    "operation": "set",
                    "value": 2050,
                }
            ],
            "not active",
        ),
        (
            "esg",
            [
                {"feature_name": "env_scope_1_tco2e", "operation": "set", "value": 1},
                {"feature_name": "env_scope_1_tco2e", "operation": "add", "value": 1},
            ],
            "Duplicate",
        ),
        (
            "esg",
            [
                {
                    "feature_name": "env_scope_1_tco2e",
                    "operation": "set",
                    "value": float("nan"),
                }
            ],
            "finite",
        ),
        (
            "esg",
            [{"feature_name": "env_scope_1_tco2e", "operation": "set", "value": -1}],
            "negative",
        ),
        (
            "esg",
            [
                {
                    "feature_name": "env_renewable_electricity_percent",
                    "operation": "set",
                    "value": 101,
                }
            ],
            "0 to 100",
        ),
        (
            "esg",
            [
                {
                    "feature_name": "social_employees_count",
                    "operation": "set",
                    "value": 1.5,
                }
            ],
            "whole number",
        ),
        (
            "csa",
            [
                {
                    "feature_name": "climate_net_zero_target_year",
                    "operation": "set",
                    "value": 2101,
                }
            ],
            "1900 to 2100",
        ),
        (
            "csa",
            [
                {
                    "feature_name": "climate_target_scope_1_2_absolute_reduction_target_year",
                    "operation": "set",
                    "value": 2019,
                }
            ],
            "after its baseline",
        ),
    ],
)
def test_scenario_rejects_invalid_adjustments(
    saved_service, target, adjustments, message
):
    service, _ = saved_service
    with pytest.raises(ValueError, match=message):
        service.apply_scenario([_row()], target, adjustments)


def test_service_rejects_invalid_target_and_dates(saved_service):
    service, _ = saved_service
    with pytest.raises(ValueError, match="esg or csa"):
        service.evaluate_rows([_row()], "other")

    row = _row()
    row["prediction_as_of"] = "2026-02-30"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        service.evaluate_rows([row], "esg")
