"""Verify all-company renewable score, rank and sector-share changes."""

import copy

import pytest

from green500.ml.renewable_scenario import evaluate_renewable_scenario


class ScenarioService:
    def predict_rows_only(self, rows, target):
        assert target == "csa"
        results = []
        for row in rows:
            features = row["features"]
            renewable = features["env_renewable_electricity_percent"] or 0
            score = features["base"] + features["slope"] * renewable
            results.append({"ebm": score, "catboost": score + 1})
        return results


def company(cik, name, sector, renewable, base, slope):
    renewable_for_score = renewable or 0
    score = base + slope * renewable_for_score
    return {
        "company": {
            "company_cik": cik,
            "name": name,
            "ticker": name[0],
            "industry": sector,
        },
        "features": {
            "env_renewable_electricity_percent": renewable,
            "base": base,
            "slope": slope,
        },
        "availability_policy": "test",
        "original_predictions": {"ebm": score, "catboost": score + 1},
    }


def rows():
    return [
        company("1", "Alpha", "Technology", 50, 5, 0.1),
        company("2", "Beta", "Energy", 80, 17, -0.1),
        company("3", "Gamma", "Technology", None, 8, 0),
    ]


def test_scenario_caps_values_reranks_and_preserves_negative_changes():
    source = rows()
    before = copy.deepcopy(source)
    result = evaluate_renewable_scenario(ScenarioService(), source, "csa", "ebm", 1.5)
    assert source == before
    assert result["step_summary"] == {
        "company_count": 3,
        "observed_count": 2,
        "missing_count": 1,
        "changed_count": 2,
        "saturated_count": 1,
        "increaseable_count": 1,
    }
    by_name = {item["company"]["name"]: item for item in result["companies"]}
    assert by_name["Alpha"]["scenario_renewable_pct"] == 75
    assert by_name["Alpha"]["score_change"] == pytest.approx(2.5)
    assert by_name["Beta"]["scenario_renewable_pct"] == 100
    assert by_name["Beta"]["score_change"] == pytest.approx(-2)
    assert by_name["Beta"]["original_rank"] == 2
    assert by_name["Beta"]["scenario_rank"] == 3
    assert by_name["Gamma"]["scenario_renewable_pct"] is None
    assert by_name["Gamma"]["score_change"] == 0


def test_sector_score_shares_and_fund_amounts_reconcile_exactly():
    result = evaluate_renewable_scenario(ScenarioService(), rows(), "csa", "ebm", 1.5)
    assert sum(
        item["scenario_share_pct"] for item in result["sectors"]
    ) == pytest.approx(100)
    assert sum(item["fund_amount_usd"] for item in result["sectors"]) == 1_000_000_000
    sectors = {item["sector"]: item for item in result["sectors"]}
    assert sectors["Technology"]["direction"] == "up"
    assert sectors["Energy"]["direction"] == "down"
    assert sectors["Technology"]["share_change_percentage_points"] > 0
    assert sectors["Energy"]["share_change_percentage_points"] < 0


def test_multiplier_one_reproduces_original_scores_and_ranks():
    result = evaluate_renewable_scenario(ScenarioService(), rows(), "csa", "ebm", 1)
    assert result["step_summary"]["changed_count"] == 0
    assert all(item["score_change"] == 0 for item in result["companies"])
    assert all(
        item["original_rank"] == item["scenario_rank"] for item in result["companies"]
    )
    assert all(
        item["share_change_percentage_points"] == pytest.approx(0)
        for item in result["sectors"]
    )


@pytest.mark.parametrize("multiplier", [0.99, float("nan")])
def test_invalid_multiplier_is_rejected(multiplier):
    with pytest.raises(ValueError, match="renewable_multiplier"):
        evaluate_renewable_scenario(ScenarioService(), rows(), "csa", "ebm", multiplier)


def test_large_multiplier_is_allowed_and_caps_observed_values():
    result = evaluate_renewable_scenario(
        ScenarioService(), rows(), "csa", "ebm", 1_000_000
    )
    observed = [
        item["scenario_renewable_pct"]
        for item in result["companies"]
        if item["original_renewable_pct"] is not None
    ]
    assert observed == [100, 100]
