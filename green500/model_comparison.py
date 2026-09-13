"""Serve published company comparisons and publish new report snapshots explicitly."""

from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from green500 import model_comparison_store as store
from green500 import view_access
from green500.config import load_settings

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/model-comparison", dependencies=[Depends(view_access.authorize_view)]
)
_service_lock = threading.Lock()
_service_instance = None
_service_directory = None
_service_manifest_hash = None
_requests = threading.BoundedSemaphore(2)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NumericFilter(StrictRequest):
    feature_name: str = Field(max_length=100)
    minimum: float | None = None
    maximum: float | None = None


class CompanyQuery(StrictRequest):
    target: Literal["esg", "csa"] = "esg"
    search: str = Field(default="", max_length=200)
    industry: str | None = Field(default=None, max_length=150)
    filters: list[NumericFilter] = Field(default_factory=list, max_length=30)
    sort_by: str = Field(default="company_name", max_length=100)
    direction: Literal["asc", "desc"] = "asc"
    limit: int = Field(default=500, ge=1, le=500)


class Adjustment(StrictRequest):
    feature_name: str = Field(max_length=100)
    operation: Literal["add", "percent", "set"]
    value: float


class ComparisonRequest(StrictRequest):
    target: Literal["esg", "csa"] = "esg"
    company_ids: list[Annotated[str, Field(pattern=r"^[0-9]{10}$")]] = Field(
        min_length=1, max_length=500
    )
    adjustments: list[Adjustment] = Field(default_factory=list, max_length=54)
    weights: dict[str, float] = Field(default_factory=dict)
    preview_only: bool = False
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class PortfolioConstraints(StrictRequest):
    fund_usd: float = Field(default=1_000_000_000, gt=0, le=1_000_000_000_000)
    max_companies: int = Field(default=10, ge=1, le=500)
    max_company_weight: float = Field(default=0.2, gt=0, le=1)
    max_industry_weight: float = Field(default=0.4, gt=0, le=1)
    min_coverage: float = Field(default=0.3, ge=0, le=1)


class PortfolioRequest(ComparisonRequest):
    constraints: PortfolioConstraints = Field(default_factory=PortfolioConstraints)
    objective: Literal["personalized_index", "financial_resilience"] = (
        "personalized_index"
    )
    sustainability_eligible_fraction: float = Field(default=0.75, gt=0, le=1)


class TargetSuggestionRequest(StrictRequest):
    target: Literal["esg", "csa"] = "esg"
    company_id: Annotated[str, Field(pattern=r"^[0-9]{10}$")]
    desired_score: float = Field(ge=0, le=100)
    model_family: Literal["ebm", "catboost"] = "ebm"
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    max_suggestions: int = Field(default=5, ge=1, le=10)


class PublishedRankingRequest(StrictRequest):
    target: Literal["esg", "csa"] = "esg"
    weights: dict[str, float] = Field(default_factory=dict)
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RenewableScenarioRequest(StrictRequest):
    target: Literal["esg", "csa"] = "csa"
    model_family: Literal["ebm", "catboost"] = "ebm"
    renewable_multiplier: float = Field(default=1, ge=1)
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _snapshot():
    try:
        return store.current_snapshot(load_settings())
    except (FileNotFoundError, psycopg.Error) as error:
        raise HTTPException(
            503,
            "Company comparisons are unavailable until a verified model snapshot is published.",
        ) from error


def _service(snapshot):
    global _service_instance, _service_directory, _service_manifest_hash
    with _service_lock:
        expected_hash = snapshot["summary"].get("run_manifest_sha256")
        if (
            _service_instance is None
            or _service_directory != snapshot["run_directory"]
            or _service_manifest_hash != expected_hash
        ):
            from green500.ml.artifacts import sha256_file
            from green500.ml.serving import SavedModelService

            try:
                expected_hash = snapshot["summary"].get("run_manifest_sha256")
                if (
                    not expected_hash
                    or sha256_file(
                        Path(snapshot["run_directory"]) / "run_manifest.json"
                    )
                    != expected_hash
                ):
                    raise ValueError(
                        "Published model manifest hash does not match the installed model."
                    )
                candidate = SavedModelService(snapshot["run_directory"])
                if candidate.describe()["run_id"] != snapshot["run_id"]:
                    raise ValueError(
                        "Published run identity differs from the installed model."
                    )
            except (OSError, ValueError, TypeError, RuntimeError, ImportError) as error:
                logger.exception("Cannot load the published company comparison models.")
                raise HTTPException(
                    503,
                    "The published models failed to load or verify. Source reports remain available.",
                ) from error
            _service_instance = candidate
            _service_directory = snapshot["run_directory"]
            _service_manifest_hash = expected_hash
    return _service_instance


