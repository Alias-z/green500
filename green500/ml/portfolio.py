"""Build a deterministic equal-allocation portfolio from personalized rankings.

The allocator first removes companies below the requested active-feature coverage,
then ranks the remaining companies by descending signed personalized index and CIK.
For every permitted shortlist size, it finds the largest whole-cent amount that can
be assigned equally without exceeding either cap.  It scans the complete ranking
when constructing that shortlist, skipping a company whose industry has reached
capacity instead of stopping at that industry.  The chosen shortlist maximizes
allocated dollars; ties prefer more companies and then the higher-ranked set.

At most one residual cent is added to each selected company in rank order.  This
handles indivisible cents while keeping allocations equal to within one cent.
Cash that cannot be allocated without relaxing a constraint remains unallocated.
"""

from __future__ import annotations

import math
from collections import Counter
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

CONSTRAINT_NAMES = {
    "fund_usd",
    "max_companies",
    "max_company_weight",
    "max_industry_weight",
    "min_coverage",
}
MAX_EVALUATION_COUNT = 500
MAX_FUND_USD = Decimal(1_000_000_000_000)


def _finite_number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be a finite number.")
    return float(value)


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    return value.strip()


def _fund_cents(value: object) -> int:
    number = _finite_number(value, "fund_usd")
    if number <= 0:
        raise ValueError("fund_usd must be greater than zero.")
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("fund_usd must be a finite number.") from error
    if decimal_value > MAX_FUND_USD:
        raise ValueError("fund_usd must be at most 1000000000000.")
    cents = decimal_value * 100
    if cents != cents.to_integral_value():
        raise ValueError("fund_usd must use whole-cent precision.")
    return int(cents)


def _normalize_constraints(constraints: dict) -> tuple[dict, int]:
    if not isinstance(constraints, dict):
        raise TypeError("constraints must be an object.")
    missing = sorted(CONSTRAINT_NAMES - set(constraints))
    unknown = sorted(set(constraints) - CONSTRAINT_NAMES)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ValueError(
            "constraints must contain exactly the supported fields: "
            + "; ".join(details)
        )

    fund_cents = _fund_cents(constraints["fund_usd"])
    maximum_count = constraints["max_companies"]
    if (
        isinstance(maximum_count, bool)
        or not isinstance(maximum_count, int)
        or not 1 <= maximum_count <= MAX_EVALUATION_COUNT
    ):
        raise ValueError(
            f"max_companies must be an integer from 1 to {MAX_EVALUATION_COUNT}."
        )

    maximum_company_weight = _finite_number(
        constraints["max_company_weight"], "max_company_weight"
    )
    maximum_industry_weight = _finite_number(
        constraints["max_industry_weight"], "max_industry_weight"
    )
    minimum_coverage = _finite_number(constraints["min_coverage"], "min_coverage")
    if not 0 < maximum_company_weight <= 1:
        raise ValueError(
            "max_company_weight must be greater than zero and at most one."
        )
    if not 0 < maximum_industry_weight <= 1:
        raise ValueError(
            "max_industry_weight must be greater than zero and at most one."
        )
    if not 0 <= minimum_coverage <= 1:
        raise ValueError("min_coverage must be from zero to one.")

    normalized = {
        "fund_usd": fund_cents / 100,
        "max_companies": maximum_count,
        "max_company_weight": maximum_company_weight,
        "max_industry_weight": maximum_industry_weight,
        "min_coverage": minimum_coverage,
    }
    return normalized, fund_cents


