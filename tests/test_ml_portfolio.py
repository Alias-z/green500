"""Verify deterministic, bounded portfolio allocation."""

from __future__ import annotations

from copy import deepcopy

import pytest

from green500.ml.portfolio import (
    allocate_portfolio,
    optimize_financial_resilience_portfolio,
)


def constraints(**changes):
    values = {
        "fund_usd": 1_000_000_000,
        "max_companies": 10,
        "max_company_weight": 0.2,
        "max_industry_weight": 0.4,
        "min_coverage": 0.3,
    }
    values.update(changes)
    return values


def company(
    number: int,
    index: float,
    industry: str,
    *,
    available: int = 4,
    active: int = 5,
    contributions: dict[str, float] | None = None,
    features: dict | None = None,
):
    return {
        "company": {
            "company_cik": f"{number:010d}",
            "name": f"Company {number}",
            "ticker": f"C{number}",
            "industry": industry,
        },
        "predictions": {"ebm": 50.0 + index, "catboost": 49.0 + index},
        "personalized_index": index,
        "category_contributions": contributions
        if contributions is not None
        else {"environmental": index / 2, "climate_target": -index / 4},
        "coverage": {
            "active_feature_count": active,
            "available_active_feature_count": available,
        },
        "warnings": [],
        "features": features or {},
    }


def test_allocates_full_fund_without_exceeding_company_or_industry_caps():
    evaluations = [
        company(
            number, 100 - number, ["Energy", "Technology", "Industrials"][number % 3]
        )
        for number in range(1, 11)
    ]

    result = allocate_portfolio(evaluations, constraints())

    assert result["allocated_usd"] == 1_000_000_000
    assert result["unallocated_usd"] == 0
    assert len(result["allocations"]) == 10
    assert all(allocation["weight"] <= 0.2 for allocation in result["allocations"])
    assert all(weight <= 0.4 for weight in result["industry_weights"].values())
    assert (
        sum(allocation["amount_usd"] for allocation in result["allocations"])
        == 1_000_000_000
    )
    assert result["allocation_method"] == "ranked_constrained_equal_allocation"


def test_scans_past_an_industry_at_capacity_to_select_lower_ranked_industries():
    evaluations = [company(number, 100 - number, "Energy") for number in range(1, 9)]
    evaluations.extend(
        [company(number, 30 - number, "Technology") for number in range(9, 12)]
    )
    evaluations.extend(
        [company(number, 20 - number, "Industrials") for number in range(12, 15)]
    )

    result = allocate_portfolio(evaluations, constraints())

    selected = result["allocations"]
    assert len(selected) == 10
    assert result["industry_weights"] == {
        "Energy": 0.4,
        "Industrials": 0.3,
        "Technology": 0.3,
    }
    assert {item["company_cik"] for item in selected} >= {"0000000009", "0000000010"}
    assert result["allocated_usd"] == 1_000_000_000


def test_impossible_constraints_leave_cash_unallocated():
    evaluations = [company(number, 10 - number, "Energy") for number in range(1, 7)]

    result = allocate_portfolio(evaluations, constraints())

    assert result["allocated_usd"] == 400_000_000
    assert result["unallocated_usd"] == 600_000_000
    assert result["industry_weights"] == {"Energy": 0.4}
    assert len(result["allocations"]) == 6
    allocation_spread = max(item["amount_usd"] for item in result["allocations"]) - min(
        item["amount_usd"] for item in result["allocations"]
    )
    assert allocation_spread == pytest.approx(0.01)
    assert "remains unallocated" in result["warnings"][-1]


def test_zero_and_sparse_coverage_are_excluded_without_division_by_zero():
    evaluations = [
        company(1, 3, "Energy", active=0, available=0),
        company(2, 2, "Technology", active=10, available=2),
        company(3, 1, "Industrials", active=10, available=3),
    ]

    result = allocate_portfolio(evaluations, constraints())

    assert result["eligible_count"] == 1
    assert result["excluded_count"] == 2
    assert [item["company_cik"] for item in result["allocations"]] == ["0000000003"]
    assert result["allocated_usd"] == 200_000_000