@router.get("/run")
def model_run():
    return _snapshot()["summary"]


@router.get("/demos")
def demo_presets():
    """Return four reviewed demonstrations bound to the published company snapshot."""
    snapshot = _snapshot()
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "pair_presets": [
            {
                "id": "opposite_emissions",
                "label": "Scope 1 contrast · PG&E vs Capital One",
                "description": "Apply the same 50% Scope 1 emissions increase to both companies. PG&E's saved EBM and CatBoost ESG predictions rise, while Capital One's fall. This is a model-sensitivity contrast, not a causal claim.",
                "target": "esg",
                "company_ids": ["0001004980", "0000927628"],
                "default_feature": "env_scope_1_tco2e",
                "actions": {
                    "decrease": {
                        "label": "−50% per click",
                        "operation": "percent",
                        "value": -50,
                    },
                    "increase": {
                        "label": "+50% per click",
                        "operation": "percent",
                        "value": 50,
                    },
                },
            },
            {
                "id": "clean_power",
                "label": "Clean power · Abbott vs 3M",
                "description": "Both companies disclose renewable electricity; the endpoints create visible responses from both saved models.",
                "target": "esg",
                "company_ids": ["0000001800", "0000066740"],
                "default_feature": "env_renewable_electricity_percent",
                "actions": {
                    "decrease": {
                        "label": "−25% per click",
                        "operation": "add",
                        "value": -25,
                    },
                    "increase": {
                        "label": "+25% per click",
                        "operation": "add",
                        "value": 25,
                    },
                },
            },
            {
                "id": "revenue_scale",
                "label": "Revenue scale · Apple vs Walmart",
                "description": "Both companies have large source-backed revenue values; percentage changes avoid meaningless dollar increments.",
                "target": "esg",
                "company_ids": ["0000320193", "0000104169"],
                "default_feature": "financial_revenue_usd",
                "actions": {
                    "decrease": {
                        "label": "−50% per click",
                        "operation": "percent",
                        "value": -50,
                    },
                    "increase": {
                        "label": "+100% per click",
                        "operation": "percent",
                        "value": 100,
                    },
                },
            },
        ],
        "reach_preset": {
            "company_id": "0000001800",
            "target": "esg",
            "model_family": "ebm",
            "desired_score": 50,
        },
        "ranking_preset": {
            "label": "Environment-first ranking",
            "target": "esg",
            "company_ids": [
                "0001701605",
                "0001551152",
                "0000012927",
                "0000014272",
                "0000064803",
                "0001467858",
                "0001002047",
                "0000872589",
                "0000097476",
                "0000313616",
            ],
            "weights": {"environmental": 3},
        },
        "fund_preset": {
            "label": "Rapid net-zero allocation",
            "target": "csa",
            "objective": "financial_resilience",
            "sustainability_eligible_fraction": 0.75,
            "company_ids": [
                "0001701605",
                "0001551152",
                "0000012927",
                "0000014272",
                "0000064803",
                "0001467858",
                "0001002047",
                "0000872589",
                "0000097476",
                "0000313616",
                "0001751788",
                "0000866374",
                "0000047111",
                "0000064040",
                "0000093556",
                "0000310158",
                "0001341439",
                "0000075677",
                "0000749251",
                "0000021344",
            ],
            "adjustments": [
                {
                    "feature_name": "env_renewable_electricity_percent",
                    "operation": "set",
                    "value": 100,
                },
                {
                    "feature_name": "env_scope_1_tco2e",
                    "operation": "percent",
                    "value": -50,
                },
            ],
            "weights": {"environmental": 3, "climate_target": 3},
            "constraints": {
                "fund_usd": 1000000000,
                "max_companies": 10,
                "max_company_weight": 0.15,
                "max_industry_weight": 0.3,
                "min_coverage": 0.45,
            },
        },
    }


