"""Verify bounded one-field searches for a desired model estimate."""

import copy
from typing import ClassVar

import pytest

from green500.ml.target_suggestions import suggest_target_adjustments


class LinearService:
    manifest: ClassVar = {
        "ordered_features": ["metric", "industry"],
        "feature_categories": {
            "metric": "environmental",
            "industry": "fixed_context",
        },
        "active_features": {"esg": ["metric", "industry"]},
        "excluded_features": {"esg": {}},
        "numeric_training_ranges": {"metric": {"min": 0, "max": 100}},
    }
    registry: ClassVar = {
        "metric": {
            "category_or_context": "environmental",
            "derived_feature_dependencies": [],
            "data_type": "number",
            "canonical_unit": "percent",
        },
        "industry": {
            "category_or_context": "fixed_context",
            "derived_feature_dependencies": [],
            "data_type": "string",
            "canonical_unit": None,
        },
    }

    def evaluate_rows(self, rows, target):
        assert target == "esg"
        results = []
        for row in rows:
            value = row["features"]["metric"]
            score = float(value) if value is not None else 0.0
            results.append(
                {
                    "company": copy.deepcopy(row["company"]),
                    "predictions": {"ebm": score, "catboost": score / 2},
                }
            )
        return results

    def predict_rows_only(self, rows, target):
        return [result["predictions"] for result in self.evaluate_rows(rows, target)]

    def apply_scenario(self, rows, target, adjustments):
        changed = copy.deepcopy(rows)
        adjustment = adjustments[0]
        original = changed[0]["features"][adjustment["feature_name"]]
        changed[0]["features"][adjustment["feature_name"]] = adjustment["value"]
        return changed, [
            {
                "company_cik": changed[0]["company"]["company_cik"],
                "feature_name": adjustment["feature_name"],
                "original_value": original,
                "scenario_value": adjustment["value"],
                "operation": "set",
                "amount": adjustment["value"],
                "status": "applied",
            }
        ]

    def describe(self):
        return {
            "targets": {
                "esg": {
                    "fields": [
                        {"feature_name": "metric", "label": "Renewable electricity"},
                        {"feature_name": "industry", "label": "Industry"},
                    ]
                }
            }
        }


def row(value=10):
    return {
        "company": {"company_cik": "0000000001", "name": "Example"},
        "features": {"metric": value, "industry": "Utilities"},
    }


def test_search_returns_real_one_field_prediction_and_preserves_source_row():
    source = row()
    before = copy.deepcopy(source)
    result = suggest_target_adjustments(
        LinearService(), source, "esg", 50, "ebm", {"metric": [0, 25, 50, 75, 100]}
    )
    assert source == before
    assert result["desired_direction"] == "increase"
    assert result["suggestions"][0] == {
        "feature_name": "metric",
        "label": "Renewable electricity",
        "category": "environmental",
        "unit": "percent",
        "current_value": 10,
        "suggested_value": 50.0,
        "absolute_change": 40.0,
        "relative_change_pct": 400.0,
        "percentage_point_change": 40.0,
        "operation": "set",
        "projected_predictions": {"ebm": 50.0, "catboost": 25.0},
        "selected_model_change": 40.0,
        "remaining_gap": 0.0,
        "reaches_target": True,
        "models_agree": True,
        "range_position": 0.5,
        "warning": None,
    }


def test_equal_target_and_invalid_requests_are_explicit():
    result = suggest_target_adjustments(
        LinearService(), row(), "esg", 10, "ebm", {"metric": [10]}
    )
    assert result["suggestions"] == []
    assert "already" in result["warnings"][0]
    for arguments in [
        ("unknown", 50, "ebm", 5),
        ("esg", -1, "ebm", 5),
        ("esg", 50, "other", 5),
        ("esg", 50, "ebm", 0),
    ]:
        with pytest.raises(ValueError):
            suggest_target_adjustments(
                LinearService(),
                row(),
                arguments[0],
                arguments[1],
                arguments[2],
                {"metric": [50]},
                max_suggestions=arguments[3],
            )


def test_missing_editable_value_cannot_be_presented_as_a_tweak():
    with pytest.raises(ValueError, match="No observed editable field"):
        suggest_target_adjustments(
            LinearService(), row(None), "esg", 50, "ebm", {"metric": [50]}
        )