def test_negative_indices_retain_signed_descending_rank_and_category_reason():
    evaluations = [
        company(2, -3, "Energy", contributions={"social": -2.0}),
        company(1, -3, "Technology", contributions={"environmental": 1.5}),
        company(3, -1, "Industrials", contributions={"climate_target": -4.0}),
    ]

    result = allocate_portfolio(evaluations, constraints())

    assert [(item["company_cik"], item["rank"]) for item in result["allocations"]] == [
        ("0000000003", 1),
        ("0000000001", 2),
        ("0000000002", 3),
    ]
    assert "climate_target -4" in result["allocations"][0]["reason"]
    assert (
        "Rank 1 by signed personalized index -1" in result["allocations"][0]["reason"]
    )


def test_allocation_is_deterministic_and_does_not_mutate_input():
    evaluations = [
        company(3, 1, "Energy"),
        company(1, 1, "Technology"),
        company(2, 1, "Industrials"),
    ]
    original = deepcopy(evaluations)

    small_fund_constraints = constraints(fund_usd=100.00, max_company_weight=0.4)
    first = allocate_portfolio(evaluations, small_fund_constraints)
    second = allocate_portfolio(list(reversed(evaluations)), small_fund_constraints)

    assert evaluations == original
    assert first == second
    assert [item["amount_usd"] for item in first["allocations"]] == [
        33.34,
        33.33,
        33.33,
    ]
    assert first["allocated_usd"] == 100.00


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"fund_usd": float("nan")}, "fund_usd"),
        ({"fund_usd": 1.001}, "whole-cent"),
        ({"fund_usd": 1_000_000_000_001}, "fund_usd"),
        ({"max_companies": 0}, "max_companies"),
        ({"max_companies": 501}, "max_companies"),
        ({"max_company_weight": 1.1}, "max_company_weight"),
        ({"max_industry_weight": 0}, "max_industry_weight"),
        ({"min_coverage": -0.1}, "min_coverage"),
    ],
)
def test_constraint_bounds_are_validated(change, message):
    with pytest.raises(ValueError, match=message):
        allocate_portfolio([company(1, 1, "Energy")], constraints(**change))


def test_duplicate_company_identifiers_and_nonfinite_scores_are_rejected():
    duplicate = company(1, 2, "Technology")
    with pytest.raises(ValueError, match="Duplicate company_cik"):
        allocate_portfolio([company(1, 1, "Energy"), duplicate], constraints())

    invalid = company(1, 1, "Energy")
    invalid["personalized_index"] = float("inf")
    with pytest.raises(ValueError, match="personalized_index"):
        allocate_portfolio([invalid], constraints())


def test_available_coverage_cannot_exceed_active_coverage():
    with pytest.raises(ValueError, match="cannot exceed"):
        allocate_portfolio(
            [company(1, 1, "Energy", active=2, available=3)], constraints()
        )


def financial_features(margin, cash_flow, revenue, debt_assets):
    return {
        "operating_margin": margin,
        "financial_operating_cash_flow_usd": cash_flow,
        "financial_revenue_usd": revenue,
        "debt_assets": debt_assets,
    }


def test_financial_resilience_greedy_objective_and_caps_are_exact():
    evaluations = [
        company(1, 40, "A", features=financial_features(0.4, 30, 100, 0.1)),
        company(2, 30, "A", features=financial_features(0.3, 20, 100, 0.2)),
        company(3, 20, "B", features=financial_features(0.2, 10, 100, 0.3)),
        company(4, 10, "C", features=financial_features(0.1, 0, 100, 0.4)),
    ]

    result = optimize_financial_resilience_portfolio(
        evaluations,
        constraints(
            fund_usd=100,
            max_companies=4,
            max_company_weight=0.4,
            max_industry_weight=0.5,
        ),
        sustainability_eligible_fraction=1,
    )

    assert [item["amount_usd"] for item in result["allocations"]] == [40, 10, 40, 10]
    assert [item["objective_score"] for item in result["allocations"]] == pytest.approx(
        [1, 2 / 3, 1 / 3, 0]
    )
    assert result["industry_weights"] == {"A": 0.5, "B": 0.4, "C": 0.1}
    assert result["allocated_usd"] == 100
    assert result["portfolio_objective_score"] == pytest.approx(0.6)
    assert result["objective_name"] == "Financial resilience proxy"
    assert result["allocations"][1]["reason"] == (
        "Financial resilience #2; sustainability-eligible #2."
    )