@router.post("/companies")
def companies(body: CompanyQuery, request: Request):
    view_access._require_same_origin(request)
    try:
        return store.query_companies(load_settings(), _snapshot(), **body.model_dump())
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(
            503, "The comparison database is temporarily unavailable."
        ) from error


@router.post("/target-suggestions")
def target_suggestions(body: TargetSuggestionRequest, request: Request):
    """Search observed one-field scenarios against a requested model estimate."""
    view_access._require_same_origin(request)
    if not _requests.acquire(blocking=False):
        raise HTTPException(
            429, "Two model searches are running. Please retry shortly."
        )
    try:
        snapshot = _snapshot()
        if body.snapshot_id != snapshot["snapshot_id"]:
            raise HTTPException(
                409, "The published report snapshot changed. Reload the demonstration."
            )
        settings = load_settings()
        row = store.source_rows(settings, snapshot["snapshot_id"], [body.company_id])[0]
        from green500.ml.target_suggestions import suggest_target_adjustments

        return suggest_target_adjustments(
            _service(snapshot),
            row,
            body.target,
            body.desired_score,
            body.model_family,
            store.observed_feature_values(
                settings, snapshot["snapshot_id"], body.target
            ),
            max_suggestions=body.max_suggestions,
        )
    except KeyError as error:
        raise HTTPException(
            404, "The selected company is absent from this snapshot."
        ) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(
            503, "The comparison database is temporarily unavailable."
        ) from error
    finally:
        _requests.release()


def _rank_map(items, key):
    ordered = sorted(
        items, key=lambda item: (-key(item), item["company"]["company_cik"])
    )
    return {
        item["company"]["company_cik"]: rank for rank, item in enumerate(ordered, 1)
    }


def rank_published_values(values: list[dict], weights: dict[str, float] | None) -> dict:
    """Reweight stored signed EBM categories for a complete published ranking."""
    from green500.ml.personalization import WEIGHTED_CATEGORIES, validate_weights

    normalized_weights = validate_weights(weights)
    if not isinstance(values, list) or not values:
        raise ValueError("Published ranking needs at least one company.")
    companies = []
    seen = set()
    for position, value in enumerate(values):
        if not isinstance(value, dict):
            raise TypeError(f"Published ranking row {position} must be an object.")
        company_cik = value.get("company_cik")
        if not isinstance(company_cik, str) or not company_cik:
            raise ValueError(f"Published ranking row {position} has no company CIK.")
        if company_cik in seen:
            raise ValueError("Published ranking contains duplicate company CIKs.")
        seen.add(company_cik)
        contributions = value.get("category_contributions")
        if not isinstance(contributions, dict) or set(contributions) != set(
            WEIGHTED_CATEGORIES
        ):
            raise ValueError(
                f"Published ranking row {position} has invalid category contributions."
            )
        numeric_contributions = {
            category: float(contributions[category]) for category in WEIGHTED_CATEGORIES
        }
        numeric_values = [
            value.get("ebm"),
            value.get("intercept"),
            value.get("fixed_context_contribution"),
            *numeric_contributions.values(),
        ]
        if any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            for item in numeric_values
        ):
            raise ValueError(f"Published ranking row {position} is non-finite.")
        raw_prediction = float(value["ebm"])
        reconstructed = (
            float(value["intercept"])
            + float(value["fixed_context_contribution"])
            + sum(numeric_contributions.values())
        )
        if not math.isclose(reconstructed, raw_prediction, rel_tol=1e-9, abs_tol=1e-8):
            raise ValueError(
                f"Published ranking row {position} does not reconstruct its EBM estimate."
            )
        weighted_effects = {
            category: (normalized_weights[category] - 1) * contribution
            for category, contribution in numeric_contributions.items()
        }
        personalized = raw_prediction + sum(weighted_effects.values())
        companies.append(
            {
                "company": {
                    "company_cik": company_cik,
                    "name": value.get("company_name"),
                    "ticker": value.get("ticker"),
                    "industry": value.get("industry"),
                },
                "raw_ebm_prediction": raw_prediction,
                "personalized_index": personalized,
                "category_contributions": numeric_contributions,
                "weighted_category_effect": sum(weighted_effects.values()),
                "weighted_category_effects": weighted_effects,
            }
        )
    original = sorted(
        companies,
        key=lambda item: (
            -item["raw_ebm_prediction"],
            item["company"]["company_cik"],
        ),
    )
    original_ranks = {
        item["company"]["company_cik"]: rank for rank, item in enumerate(original, 1)
    }
    companies.sort(
        key=lambda item: (
            -item["personalized_index"],
            item["company"]["company_cik"],
        )
    )
    for rank, item in enumerate(companies, 1):
        item["original_rank"] = original_ranks[item["company"]["company_cik"]]
        item["personalized_rank"] = rank
        item["rank_change"] = item["original_rank"] - rank
    return {
        "company_count": len(companies),
        "weights": normalized_weights,
        "companies": companies,
    }


