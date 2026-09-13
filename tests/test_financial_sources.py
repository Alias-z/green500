"""Checks for strict current-period SEC Company Facts selection."""

import json
from datetime import date

import pytest

from green500.financial_sources import parse_companyfacts

CURRENT_DATE = date(2026, 9, 12)


def fact(
    value,
    *,
    end,
    filed,
    accession,
    form,
    start=None,
    fiscal_period=None,
):
    """Create one Company Facts unit entry with explicit filing provenance."""
    result = {
        "end": end,
        "val": value,
        "accn": accession,
        "form": form,
        "filed": filed,
    }
    if start is not None:
        result["start"] = start
    if fiscal_period is not None:
        result["fp"] = fiscal_period
    return result


def response_body(*, cik="789019", entity_name="Example Corporation", concepts=None):
    """Encode a small response with the same root shape as the SEC API."""
    facts = {}
    for concept, unit_facts in (concepts or {}).items():
        facts[concept] = {"label": concept, "units": {"USD": unit_facts}}
    return json.dumps(
        {"cik": int(cik), "entityName": entity_name, "facts": {"us-gaap": facts}}
    ).encode()


def observation(parsed, metric_code, period_type):
    """Return one selected metric and period from parser output."""
    return next(
        item
        for item in parsed["observations"]
        if item["metric_code"] == metric_code and item["period_type"] == period_type
    )


def complete_concepts():
    """Create annual, later quarterly and instant facts for all core metrics."""
    return {
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            fact(
                1_000,
                start="2025-01-01",
                end="2025-12-31",
                filed="2026-02-01",
                accession="0000000001-26-000001",
                form="10-K",
                fiscal_period="FY",
            ),
            fact(
                300,
                start="2026-04-01",
                end="2026-06-30",
                filed="2026-08-01",
                accession="0000000001-26-000002",
                form="10-Q",
                fiscal_period="Q2",
            ),
            fact(
                550,
                start="2026-01-01",
                end="2026-06-30",
                filed="2026-08-01",
                accession="0000000001-26-000002",
                form="10-Q",
                fiscal_period="Q2",
            ),
        ],
        "NetIncomeLoss": [
            fact(
                200,
                start="2025-01-01",
                end="2025-12-31",
                filed="2026-02-01",
                accession="0000000001-26-000001",
                form="10-K",
                fiscal_period="FY",
            ),
            fact(
                65,
                start="2026-04-01",
                end="2026-06-30",
                filed="2026-08-01",
                accession="0000000001-26-000002",
                form="10-Q",
                fiscal_period="Q2",
            ),
        ],
        "Assets": [
            fact(
                5_000,
                end="2025-12-31",
                filed="2026-02-01",
                accession="0000000001-26-000001",
                form="10-K",
                fiscal_period="FY",
            ),
            fact(
                5_200,
                end="2026-06-30",
                filed="2026-08-01",
                accession="0000000001-26-000002",
                form="10-Q",
                fiscal_period="Q2",
            ),
        ],
    }


def test_annual_quarterly_and_instant_periods_remain_separate():
    """A newer quarter cannot replace the selected complete annual duration."""
    parsed = parse_companyfacts(
        response_body(concepts=complete_concepts()), "0000789019", CURRENT_DATE
    )

    annual_revenue = observation(parsed, "revenue", "annual")
    quarterly_revenue = observation(parsed, "revenue", "quarterly")
    annual_net_income = observation(parsed, "net_income", "annual")
    current_assets = observation(parsed, "assets", "instant")

    assert annual_revenue["value"] == "1000"
    assert annual_revenue["period_start"] == "2025-01-01"
    assert annual_revenue["period_end"] == "2025-12-31"
    assert quarterly_revenue["value"] == "300"
    assert quarterly_revenue["period_start"] == "2026-04-01"
    assert annual_net_income["value"] == "200"
    assert current_assets["value"] == "5200"
    assert "period_start" not in current_assets
    assert parsed["missing_observations"] == []


def test_revenue_uses_latest_period_before_concept_priority():
    """An obsolete bank tag cannot displace a newer customer-contract period."""
    concepts = complete_concepts()
    concepts["RevenuesNetOfInterestExpense"] = [
        fact(
            700,
            start="2024-01-01",
            end="2024-12-31",
            filed="2025-02-01",
            accession="0000000001-25-000001",
            form="10-K",
            fiscal_period="FY",
        )
    ]
    parsed = parse_companyfacts(
        response_body(concepts=concepts), "0000789019", CURRENT_DATE
    )

    selected = observation(parsed, "revenue", "annual")
    assert selected["value"] == "1000"
    assert (
        selected["source_record"]["concept"]
        == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    )


def test_bank_net_revenue_wins_for_the_same_current_period():
    """JPM-style net-of-interest revenue wins over a generic same-period tag."""
    annual_details = {
        "start": "2025-01-01",
        "end": "2025-12-31",
        "filed": "2026-02-15",
        "accession": "0000019617-26-000001",
        "form": "10-K",
        "fiscal_period": "FY",
    }
    concepts = {
        "RevenuesNetOfInterestExpense": [fact(900, **annual_details)],
        "Revenues": [fact(999, **annual_details)],
    }
    parsed = parse_companyfacts(
        response_body(
            cik="19617", entity_name="JPMORGAN CHASE & CO", concepts=concepts
        ),
        "0000019617",
        CURRENT_DATE,
    )

    selected = observation(parsed, "revenue", "annual")
    assert selected["value"] == "900"
    assert (
        selected["source_record"]["concept"] == "us-gaap:RevenuesNetOfInterestExpense"
    )


def test_latest_amended_fact_keeps_same_period_publications():
    """A changed 10-K/A value remains linked to the earlier exact-period fact."""
    concepts = {
        "NetIncomeLoss": [
            fact(
                100,
                start="2025-01-01",
                end="2025-12-31",
                filed="2026-02-01",
                accession="0000000001-26-000001",
                form="10-K",
                fiscal_period="FY",
            ),
            fact(
                95,
                start="2025-01-01",
                end="2025-12-31",
                filed="2026-03-01",
                accession="0000000001-26-000003",
                form="10-K/A",
                fiscal_period="FY",
            ),
        ]
    }
    parsed = parse_companyfacts(
        response_body(concepts=concepts), "0000789019", CURRENT_DATE
    )

    selected = observation(parsed, "net_income", "annual")
    record = selected["source_record"]
    assert selected["value"] == "95"
    assert selected["published_at"] == "2026-03-01"
    assert record["is_amendment"] is True
    assert record["has_differing_same_period_value"] is True
    assert [item["val"] for item in record["same_period_facts"]] == [100, 95]


@pytest.mark.parametrize(
    ("body", "expected_cik", "message"),
    [
        (b'{"cik": 789019', "0000789019", "malformed or truncated"),
        (
            response_body(cik="320193"),
            "0000789019",
            "returned CIK 0000320193",
        ),
        (
            response_body(
                concepts={
                    "Assets": [
                        fact(
                            1,
                            end="2027-01-01",
                            filed="2026-09-01",
                            accession="0000000001-26-000001",
                            form="10-Q",
                            fiscal_period="Q3",
                        )
                    ]
                }
            ),
            "0000789019",
            "future period",
        ),
    ],
)
def test_invalid_or_wrong_company_responses_fail_closed(body, expected_cik, message):
    """Truncation, identity mismatch and future facts never publish observations."""
    with pytest.raises(ValueError, match=message):
        parse_companyfacts(body, expected_cik, CURRENT_DATE)
