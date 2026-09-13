"""Evaluate a cumulative renewable-electricity scenario for a complete company snapshot."""

from __future__ import annotations

import copy
import math
from collections import defaultdict

FUND_USD = 1_000_000_000
RENEWABLE_FEATURE = "env_renewable_electricity_percent"


def _finite(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be a finite number.")
    return float(value)


def _rank(companies: list[dict], score_name: str) -> dict[str, int]:
    ordered = sorted(
        companies,
        key=lambda company: (
            -company[score_name],
            company["company"]["company_cik"],
        ),
    )
    return {
        company["company"]["company_cik"]: rank
        for rank, company in enumerate(ordered, 1)
    }


def _fund_amounts(shares: list[float]) -> list[float]:
    fund_cents = FUND_USD * 100
    exact = [share * fund_cents for share in shares]
    cents = [math.floor(value) for value in exact]
    remaining = fund_cents - sum(cents)
    order = sorted(
        range(len(exact)), key=lambda index: (-(exact[index] - cents[index]), index)
    )
    for index in order[:remaining]:
        cents[index] += 1
    return [value / 100 for value in cents]


def evaluate_renewable_scenario(
    service,
    rows: list[dict],
    target: str,
    model_family: str,
    renewable_multiplier: float,
) -> dict:
    """Return score, rank and sector-share changes while preserving published rows."""
    multiplier = _finite(renewable_multiplier, "renewable_multiplier")
    if multiplier < 1:
        raise ValueError("renewable_multiplier must be at least 1.")
    if target not in {"esg", "csa"}:
        raise ValueError("target must be esg or csa.")
    if model_family not in {"ebm", "catboost"}:
        raise ValueError("model_family must be ebm or catboost.")
    if not isinstance(rows, list) or not rows:
        raise ValueError("The renewable scenario needs at least one company row.")

    scenario_rows = []
    prepared = []
    seen = set()
    for position, source in enumerate(rows):
        if not isinstance(source, dict):
            raise TypeError(f"Scenario row {position} must be an object.")
        company = source.get("company")
        features = source.get("features")
        original_predictions = source.get("original_predictions")
        if not isinstance(company, dict) or not isinstance(features, dict):
            raise TypeError(f"Scenario row {position} lacks company or features.")
        if not isinstance(original_predictions, dict):
            raise TypeError(f"Scenario row {position} lacks original predictions.")
        company_cik = company.get("company_cik")
        if not isinstance(company_cik, str) or not company_cik:
            raise ValueError(f"Scenario row {position} has no company CIK.")
        if company_cik in seen:
            raise ValueError("The renewable scenario contains duplicate companies.")
        seen.add(company_cik)
        if RENEWABLE_FEATURE not in features:
            raise ValueError(f"Scenario row {position} lacks renewable electricity.")
        original_renewable = features[RENEWABLE_FEATURE]
        if original_renewable is None:
            scenario_renewable = None
        else:
            original_renewable = _finite(
                original_renewable, f"Scenario row {position} renewable electricity"
            )
            if not 0 <= original_renewable <= 100:
                raise ValueError(
                    "Published renewable electricity must be from 0 to 100."
                )
            scenario_renewable = min(100.0, original_renewable * multiplier)
        original_score = _finite(
            original_predictions.get(model_family),
            f"Scenario row {position} original {model_family} score",
        )
        scenario_features = copy.deepcopy(features)
        scenario_features[RENEWABLE_FEATURE] = scenario_renewable
        scenario_rows.append(
            {
                "features": scenario_features,
                "availability_policy": source.get("availability_policy"),
            }
        )
        prepared.append(
            {
                "company": copy.deepcopy(company),
                "original_renewable_pct": original_renewable,
                "scenario_renewable_pct": scenario_renewable,
                "original_score": original_score,
            }
        )

    predictions = service.predict_rows_only(scenario_rows, target)
    if len(predictions) != len(prepared):
        raise ValueError(
            "The renewable scenario returned an incomplete prediction set."
        )
    for company, prediction in zip(prepared, predictions, strict=True):
        scenario_score = _finite(prediction.get(model_family), "Scenario model score")
        company["scenario_score"] = scenario_score
        company["score_change"] = scenario_score - company["original_score"]

    original_ranks = _rank(prepared, "original_score")
    scenario_ranks = _rank(prepared, "scenario_score")
    for company in prepared:
        company_cik = company["company"]["company_cik"]
        company["original_rank"] = original_ranks[company_cik]
        company["scenario_rank"] = scenario_ranks[company_cik]
        company["rank_change"] = (
            original_ranks[company_cik] - scenario_ranks[company_cik]
        )
    prepared.sort(key=lambda company: company["scenario_rank"])

    sector_values: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {
            "company_count": 0,
            "original_score_sum": 0.0,
            "scenario_score_sum": 0.0,
        }
    )
    for company in prepared:
        sector = company["company"].get("industry") or "Unknown"
        sector_values[sector]["company_count"] += 1
        sector_values[sector]["original_score_sum"] += company["original_score"]
        sector_values[sector]["scenario_score_sum"] += company["scenario_score"]
    original_total = sum(company["original_score"] for company in prepared)
    scenario_total = sum(company["scenario_score"] for company in prepared)
    if original_total <= 0 or scenario_total <= 0:
        raise ValueError("Sector score shares require positive total model scores.")
    sectors = []
    for sector, values in sector_values.items():
        original_share = values["original_score_sum"] / original_total * 100
        scenario_share = values["scenario_score_sum"] / scenario_total * 100
        share_change = scenario_share - original_share
        sectors.append(
            {
                "sector": sector,
                "company_count": values["company_count"],
                "original_score_sum": values["original_score_sum"],
                "scenario_score_sum": values["scenario_score_sum"],
                "original_share_pct": original_share,
                "scenario_share_pct": scenario_share,
                "share_change_percentage_points": share_change,
                "direction": "up"
                if share_change > 1e-12
                else "down"
                if share_change < -1e-12
                else "flat",
            }
        )
    sectors.sort(key=lambda item: (-item["scenario_share_pct"], item["sector"]))
    amounts = _fund_amounts([sector["scenario_share_pct"] / 100 for sector in sectors])
    for sector, amount in zip(sectors, amounts, strict=True):
        sector["fund_amount_usd"] = amount

    observed = sum(
        company["original_renewable_pct"] is not None for company in prepared
    )
    changed = sum(
        company["scenario_renewable_pct"] != company["original_renewable_pct"]
        for company in prepared
    )
    saturated = sum(company["scenario_renewable_pct"] == 100 for company in prepared)
    increaseable = sum(
        company["scenario_renewable_pct"] is not None
        and 0 < company["scenario_renewable_pct"] < 100
        for company in prepared
    )
    return {
        "target": target,
        "model_family": model_family,
        "renewable_multiplier": multiplier,
        "step_summary": {
            "company_count": len(prepared),
            "observed_count": observed,
            "missing_count": len(prepared) - observed,
            "changed_count": changed,
            "saturated_count": saturated,
            "increaseable_count": increaseable,
        },
        "companies": prepared,
        "sectors": sectors,
        "total_score_sum": {"original": original_total, "scenario": scenario_total},
        "disclosure": "CSA EBM score changes are model sensitivities. Sector amounts allocate $1 billion in proportion to sector scenario-score sums.",
    }