@router.post("/rank")
def published_ranking(body: PublishedRankingRequest, request: Request):
    """Continuously reweight all stored companies without rerunning either model."""
    view_access._require_same_origin(request)
    snapshot = _snapshot()
    if body.snapshot_id != snapshot["snapshot_id"]:
        raise HTTPException(
            409, "The published report snapshot changed. Reload the demonstration."
        )
    try:
        result = rank_published_values(
            store.published_ranking_values(
                load_settings(), snapshot["snapshot_id"], body.target
            ),
            body.weights,
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(
            503, "The comparison database is temporarily unavailable."
        ) from error
    return {
        **result,
        "target": body.target,
        "snapshot_id": snapshot["snapshot_id"],
        "run_id": snapshot["run_id"],
        "label": "Personalized Green500 index",
    }


@router.post("/renewable-scenario")
def renewable_scenario(body: RenewableScenarioRequest, request: Request):
    """Update all published company scores and sector shares for one renewable scenario."""
    view_access._require_same_origin(request)
    if not _requests.acquire(blocking=False):
        raise HTTPException(
            429, "Two model scenarios are running. Please retry shortly."
        )
    try:
        snapshot = _snapshot()
        if body.snapshot_id != snapshot["snapshot_id"]:
            raise HTTPException(
                409, "The published report snapshot changed. Reload the demonstration."
            )
        rows = store.published_scenario_rows(
            load_settings(), snapshot["snapshot_id"], body.target
        )
        if len(rows) != snapshot["company_count"]:
            raise ValueError(
                "The published renewable scenario does not contain every company."
            )
        from green500.ml.renewable_scenario import evaluate_renewable_scenario

        result = evaluate_renewable_scenario(
            _service(snapshot),
            rows,
            body.target,
            body.model_family,
            body.renewable_multiplier,
        )
        return {
            **result,
            "snapshot_id": snapshot["snapshot_id"],
            "run_id": snapshot["run_id"],
        }
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(
            503, "The comparison database is temporarily unavailable."
        ) from error
    finally:
        _requests.release()


def _interpret(
    original,
    scenario,
    changes,
    original_rank,
    scenario_rank,
    personalized_rank,
    fields,
    weights,
):
    company = original["company"]
    own_changes = [
        change for change in changes if change["company_cik"] == company["company_cik"]
    ]
    applied = [change for change in own_changes if change["status"] == "applied"]
    skipped = len(own_changes) - len(applied)
    ebm = scenario["predictions"]["ebm"] - original["predictions"]["ebm"]
    catboost = scenario["predictions"]["catboost"] - original["predictions"]["catboost"]
    texts = [
        f"{len(applied)} hypothetical field adjustments: EBM changes by {ebm:+.2f} points and CatBoost by {catboost:+.2f} points."
    ]
    for change in applied[:5]:
        definition = fields[change["feature_name"]]
        before = (
            "missing"
            if change["original_value"] is None
            else f"{change['original_value']:,.4g}"
        )
        after = f"{change['scenario_value']:,.4g}"
        unit = definition.get("canonical_unit") or ""
        texts.append(
            f"{definition['label']}: {before} → {after} {unit} under the scenario."
        )
    if skipped:
        texts.append(
            f"{skipped} adjustments could not be applied because the original value is missing; those values stay missing."
        )
    deltas = {
        name: value - original["feature_contributions"].get(name, 0)
        for name, value in scenario["feature_contributions"].items()
    }
    largest = sorted(deltas, key=lambda name: abs(deltas[name]), reverse=True)[:3]
    changed = [
        f"{fields[name]['label']} ({deltas[name]:+.2f} points)"
        for name in largest
        if abs(deltas[name]) > 1e-8
    ]
    if changed:
        texts.append(
            "Largest changes in EBM contributions: " + "; ".join(changed) + "."
        )
    if ebm * catboost < 0:
        texts.append(
            "The two models disagree on the direction of this sensitivity response."
        )
    elif abs(ebm) < 1e-8 and abs(catboost) < 1e-8:
        texts.append(
            "These changes do not alter either model estimate at the saved model's resolution."
        )
    else:
        texts.append(
            "The two model responses should be compared in both direction and magnitude."
        )
    texts.append(
        f"Within the same selected cohort, EBM rank changes from {original_rank} to {scenario_rank}; the scenario with category multipliers ranks {personalized_rank}."
    )
    category_labels = {
        "financial": "Financial",
        "social": "People & society",
        "environmental": "Environment",
        "climate_target": "Climate targets",
        "financial_target": "Financial targets",
    }
    preference_changes = {
        category: (weights.get(category, 1) - 1) * contribution
        for category, contribution in scenario["category_contributions"].items()
    }
    preference_details = [
        f"{category_labels[category]} at {weights.get(category, 1):g}× ({change:+.2f} index points)"
        for category, change in sorted(
            preference_changes.items(), key=lambda item: abs(item[1]), reverse=True
        )
        if abs(change) > 1e-8
    ]
    if preference_details:
        texts.append(
            "Personal priorities change the index through signed category contributions: "
            + "; ".join(preference_details)
            + ". Company context stays fixed."
        )
    coverage = original["coverage"]
    missing = (
        coverage["active_feature_count"] - coverage["available_active_feature_count"]
    )
    if missing:
        texts.append(
            f"{missing} active inputs are missing. The models' learned missing-value contributions affect these estimates."
        )
    texts.append(
        "This is model sensitivity under stated assumptions; it does not establish a real-world rating improvement or investment return."
    )
    return texts


def compare_companies(body: ComparisonRequest) -> dict:
    """Compute all three ranks on precisely the same published company cohort."""
    if len(set(body.company_ids)) != len(body.company_ids):
        raise ValueError("Select each company only once.")
    snapshot = _snapshot()
    if body.snapshot_id and body.snapshot_id != snapshot["snapshot_id"]:
        raise HTTPException(
            409,
            "The published report snapshot changed. Reload the comparison before applying a scenario.",
        )
    service = _service(snapshot)
    fields = {
        field["feature_name"]: field
        for field in snapshot["summary"]["targets"][body.target]["fields"]
    }
    rows = store.source_rows(load_settings(), snapshot["snapshot_id"], body.company_ids)
    adjustments = [value.model_dump() for value in body.adjustments]
    scenario_rows, changes = service.apply_scenario(rows, body.target, adjustments)
    result = {
        "run_id": snapshot["run_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "prediction_as_of": str(snapshot["prediction_as_of"]),
        "target": body.target,
        "changes": changes,
        "warnings": [],
        "interpretation": [],
        "companies": [],
    }
    if body.preview_only:
        return result
    original = service.evaluate_rows(rows, body.target)
    scenario = service.evaluate_rows(scenario_rows, body.target, body.weights)
    original_ranks = _rank_map(original, lambda item: item["predictions"]["ebm"])
    scenario_ranks = _rank_map(scenario, lambda item: item["predictions"]["ebm"])
    personalized_ranks = _rank_map(scenario, lambda item: item["personalized_index"])
    for baseline, modified in zip(original, scenario, strict=True):
        cik = baseline["company"]["company_cik"]
        interpretation = _interpret(
            baseline,
            modified,
            changes,
            original_ranks[cik],
            scenario_ranks[cik],
            personalized_ranks[cik],
            fields,
            body.weights,
        )
        result["companies"].append(
            {
                "company": baseline["company"],
                "original": baseline,
                "scenario": modified,
                "differences": {
                    family: modified["predictions"][family]
                    - baseline["predictions"][family]
                    for family in ("ebm", "catboost")
                },
                "original_rank": original_ranks[cik],
                "scenario_rank": scenario_ranks[cik],
                "personalized_rank": personalized_ranks[cik],
                "interpretation": interpretation,
            }
        )
    result["companies"].sort(key=lambda item: item["personalized_rank"])
    result["interpretation"] = [
        f"Compared {len(rows)} companies using one report snapshot and the same selected cohort for all rankings.",
        "Original and scenario ranks use raw EBM estimates. Personalized ranks apply category multipliers to signed EBM contributions, with company context fixed.",
        "Interpretations are computed directly from model outputs and recorded adjustments.",
    ]
    return result


def _bounded_comparison(body, is_portfolio=False):
    if not _requests.acquire(blocking=False):
        raise HTTPException(429, "Two comparisons are running. Please retry shortly.")
    try:
        if is_portfolio:
            body = body.model_copy(update={"preview_only": False})
        result = compare_companies(body)
        if not is_portfolio:
            return result
        from green500.ml.portfolio import (
            allocate_portfolio,
            optimize_financial_resilience_portfolio,
        )

        constraints = body.constraints.model_dump()
        if body.objective == "financial_resilience":
            baseline = optimize_financial_resilience_portfolio(
                [item["original"] for item in result["companies"]],
                constraints,
                sustainability_eligible_fraction=body.sustainability_eligible_fraction,
            )
            scenario = optimize_financial_resilience_portfolio(
                [item["scenario"] for item in result["companies"]],
                constraints,
                sustainability_eligible_fraction=body.sustainability_eligible_fraction,
            )
        else:
            baseline = allocate_portfolio(
                [item["original"] for item in result["companies"]], constraints
            )
            scenario = allocate_portfolio(
                [item["scenario"] for item in result["companies"]], constraints
            )
        return {
            "baseline": baseline,
            "scenario": scenario,
            "objective": body.objective,
            "run_id": result["run_id"],
            "snapshot_id": result["snapshot_id"],
            "interpretation": [
                "Both portfolios use the same candidate cohort and explicit company, industry and disclosure constraints. Selected holdings may differ.",
                "The baseline uses original measurements and unit multipliers; the scenario uses the selected adjustments and category multipliers.",
                f"Unallocated cash: baseline ${baseline['unallocated_usd']:,.2f}; scenario ${scenario['unallocated_usd']:,.2f}. Constraints are not relaxed automatically.",
                "The sustainability models determine eligibility under the stated scenario. The financial-resilience objective uses current profitability, cash flow and leverage inputs; it does not predict investment returns.",
            ],
        }
    except KeyError as error:
        raise HTTPException(
            404, "A selected company is absent from the published snapshot."
        ) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(
            503, "The comparison database is temporarily unavailable."
        ) from error
    finally:
        _requests.release()


@router.post("/compare")
def compare(body: ComparisonRequest, request: Request):
    view_access._require_same_origin(request)
    return _bounded_comparison(body)


@router.post("/portfolio")
def portfolio(body: PortfolioRequest, request: Request):
    view_access._require_same_origin(request)
    return _bounded_comparison(body, is_portfolio=True)


def main():
    """Publish current report measurements without retraining or extraction."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish"])
    parser.add_argument("--model-run", type=Path, required=True)
    parser.add_argument(
        "--prediction-as-of", type=date.fromisoformat, default=datetime.now(UTC).date()
    )
    args = parser.parse_args()
    settings = load_settings()
    from green500.ml.artifacts import sha256_file
    from green500.ml.inference import _saved_availability_policy, load_prediction_run
    from green500.ml.personalization import (
        _build_default_cohort_rows,
        _cohort_company_ids,
    )
    from green500.ml.serving import SavedModelService

    run_dir, _, manifest = load_prediction_run(args.model_run)
    destination = settings.data_dir / "ml" / "comparison_runs" / manifest["run_id"]
    if destination.exists() and sha256_file(
        destination / "run_manifest.json"
    ) != sha256_file(run_dir / "run_manifest.json"):
        raise ValueError(
            "An installed run with the same identifier has different model contents."
        )
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / ".installing" / destination.name
        if temporary.exists():
            raise FileExistsError(
                "A comparison run installation is already in progress."
            )
        try:
            temporary.parent.mkdir(exist_ok=True)
            shutil.copytree(run_dir, temporary)
            load_prediction_run(temporary)
            temporary.rename(destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    service = SavedModelService(destination)
    rows = _build_default_cohort_rows(
        settings,
        _cohort_company_ids(settings, None),
        args.prediction_as_of.isoformat(),
        "comparison",
        _saved_availability_policy(destination, manifest),
    )
    result = store.publish_snapshot(settings, service, rows)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "run_id",
                    "snapshot_id",
                    "prediction_as_of",
                    "company_count",
                )
            },
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