def test_sustainability_cutoff_excludes_financially_stronger_lower_index_company():
    evaluations = [
        company(1, 40, "A", features=financial_features(0.1, 1, 100, 0.4)),
        company(2, 30, "B", features=financial_features(0.2, 2, 100, 0.3)),
        company(3, 20, "C", features=financial_features(0.3, 3, 100, 0.2)),
        company(4, 10, "D", features=financial_features(0.4, 4, 100, 0.1)),
    ]

    result = optimize_financial_resilience_portfolio(
        evaluations, constraints(fund_usd=100, max_company_weight=0.4)
    )

    assert result["sustainability_eligible_fraction"] == 0.75
    assert result["sustainability_cutoff"] == 20
    assert result["eligible_count"] == 3
    assert result["excluded_count"] == 1
    assert "0000000004" not in {item["company_cik"] for item in result["allocations"]}


def test_financial_resilience_normalizes_two_components_and_excludes_one():
    evaluations = [
        company(1, 3, "A", features=financial_features(0.3, None, None, 0.1)),
        company(2, 2, "B", features=financial_features(0.2, 10, 100, None)),
        company(3, 1, "C", features=financial_features(None, None, 100, 0.2)),
    ]

    result = optimize_financial_resilience_portfolio(
        evaluations, constraints(fund_usd=100), sustainability_eligible_fraction=1
    )

    assert result["eligible_count"] == 2
    assert result["excluded_count"] == 1
    first = next(
        item for item in result["allocations"] if item["company_cik"] == "0000000001"
    )
    assert set(first["component_scores"]) == {
        "operating_margin",
        "inverse_debt_assets",
    }
    assert any("fewer than two" in warning for warning in result["warnings"])


def test_financial_resilience_scans_saturated_industry_and_reports_cents():
    evaluations = [
        company(
            number,
            20 - number,
            "A" if number < 4 else "B",
            features=financial_features(1 - number / 10, 20 - number, 100, number / 10),
        )
        for number in range(1, 6)
    ]

    result = optimize_financial_resilience_portfolio(
        evaluations,
        constraints(
            fund_usd=1,
            max_companies=3,
            max_company_weight=0.3333,
            max_industry_weight=0.5,
        ),
        sustainability_eligible_fraction=1,
    )

    assert [item["company_cik"] for item in result["allocations"]] == [
        "0000000001",
        "0000000002",
        "0000000004",
    ]
    assert [item["amount_usd"] for item in result["allocations"]] == [0.33, 0.17, 0.33]
    assert result["allocated_usd"] == 0.83
    assert result["unallocated_usd"] == 0.17
    assert all(
        round(item["amount_usd"] * 100) == item["amount_usd"] * 100
        for item in result["allocations"]
    )
    assert "remains unallocated" in result["warnings"][-1]


def test_financial_resilience_is_deterministic_and_validates_fraction():
    evaluations = [
        company(2, 1, "B", features=financial_features(0.2, 20, 100, 0.2)),
        company(1, 1, "A", features=financial_features(0.2, 20, 100, 0.2)),
    ]

    first = optimize_financial_resilience_portfolio(
        evaluations, constraints(fund_usd=100), sustainability_eligible_fraction=1
    )
    second = optimize_financial_resilience_portfolio(
        list(reversed(evaluations)),
        constraints(fund_usd=100),
        sustainability_eligible_fraction=1,
    )
    assert first == second
    assert first["allocations"][0]["company_cik"] == "0000000001"

    with pytest.raises(ValueError, match="sustainability_eligible_fraction"):
        optimize_financial_resilience_portfolio(
            evaluations,
            constraints(),
            sustainability_eligible_fraction=0,
        )