def _coverage_ratio(coverage: object, position: int) -> float:
    if not isinstance(coverage, dict):
        raise TypeError(f"companies[{position}].coverage must be an object.")
    active = coverage.get("active_feature_count")
    available = coverage.get("available_active_feature_count")
    for value, field in (
        (active, "active_feature_count"),
        (available, "available_active_feature_count"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"companies[{position}].coverage.{field} must be a nonnegative integer."
            )
    if available > active:
        raise ValueError(
            f"companies[{position}] available active features cannot exceed active features."
        )
    return available / active if active else 0.0


def _normalize_evaluation(evaluation: object, position: int) -> dict:
    if not isinstance(evaluation, dict):
        raise TypeError(f"companies[{position}] must be an object.")
    company = evaluation.get("company")
    if not isinstance(company, dict):
        raise TypeError(f"companies[{position}].company must be an object.")
    normalized_company = {
        field: _nonempty_text(
            company.get(field), f"companies[{position}].company.{field}"
        )
        for field in ("company_cik", "name", "ticker", "industry")
    }

    predictions = evaluation.get("predictions")
    if not isinstance(predictions, dict):
        raise TypeError(f"companies[{position}].predictions must be an object.")
    for model_name in ("ebm", "catboost"):
        _finite_number(
            predictions.get(model_name),
            f"companies[{position}].predictions.{model_name}",
        )

    personalized_index = _finite_number(
        evaluation.get("personalized_index"),
        f"companies[{position}].personalized_index",
    )
    contributions = evaluation.get("category_contributions")
    if not isinstance(contributions, dict):
        raise TypeError(
            f"companies[{position}].category_contributions must be an object."
        )
    normalized_contributions = {}
    for category, value in contributions.items():
        normalized_category = _nonempty_text(
            category, f"companies[{position}] contribution category"
        )
        normalized_contributions[normalized_category] = _finite_number(
            value,
            f"companies[{position}].category_contributions.{normalized_category}",
        )

    warnings = evaluation.get("warnings", [])
    if not isinstance(warnings, list) or any(
        not isinstance(warning, str) or not warning.strip() for warning in warnings
    ):
        raise ValueError(
            f"companies[{position}].warnings must contain non-empty strings."
        )
    return {
        "company": normalized_company,
        "personalized_index": personalized_index,
        "category_contributions": normalized_contributions,
        "coverage_ratio": _coverage_ratio(evaluation.get("coverage"), position),
        "warnings": [warning.strip() for warning in warnings],
    }


def _cap_cents(fund_cents: int, weight: float) -> int:
    return int(
        (Decimal(fund_cents) * Decimal(str(weight))).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )


def _can_select_count(
    industry_counts: Counter[str], shortlist_size: int, industry_capacity: int
) -> bool:
    return (
        sum(
            min(company_count, industry_capacity)
            for company_count in industry_counts.values()
        )
        >= shortlist_size
    )


def _largest_equal_amount(
    *,
    shortlist_size: int,
    fund_cents: int,
    company_cap_cents: int,
    industry_cap_cents: int,
    industry_counts: Counter[str],
) -> int:
    low = 1
    high = min(fund_cents // shortlist_size, company_cap_cents, industry_cap_cents)
    best = 0
    while low <= high:
        candidate = (low + high) // 2
        industry_capacity = industry_cap_cents // candidate
        if _can_select_count(industry_counts, shortlist_size, industry_capacity):
            best = candidate
            low = candidate + 1
        else:
            high = candidate - 1
    return best


def _ranked_shortlist(
    ranked: list[dict], size: int, industry_capacity: int
) -> list[dict]:
    selected = []
    selected_by_industry: Counter[str] = Counter()
    for evaluation in ranked:
        industry = evaluation["company"]["industry"]
        if selected_by_industry[industry] >= industry_capacity:
            continue
        selected.append(evaluation)
        selected_by_industry[industry] += 1
        if len(selected) == size:
            break
    return selected


def _amounts_with_residual_cents(
    selected: list[dict],
    equal_amount_cents: int,
    fund_cents: int,
    company_cap_cents: int,
    industry_cap_cents: int,
) -> list[int]:
    amounts = [equal_amount_cents] * len(selected)
    remaining = fund_cents - equal_amount_cents * len(selected)
    industry_totals = Counter(
        {
            industry: count * equal_amount_cents
            for industry, count in Counter(
                item["company"]["industry"] for item in selected
            ).items()
        }
    )
    for index, evaluation in enumerate(selected):
        if not remaining:
            break
        industry = evaluation["company"]["industry"]
        if (
            amounts[index] < company_cap_cents
            and industry_totals[industry] < industry_cap_cents
        ):
            amounts[index] += 1
            industry_totals[industry] += 1
            remaining -= 1
    return amounts


def _category_reason(evaluation: dict) -> str:
    rank = evaluation["rank"]
    index = evaluation["personalized_index"]
    contributions = evaluation["category_contributions"]
    if not contributions:
        return (
            f"Rank {rank} by signed personalized index {index:.6g}; "
            "no category contributions were supplied."
        )
    ordered = sorted(contributions.items(), key=lambda item: (-abs(item[1]), item[0]))
    categories = ", ".join(
        f"{category} {value:+.6g}" for category, value in ordered[:3]
    )
    return (
        f"Rank {rank} by signed personalized index {index:.6g}; "
        f"largest absolute category contributions: {categories}."
    )


def allocate_portfolio(companies: list[dict], constraints: dict) -> dict:
    """Allocate whole cents without using model scores as return estimates."""
    normalized_constraints, fund_cents = _normalize_constraints(constraints)
    if not isinstance(companies, list):
        raise TypeError("companies must be a list.")
    if len(companies) > MAX_EVALUATION_COUNT:
        raise ValueError(
            f"companies must contain at most {MAX_EVALUATION_COUNT} evaluations."
        )

    normalized = [
        _normalize_evaluation(evaluation, position)
        for position, evaluation in enumerate(companies)
    ]
    company_ids = [item["company"]["company_cik"] for item in normalized]
    duplicates = sorted(
        company_id for company_id, count in Counter(company_ids).items() if count > 1
    )
    if duplicates:
        raise ValueError("Duplicate company_cik values: " + ", ".join(duplicates))

    eligible = [
        item
        for item in normalized
        if item["coverage_ratio"] >= normalized_constraints["min_coverage"]
        and item["coverage_ratio"] > 0
    ]
    eligible.sort(
        key=lambda item: (
            -item["personalized_index"],
            item["company"]["company_cik"],
        )
    )
    for rank, evaluation in enumerate(eligible, 1):
        evaluation["rank"] = rank

    company_cap_cents = _cap_cents(
        fund_cents, normalized_constraints["max_company_weight"]
    )
    industry_cap_cents = _cap_cents(
        fund_cents, normalized_constraints["max_industry_weight"]
    )
    industry_counts = Counter(item["company"]["industry"] for item in eligible)
    maximum_size = min(normalized_constraints["max_companies"], len(eligible))
    best_selected: list[dict] = []
    best_amounts: list[int] = []
    best_key: tuple = (-1, -1, ())

    for size in range(1, maximum_size + 1):
        equal_amount = _largest_equal_amount(
            shortlist_size=size,
            fund_cents=fund_cents,
            company_cap_cents=company_cap_cents,
            industry_cap_cents=industry_cap_cents,
            industry_counts=industry_counts,
        )
        if not equal_amount:
            continue
        capacity = industry_cap_cents // equal_amount
        selected = _ranked_shortlist(eligible, size, capacity)
        if len(selected) != size:
            continue
        amounts = _amounts_with_residual_cents(
            selected,
            equal_amount,
            fund_cents,
            company_cap_cents,
            industry_cap_cents,
        )
        rank_key = tuple(-item["rank"] for item in selected)
        candidate_key = (sum(amounts), size, rank_key)
        if candidate_key > best_key:
            best_key = candidate_key
            best_selected = selected
            best_amounts = amounts

    allocations = []
    industry_amounts: Counter[str] = Counter()
    for evaluation, amount_cents in zip(best_selected, best_amounts, strict=True):
        company = evaluation["company"]
        allocations.append(
            {
                **company,
                "weight": round(amount_cents / fund_cents, 12),
                "amount_usd": amount_cents / 100,
                "rank": evaluation["rank"],
                "reason": _category_reason(evaluation),
            }
        )
        industry_amounts[company["industry"]] += amount_cents

    allocated_cents = sum(best_amounts)
    unallocated_cents = fund_cents - allocated_cents
    excluded_count = len(normalized) - len(eligible)
    warnings = []
    if excluded_count:
        warnings.append(
            f"Excluded {excluded_count} companies without positive active-feature coverage "
            f"at or above {normalized_constraints['min_coverage']:.1%}."
        )
    selected_ids = {item["company"]["company_cik"] for item in best_selected}
    for evaluation in eligible:
        if evaluation["company"]["company_cik"] not in selected_ids:
            continue
        for warning in evaluation["warnings"]:
            warnings.append(
                f"{evaluation['company']['ticker']} "
                f"({evaluation['company']['company_cik']}): {warning}"
            )
    if unallocated_cents:
        warnings.append(
            f"${unallocated_cents / 100:,.2f} remains unallocated because the "
            "equal-allocation, "
            "company-count, company-weight, or industry-weight limit binds."
        )

    return {
        "allocations": allocations,
        "allocated_usd": allocated_cents / 100,
        "unallocated_usd": unallocated_cents / 100,
        "warnings": list(dict.fromkeys(warnings)),
        "industry_weights": {
            industry: round(amount_cents / fund_cents, 12)
            for industry, amount_cents in sorted(industry_amounts.items())
        },
        "constraints": normalized_constraints,
        "eligible_count": len(eligible),
        "excluded_count": excluded_count,
        "allocation_method": "ranked_constrained_equal_allocation",
    }


RESILIENCE_COMPONENT_WEIGHTS = {
    "operating_margin": 0.5,
    "operating_cash_flow_margin": 0.3,
    "inverse_debt_assets": 0.2,
}
FINANCIAL_RESILIENCE_METHOD = (
    "Cohort percentile ranks score operating margin (50%, higher is better), "
    "operating cash flow divided by positive revenue (30%, higher is better), "
    "and debt-to-assets (20%, lower is better). Average tie ranks run from zero "
    "for worst to one for best; a lone observed value scores one. Companies need "
    "two observed components; available weights are normalized without imputation. "
    "Only the requested top fraction of covered companies by signed personalized "
    "index is eligible. Whole cents are greedily allocated by resilience score and "
    "CIK up to the company, industry, company-count, and fund limits."
)


def _observed_number(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def _resilience_component_values(
    evaluations: list[dict],
) -> dict[str, dict[str, float]]:
    result = {}
    for evaluation in evaluations:
        features = evaluation["features"]
        values = {}
        operating_margin = _observed_number(features.get("operating_margin"))
        if operating_margin is not None:
            values["operating_margin"] = operating_margin

        cash_flow = _observed_number(features.get("financial_operating_cash_flow_usd"))
        revenue = _observed_number(features.get("financial_revenue_usd"))
        if cash_flow is not None and revenue is not None and revenue > 0:
            cash_flow_margin = cash_flow / revenue
            if math.isfinite(cash_flow_margin):
                values["operating_cash_flow_margin"] = cash_flow_margin

        debt_assets = _observed_number(features.get("debt_assets"))
        if debt_assets is not None:
            values["inverse_debt_assets"] = debt_assets
        result[evaluation["company"]["company_cik"]] = values
    return result


def _percentile_ranks(
    values_by_company: dict[str, float], *, higher_is_better: bool
) -> dict[str, float]:
    if not values_by_company:
        return {}
    if len(values_by_company) == 1:
        company_id = next(iter(values_by_company))
        return {company_id: 1.0}
    ordered_values = sorted(values_by_company.values())
    first_positions = {}
    last_positions = {}
    for position, value in enumerate(ordered_values):
        first_positions.setdefault(value, position)
        last_positions[value] = position
    denominator = len(ordered_values) - 1
    result = {}
    for company_id, value in values_by_company.items():
        ascending = (first_positions[value] + last_positions[value]) / 2 / denominator
        result[company_id] = ascending if higher_is_better else 1 - ascending
    return result


def _financial_resilience_scores(evaluations: list[dict]) -> dict[str, dict]:
    raw_components = _resilience_component_values(evaluations)
    component_percentiles = {}
    for component in RESILIENCE_COMPONENT_WEIGHTS:
        observed = {
            company_id: values[component]
            for company_id, values in raw_components.items()
            if component in values
        }
        component_percentiles[component] = _percentile_ranks(
            observed,
            higher_is_better=component != "inverse_debt_assets",
        )

    scores = {}
    for company_id, raw_values in raw_components.items():
        if len(raw_values) < 2:
            continue
        components = {
            component: component_percentiles[component][company_id]
            for component in raw_values
        }
        available_weight = sum(
            RESILIENCE_COMPONENT_WEIGHTS[component] for component in components
        )
        objective_score = (
            sum(
                RESILIENCE_COMPONENT_WEIGHTS[component] * score
                for component, score in components.items()
            )
            / available_weight
        )
        scores[company_id] = {
            "objective_score": objective_score,
            "component_scores": components,
        }
    return scores


def _normalize_sustainability_fraction(value: object) -> float:
    fraction = _finite_number(value, "sustainability_eligible_fraction")
    if not 0 < fraction <= 1:
        raise ValueError(
            "sustainability_eligible_fraction must be greater than zero and at most one."
        )
    return fraction


def optimize_financial_resilience_portfolio(
    companies: list[dict],
    constraints: dict,
    *,
    sustainability_eligible_fraction: float = 0.75,
) -> dict:
    """Maximize an observed financial-resilience proxy under explicit caps."""
    normalized_constraints, fund_cents = _normalize_constraints(constraints)
    fraction = _normalize_sustainability_fraction(sustainability_eligible_fraction)
    if not isinstance(companies, list):
        raise TypeError("companies must be a list.")
    if len(companies) > MAX_EVALUATION_COUNT:
        raise ValueError(
            f"companies must contain at most {MAX_EVALUATION_COUNT} evaluations."
        )

    normalized = []
    for position, source in enumerate(companies):
        evaluation = _normalize_evaluation(source, position)
        features = source.get("features")
        if not isinstance(features, dict):
            raise TypeError(f"companies[{position}].features must be an object.")
        evaluation["features"] = features
        normalized.append(evaluation)
    company_ids = [item["company"]["company_cik"] for item in normalized]
    duplicates = sorted(
        company_id for company_id, count in Counter(company_ids).items() if count > 1
    )
    if duplicates:
        raise ValueError("Duplicate company_cik values: " + ", ".join(duplicates))

    covered = [
        item
        for item in normalized
        if item["coverage_ratio"] >= normalized_constraints["min_coverage"]
        and item["coverage_ratio"] > 0
    ]
    covered.sort(
        key=lambda item: (
            -item["personalized_index"],
            item["company"]["company_cik"],
        )
    )
    for rank, evaluation in enumerate(covered, 1):
        evaluation["sustainability_rank"] = rank
    sustainable_count = (
        int(
            (Decimal(len(covered)) * Decimal(str(fraction))).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if covered
        else 0
    )
    sustainable = covered[:sustainable_count]
    sustainability_cutoff = (
        sustainable[-1]["personalized_index"] if sustainable else None
    )

    scores = _financial_resilience_scores(covered)
    eligible = [
        evaluation
        for evaluation in sustainable
        if evaluation["company"]["company_cik"] in scores
    ]
    for evaluation in eligible:
        evaluation.update(scores[evaluation["company"]["company_cik"]])
    eligible.sort(
        key=lambda item: (
            -item["objective_score"],
            item["company"]["company_cik"],
        )
    )
    for rank, evaluation in enumerate(eligible, 1):
        evaluation["objective_rank"] = rank

    company_cap_cents = _cap_cents(
        fund_cents, normalized_constraints["max_company_weight"]
    )
    industry_cap_cents = _cap_cents(
        fund_cents, normalized_constraints["max_industry_weight"]
    )
    remaining_cents = fund_cents
    industry_amounts: Counter[str] = Counter()
    selected: list[tuple[dict, int]] = []
    for evaluation in eligible:
        if (
            not remaining_cents
            or len(selected) >= normalized_constraints["max_companies"]
        ):
            break
        industry = evaluation["company"]["industry"]
        amount_cents = min(
            remaining_cents,
            company_cap_cents,
            industry_cap_cents - industry_amounts[industry],
        )
        if amount_cents <= 0:
            continue
        selected.append((evaluation, amount_cents))
        remaining_cents -= amount_cents
        industry_amounts[industry] += amount_cents

    allocations = []
    for evaluation, amount_cents in selected:
        allocations.append(
            {
                **evaluation["company"],
                "weight": amount_cents / fund_cents,
                "amount_usd": amount_cents / 100,
                "rank": evaluation["objective_rank"],
                "reason": (
                    f"Financial resilience #{evaluation['objective_rank']}; "
                    f"sustainability-eligible #{evaluation['sustainability_rank']}."
                ),
                "objective_score": evaluation["objective_score"],
                "component_scores": evaluation["component_scores"],
            }
        )

    allocated_cents = fund_cents - remaining_cents
    coverage_excluded_count = len(normalized) - len(covered)
    financial_excluded_count = sum(
        evaluation["company"]["company_cik"] not in scores for evaluation in sustainable
    )
    warnings = []
    if coverage_excluded_count:
        warnings.append(
            f"Excluded {coverage_excluded_count} companies without positive active-feature "
            f"coverage at or above {normalized_constraints['min_coverage']:.1%}."
        )
    outside_sustainability_count = len(covered) - len(sustainable)
    if outside_sustainability_count:
        warnings.append(
            f"Excluded {outside_sustainability_count} covered companies outside the top "
            f"{fraction:.1%} by signed personalized index."
        )
    if financial_excluded_count:
        warnings.append(
            f"Excluded {financial_excluded_count} sustainability-eligible companies with "
            "fewer than two observed financial-resilience components."
        )
    selected_ids = {evaluation["company"]["company_cik"] for evaluation, _ in selected}
    for evaluation in eligible:
        if evaluation["company"]["company_cik"] not in selected_ids:
            continue
        for warning in evaluation["warnings"]:
            warnings.append(
                f"{evaluation['company']['ticker']} "
                f"({evaluation['company']['company_cik']}): {warning}"
            )
    if remaining_cents:
        warnings.append(
            f"${remaining_cents / 100:,.2f} remains unallocated because the company-count, "
            "company-weight, industry-weight, sustainability, or observed-component limit binds."
        )

    portfolio_objective_score = sum(
        (amount_cents / fund_cents) * evaluation["objective_score"]
        for evaluation, amount_cents in selected
    )
    return {
        "allocations": allocations,
        "allocated_usd": allocated_cents / 100,
        "unallocated_usd": remaining_cents / 100,
        "warnings": list(dict.fromkeys(warnings)),
        "industry_weights": {
            industry: amount_cents / fund_cents
            for industry, amount_cents in sorted(industry_amounts.items())
        },
        "constraints": normalized_constraints,
        "eligible_count": len(eligible),
        "excluded_count": len(normalized) - len(eligible),
        "allocation_method": "greedy_financial_resilience_proxy",
        "objective_name": "Financial resilience proxy",
        "objective_method": FINANCIAL_RESILIENCE_METHOD,
        "sustainability_eligible_fraction": fraction,
        "sustainability_cutoff": sustainability_cutoff,
        "portfolio_objective_score": portfolio_objective_score,
    }
