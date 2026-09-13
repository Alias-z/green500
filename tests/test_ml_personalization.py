"""Verify signed EBM contribution weighting and ranking boundaries."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from green500.ml.personalization import personalize_feature_rows, validate_weights

FEATURES = [
    "financial_value",
    "social_value",
    "environmental_value",
    "climate_target_value",
    "financial_target_value",
    "industry",
]
CATEGORIES = {
    "financial_value": "financial",
    "social_value": "social",
    "environmental_value": "environmental",
    "climate_target_value": "climate_target",
    "financial_target_value": "financial_target",
    "industry": "fixed_context",
}


class SyntheticEbm:
    """Identity-link additive model used only to test contribution arithmetic."""

    def __init__(self):
        self.link_ = "identity"
        self.intercept_ = [10.0]
        self.feature_names_in_ = [
            "financial_value",
            "environmental_value",
            "financial_target_value",
            "industry",
        ]
        self.term_features_ = [(0,), (1,), (2,), (3,)]

    def get_params(self, deep=False):
        return {"interactions": 0}

    def eval_terms(self, frame):
        multipliers = {
            "financial_value": 1,
            "environmental_value": 3,
            "financial_target_value": 5,
        }
        rows = []
        for _, row in frame.iterrows():
            rows.append([
                row[name] * multipliers[name]
                if name != "industry"
                else (2.0 if row[name] == "Industrials" else -1.0)
                for name in self.feature_names_in_
            ])
        return rows

    def predict(self, frame):
        return [10.0 + sum(terms) for terms in self.eval_terms(frame)]


def manifest():
    active = [
        "financial_value",
        "environmental_value",
        "financial_target_value",
        "industry",
    ]
    return {
        "run_id": "synthetic-run",
        "dataset_snapshot_id": "synthetic-dataset",
        "ordered_features": FEATURES,
        "categorical_features": ["industry"],
        "feature_categories": CATEGORIES,
        "numeric_training_ranges": {},
        "known_industries": ["Industrials"],
        "files": {"dataset_schema.json": {"sha256": "unused", "byte_count": 0}},
        "active_features": {"esg": active},
        "excluded_features": {
            "esg": {
                feature: {
                    "reasons": ["Insufficient synthetic training support."],
                    "support": {"available_count": 0},
                }
                for feature in FEATURES
                if feature not in active
            }
        },
        "models": {"esg": {"ebm": {"path": "unused"}}},
    }


def rows():
    return [
        {
            "row_id": "b",
            "company": {"company_cik": "0000000002", "ticker": "TWO"},
            "prediction_as_of": "2025-06-30",
            "availability_policy": "public_document_acquisition_fallback",
            "features": {
                "financial_value": 1.0,
                "social_value": 1.0,
                "environmental_value": 1.0,
                "climate_target_value": 1.0,
                "financial_target_value": 1.0,
                "industry": "Industrials",
            },
            "coverage": {},
            "warnings": [],
        },
        {
            "row_id": "a",
            "company": {"company_cik": "0000000001", "ticker": "ONE"},
            "prediction_as_of": "2025-06-30",
            "availability_policy": "public_document_acquisition_fallback",
            "features": {
                "financial_value": 0.0,
                "social_value": 0.0,
                "environmental_value": 1.0,
                "climate_target_value": 0.0,
                "financial_target_value": 0.0,
                "industry": "Industrials",
            },
            "coverage": {},
            "warnings": [],
        },
    ]


@pytest.fixture
def synthetic_run(monkeypatch, tmp_path):
    (tmp_path / "dataset_schema.json").write_text(
        json.dumps({"availability_policy": "public_document_acquisition_fallback"})
    )
    (tmp_path / "dataset_manifest.json").write_text(
        json.dumps({"availability_policy": "public_document_acquisition_fallback"})
    )
    model = SyntheticEbm()
    monkeypatch.setattr(
        "green500.ml.artifacts.load_verified_run",
        lambda run_dir: {"manifest": manifest()},
    )
    monkeypatch.setattr(
        "green500.ml.artifacts.load_model_artifact",
        lambda run_dir, target, family: model,
    )
    return model


def test_default_multipliers_reconstruct_raw_prediction(synthetic_run, tmp_path):
    source_rows = rows()
    preserved = deepcopy(source_rows)
    result = personalize_feature_rows(source_rows, {}, "esg", tmp_path)

    assert source_rows == preserved
    assert result["label"] == "Personalized Green500 index"
    assert result["weights"] == {
        "financial": 1.0,
        "social": 1.0,
        "environmental": 1.0,
        "climate_target": 1.0,
        "financial_target": 1.0,
    }
    assert all(
        item["personalized_index"] == pytest.approx(item["raw_ebm_prediction"])
        for item in result["companies"]
    )
    assert [item["rank"] for item in result["companies"]] == [1, 2]
    first = result["companies"][0]
    assert first["category_contributions"]["social"] == 0
    assert first["category_details"]["social"] == {
        "contribution": 0.0,
        "active_features": [],
        "excluded_features": {
            "social_value": {
                "reasons": ["Insufficient synthetic training support."],
                "support": {"available_count": 0},
            }
        },
        "category_excluded_from_model": True,
    }


def test_excluded_category_multiplier_keeps_zero_contribution(
    synthetic_run, tmp_path
):
    baseline = personalize_feature_rows(rows()[:1], {}, "esg", tmp_path)[
        "companies"
    ][0]
    weighted = personalize_feature_rows(
        rows()[:1], {"social": 3.0}, "esg", tmp_path
    )["companies"][0]

    assert weighted["category_contributions"]["social"] == 0
    assert weighted["category_details"]["social"][
        "category_excluded_from_model"
    ] is True
    assert weighted["personalized_index"] == pytest.approx(
        baseline["personalized_index"]
    )


def test_one_multiplier_changes_only_its_signed_category_term(synthetic_run, tmp_path):
    default = personalize_feature_rows(rows()[:1], {}, "esg", tmp_path)["companies"][0]
    weighted = personalize_feature_rows(
        rows()[:1], {"environmental": 2.5}, "esg", tmp_path
    )["companies"][0]

    expected_change = 1.5 * default["category_contributions"]["environmental"]
    assert weighted["personalized_index"] - default["personalized_index"] == pytest.approx(
        expected_change
    )
    assert weighted["category_contributions"] == default["category_contributions"]
    assert weighted["fixed_context_contribution"] == default["fixed_context_contribution"]


@pytest.mark.parametrize(
    "weights",
    [
        {"environmental": -0.1},
        {"environmental": 3.1},
        {"environmental": float("nan")},
        {"unknown": 1.0},
    ],
)
def test_invalid_multipliers_fail(weights):
    with pytest.raises(ValueError):
        validate_weights(weights)


def test_interaction_or_nonidentity_ebm_is_rejected(
    synthetic_run, monkeypatch, tmp_path
):
    synthetic_run.term_features_ = [(0, 1)]
    with pytest.raises(ValueError, match="interaction"):
        personalize_feature_rows(rows()[:1], {}, "esg", tmp_path)

    synthetic_run.term_features_ = [(0,), (1,), (2,), (3,)]
    synthetic_run.link_ = "log"
    with pytest.raises(ValueError, match="identity"):
        personalize_feature_rows(rows()[:1], {}, "esg", tmp_path)
