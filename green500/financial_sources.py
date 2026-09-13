"""Collect current SEC Company Facts with explicit periods and source versions."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, date, datetime
from typing import Any

import scrapy

from green500 import db
from green500.config import load_settings

SOURCE_KEY = "sec_companyfacts"
SEC_COMPANYFACTS_URL = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK{company_cik}.json"
)
SEC_USER_AGENT = "Green500 public-data research crawler/0.1"

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTERLY_FORMS = {
    "10-Q",
    "10-Q/A",
    "10-K",
    "10-K/A",
    "8-K",
    "8-K/A",
    "20-F",
    "20-F/A",
    "6-K",
    "6-K/A",
}
INSTANT_FORMS = ANNUAL_FORMS | QUARTERLY_FORMS

# A bank's net revenue after interest expense is more comparable to its reported
# top line than customer-contract revenue. A newer reporting period always wins
# before this priority is applied.
REVENUE_CONCEPTS = (
    "RevenuesNetOfInterestExpense",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)


def normalize_cik(value: object) -> str:
    """Return one ten-digit SEC registrant identifier or reject the input."""
    if isinstance(value, bool):
        raise TypeError("A company CIK must contain one to ten digits.")
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,10}", text):
        raise ValueError("A company CIK must contain one to ten digits.")
    return text.zfill(10)


def _reject_non_json_number(value: str) -> None:
    """Reject non-standard JSON constants such as NaN and Infinity."""
    raise ValueError(f"Company Facts contains the invalid JSON number {value}.")


def _read_companyfacts(body: bytes) -> dict[str, Any]:
    """Decode one complete strict JSON object with the documented root fields."""
    if not body:
        raise ValueError("Company Facts returned an empty response.")
    try:
        value = json.loads(body.decode("utf-8"), parse_constant=_reject_non_json_number)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "Company Facts returned malformed or truncated JSON."
        ) from error
    if not isinstance(value, dict):
        raise TypeError("Company Facts must be one JSON object.")
    return value


def _read_iso_date(value: object, subject: str) -> date:
    """Read a required SEC ISO date without accepting timestamps or loose text."""
    if not isinstance(value, str):
        raise TypeError(f"Company Facts {subject} must be an ISO date.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Company Facts {subject} must be an ISO date.") from error
    if parsed.isoformat() != value:
        raise ValueError(f"Company Facts {subject} must be an ISO date.")
    return parsed


def _json_number_string(value: object) -> str:
    """Preserve a JSON number as text for exact database decimal conversion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("A selected Company Facts value must be a JSON number.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("A selected Company Facts value must be finite.")
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def _concept_facts(
    us_gaap_facts: dict[str, Any], concept: str, current_date: date
) -> list[dict[str, Any]]:
    """Return validated USD facts for one standard taxonomy concept."""
    concept_value = us_gaap_facts.get(concept)
    if concept_value is None:
        return []
    if not isinstance(concept_value, dict):
        raise TypeError(f"Company Facts concept {concept} must be an object.")
    units = concept_value.get("units")
    if not isinstance(units, dict):
        raise TypeError(f"Company Facts concept {concept} has no units object.")
    facts = units.get("USD", [])
    if not isinstance(facts, list):
        raise TypeError(f"Company Facts concept {concept} USD facts must be a list.")

    validated = []
    for exact_fact in facts:
        if not isinstance(exact_fact, dict):
            raise TypeError(
                f"Company Facts concept {concept} contains a non-object fact."
            )
        period_end = _read_iso_date(exact_fact.get("end"), "period end")
        published_at = _read_iso_date(exact_fact.get("filed"), "filed date")
        if period_end > current_date or published_at > current_date:
            raise ValueError(
                f"Company Facts concept {concept} contains a future period or filing date."
            )
        period_start = None
        if exact_fact.get("start") is not None:
            period_start = _read_iso_date(exact_fact["start"], "period start")
            if period_start > period_end:
                raise ValueError(
                    f"Company Facts concept {concept} has a period start after its end."
                )
        accession = exact_fact.get("accn")
        form = exact_fact.get("form")
        if not isinstance(accession, str) or not accession:
            raise ValueError(
                f"Company Facts concept {concept} has no accession number."
            )
        if not isinstance(form, str) or not form:
            raise ValueError(f"Company Facts concept {concept} has no filing form.")
        numeric_value = _json_number_string(exact_fact.get("val"))
        validated.append(
            {
                "concept": concept,
                "fact": {**exact_fact, "unit": "USD"},
                "period_start_date": period_start,
                "period_end_date": period_end,
                "published_date": published_at,
                "numeric_value": numeric_value,
            }
        )
    return validated


def _is_annual_fact(candidate: dict[str, Any]) -> bool:
    """Return whether one duration fact represents a complete annual filing period."""
    start = candidate["period_start_date"]
    if start is None:
        return False
    duration_days = (candidate["period_end_date"] - start).days + 1
    fact = candidate["fact"]
    return (
        fact["form"] in ANNUAL_FORMS
        and fact.get("fp") == "FY"
        and 300 <= duration_days <= 430
    )


def _is_quarterly_fact(candidate: dict[str, Any]) -> bool:
    """Return whether one duration fact is a standalone quarter, not year-to-date."""
    start = candidate["period_start_date"]
    if start is None:
        return False
    duration_days = (candidate["period_end_date"] - start).days + 1
    return candidate["fact"]["form"] in QUARTERLY_FORMS and 60 <= duration_days <= 120


def _is_instant_fact(candidate: dict[str, Any]) -> bool:
    """Return whether one balance-sheet fact is reported at one date."""
    return (
        candidate["period_start_date"] is None
        and candidate["fact"]["form"] in INSTANT_FORMS
    )


def _deduplicated_exact_facts(
    candidates: list[dict[str, Any]], selected: dict[str, Any]
) -> list[dict[str, Any]]:
    """Keep every distinct publication of the selected concept and exact period."""
    matching = [
        candidate
        for candidate in candidates
        if candidate["concept"] == selected["concept"]
        and candidate["period_start_date"] == selected["period_start_date"]
        and candidate["period_end_date"] == selected["period_end_date"]
    ]
    matching.sort(
        key=lambda candidate: (
            candidate["published_date"],
            candidate["fact"]["accn"],
        )
    )
    exact_facts = []
    seen = set()
    for candidate in matching:
        signature = json.dumps(candidate["fact"], sort_keys=True, separators=(",", ":"))
        if signature not in seen:
            seen.add(signature)
            exact_facts.append(candidate["fact"])
    return exact_facts


def _select_observation(
    *,
    metric_code: str,
    period_type: str,
    concepts: tuple[str, ...],
    us_gaap_facts: dict[str, Any],
    current_date: date,
) -> dict[str, Any] | None:
    """Choose the latest period, then the declared concept and publication priority."""
    all_candidates = []
    for priority, concept in enumerate(concepts):
        for candidate in _concept_facts(us_gaap_facts, concept, current_date):
            candidate["concept_priority"] = priority
            all_candidates.append(candidate)

    period_test = {
        "annual": _is_annual_fact,
        "quarterly": _is_quarterly_fact,
        "instant": _is_instant_fact,
    }[period_type]
    eligible = [candidate for candidate in all_candidates if period_test(candidate)]
    if not eligible:
        return None

    newest_period_end = max(candidate["period_end_date"] for candidate in eligible)
    newest_period = [
        candidate
        for candidate in eligible
        if candidate["period_end_date"] == newest_period_end
    ]
    preferred_concept = min(
        candidate["concept_priority"] for candidate in newest_period
    )
    preferred = [
        candidate
        for candidate in newest_period
        if candidate["concept_priority"] == preferred_concept
    ]
    selected = max(
        preferred,
        key=lambda candidate: (
            candidate["published_date"],
            candidate["fact"]["accn"],
        ),
    )
    same_period_facts = _deduplicated_exact_facts(eligible, selected)
    same_period_values = {
        _json_number_string(fact["val"]) for fact in same_period_facts
    }
    source_record = {
        "concept": f"us-gaap:{selected['concept']}",
        "fact": selected["fact"],
        "same_period_facts": same_period_facts,
        "has_differing_same_period_value": len(same_period_values) > 1,
        "is_amendment": selected["fact"]["form"].endswith("/A"),
        "selection_reason": (
            f"Selected the latest {period_type} period end, then concept priority "
            "and the latest publication for that exact period."
        ),
    }
    observation = {
        "metric_code": metric_code,
        "value": selected["numeric_value"],
        "unit": "USD",
        "period_end": selected["period_end_date"].isoformat(),
        "period_type": period_type,
        "published_at": selected["published_date"].isoformat(),
        "source_record": source_record,
    }
    if selected["period_start_date"] is not None:
        observation["period_start"] = selected["period_start_date"].isoformat()
    return observation


def parse_companyfacts(
    body: bytes, expected_cik: str, current_date: date | None = None
) -> dict[str, Any]:
    """Validate one company response and select current annual, quarter and instant facts."""
    company_cik = normalize_cik(expected_cik)
    payload = _read_companyfacts(body)
    try:
        response_cik = normalize_cik(payload["cik"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Company Facts has no valid entity CIK.") from error
    if response_cik != company_cik:
        raise ValueError(
            f"Company Facts returned CIK {response_cik} for requested CIK {company_cik}."
        )
    entity_name = payload.get("entityName")
    if not isinstance(entity_name, str) or not entity_name.strip():
        raise ValueError("Company Facts has no entity name.")
    facts = payload.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("us-gaap"), dict):
        raise TypeError("Company Facts has no us-gaap facts object.")
    us_gaap_facts = facts["us-gaap"]
    today = current_date or datetime.now(tz=UTC).date()

    requested = (
        ("revenue", "annual", REVENUE_CONCEPTS),
        ("revenue", "quarterly", REVENUE_CONCEPTS),
        ("net_income", "annual", ("NetIncomeLoss",)),
        ("net_income", "quarterly", ("NetIncomeLoss",)),
        ("assets", "instant", ("Assets",)),
    )
    observations = []
    missing_observations = []
    for metric_code, period_type, concepts in requested:
        observation = _select_observation(
            metric_code=metric_code,
            period_type=period_type,
            concepts=concepts,
            us_gaap_facts=us_gaap_facts,
            current_date=today,
        )
        if observation is None:
            missing_observations.append(f"{metric_code}:{period_type}")
        else:
            observations.append(observation)
    return {
        "company_cik": company_cik,
        "entity_name": entity_name.strip(),
        "observations": observations,
        "missing_observations": missing_observations,
    }


def _current_company_ciks(settings, limit: int | None) -> list[str]:
    """Read the current constituent CIKs without keeping a connection open."""
    query = "SELECT cik FROM companies WHERE is_current ORDER BY cik"
    parameters: tuple[object, ...] = ()
    if limit is not None:
        query += " LIMIT %s"
        parameters = (limit,)
    with db.connect(settings) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [normalize_cik(row["cik"]) for row in rows]


def requested_company_ciks(task: dict[str, Any], settings) -> list[str]:
    """Resolve a one-company, explicit-list, limited, or complete current request."""
    company_cik = task.get("company_cik")
    task_input = task.get("input") or {}
    if not isinstance(task_input, dict):
        raise TypeError("Financial task input must be an object.")
    if company_cik is not None:
        return [normalize_cik(company_cik)]

    supplied_ciks = task_input.get("ciks")
    limit = task_input.get("limit")
    if supplied_ciks is not None and limit is not None:
        raise ValueError("Financial task input may contain ciks or limit, not both.")
    if supplied_ciks is not None:
        if not isinstance(supplied_ciks, list) or not all(
            isinstance(cik, str) for cik in supplied_ciks
        ):
            raise ValueError("Financial task ciks must be a list of strings.")
        ciks = list(dict.fromkeys(normalize_cik(cik) for cik in supplied_ciks))
    else:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("Financial task limit must be a positive integer.")
        ciks = _current_company_ciks(settings, limit)
    if not ciks:
        raise ValueError("Financial task resolved to no current companies.")
    return ciks


class FinancialFactsPipeline:
    """Persist each raw response, observations and source check outside the event loop."""

    def process_item(self, item, spider):
        """Return an awaited persistence receipt for every completed request."""
        from twisted.internet.threads import deferToThread

        return deferToThread(self.persist, item, spider)

    def persist(self, item, spider):
        """Save one immutable response before publishing its source observations."""
        from green500 import data_store

        company_cik = item["company_cik"]
        dataset_id = None
        try:
            body = item.get("body")
            if body is not None:
                receipt = data_store.save_dataset(
                    spider.settings_value,
                    SOURCE_KEY,
                    item["url"],
                    body,
                    item.get("content_type", ""),
                    company_cik=company_cik,
                    task_id=spider.task_id,
                )
                if not isinstance(receipt, dict) or receipt.get("id") is None:
                    raise ValueError("Dataset persistence returned no dataset id.")
                dataset_id = receipt["id"]
                spider.dataset_count += 1

            if item.get("error"):
                error = item["error"][:1000]
                data_store.record_check(
                    spider.settings_value,
                    SOURCE_KEY,
                    company_cik,
                    "failed",
                    error=error,
                    dataset_id=dataset_id,
                    details={"url": item["url"]},
                )
                spider.record_company_result(company_cik, "failed", 0, error)
                return item

            parsed = item["parsed"]
            observations = parsed["observations"]
            if not observations:
                error = "Company Facts contained no supported current financial observations."
                data_store.record_check(
                    spider.settings_value,
                    SOURCE_KEY,
                    company_cik,
                    "failed",
                    error=error,
                    dataset_id=dataset_id,
                    details={
                        "url": item["url"],
                        "entity_name": parsed["entity_name"],
                        "missing_observations": parsed["missing_observations"],
                    },
                )
                spider.record_company_result(company_cik, "failed", 0, error)
                return item

            saved_count = data_store.save_observations(
                spider.settings_value,
                SOURCE_KEY,
                company_cik,
                dataset_id,
                observations,
                match_method="identifier",
                task_id=spider.task_id,
            )
            missing = parsed["missing_observations"]
            status = "partial" if missing else "succeeded"
            error = (
                "Missing current observations: " + ", ".join(missing)
                if missing
                else None
            )
            data_store.record_check(
                spider.settings_value,
                SOURCE_KEY,
                company_cik,
                status,
                error=error,
                dataset_id=dataset_id,
                details={
                    "url": item["url"],
                    "entity_name": parsed["entity_name"],
                    "observation_count": len(observations),
                    "saved_observation_count": saved_count,
                    "missing_observations": missing,
                },
            )
            spider.saved_observation_count += int(saved_count)
            spider.record_company_result(company_cik, status, len(observations), error)
        except Exception as error:  # noqa: BLE001 - record external persistence failures.
            message = f"Financial response persistence failed: {error}"[:1000]
            spider.record_company_result(company_cik, "failed", 0, message)
            data_store.record_check(
                spider.settings_value,
                SOURCE_KEY,
                company_cik,
                "failed",
                error=message,
                dataset_id=dataset_id,
                details={"url": item["url"]},
            )
        return item


class SecCompanyFactsSpider(scrapy.Spider):
    """Request one bounded Company Facts response for each selected current company."""

    name = "green500_sec_companyfacts"
    allowed_domains = ("data.sec.gov",)

    def __init__(self, task, company_ciks, settings_value, **kwargs):
        """Bind the task, fixed company list and persistence counters."""
        super().__init__(**kwargs)
        self.task = task
        self.task_id = task.get("id")
        self.company_ciks = company_ciks
        self.settings_value = settings_value
        self.dataset_count = 0
        self.saved_observation_count = 0
        self.observation_count = 0
        self.status_counts = {"succeeded": 0, "partial": 0, "failed": 0}
        self.failed_ciks = []
        self.partial_ciks = []
        self.failures = []

    async def start(self):
        """Issue only stable SEC Company Facts requests from the resolved CIK list."""
        for company_cik in self.company_ciks:
            url = SEC_COMPANYFACTS_URL.format(company_cik=company_cik)
            yield scrapy.Request(
                url,
                callback=self.parse_companyfacts_response,
                errback=self.record_request_failure,
                cb_kwargs={"expected_cik": company_cik},
                meta={"company_cik": company_cik, "source_url": url},
            )

    def parse_companyfacts_response(self, response, expected_cik):
        """Convert one valid SEC response into a pipeline persistence item."""
        content_type = response.headers.get("Content-Type", b"").decode("latin1")
        try:
            parsed = parse_companyfacts(response.body, expected_cik)
            error = None
        except (TypeError, ValueError) as parse_error:
            parsed = None
            error = str(parse_error)
        yield {
            "company_cik": expected_cik,
            "url": response.meta["source_url"],
            "body": response.body,
            "content_type": content_type,
            "parsed": parsed,
            "error": error,
        }

    def record_request_failure(self, failure):
        """Persist an HTTP failure body when available and keep the CIK visible."""
        response = getattr(failure.value, "response", None)
        content_type = ""
        body = None
        if response is not None:
            body = response.body
            content_type = response.headers.get("Content-Type", b"").decode("latin1")
        return {
            "company_cik": failure.request.meta["company_cik"],
            "url": failure.request.meta["source_url"],
            "body": body,
            "content_type": content_type,
            "parsed": None,
            "error": failure.getErrorMessage(),
        }

    def record_company_result(
        self, company_cik: str, status: str, observation_count: int, error: str | None
    ) -> None:
        """Update deterministic task totals after one awaited persistence attempt."""
        self.status_counts[status] += 1
        self.observation_count += observation_count
        if status == "failed":
            self.failed_ciks.append(company_cik)
        elif status == "partial":
            self.partial_ciks.append(company_cik)
        if error:
            self.failures.append({"company_cik": company_cik, "error": error})


def collect_financial_task(task: dict) -> dict:
    """Collect and persist current SEC facts with bounded domain concurrency."""
    from scrapy.crawler import CrawlerProcess

    settings = load_settings()
    company_ciks = requested_company_ciks(task, settings)
    process = CrawlerProcess(
        {
            "USER_AGENT": SEC_USER_AGENT,
            "DEFAULT_REQUEST_HEADERS": {
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            "ROBOTSTXT_OBEY": True,
            "CONCURRENT_REQUESTS": 1,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "DOWNLOAD_DELAY": 0.75,
            "RANDOMIZE_DOWNLOAD_DELAY": False,
            "DOWNLOAD_TIMEOUT": 30,
            "DOWNLOAD_MAXSIZE": settings.max_document_bytes,
            "DOWNLOAD_WARNSIZE": settings.max_document_bytes,
            "DOWNLOAD_FAIL_ON_DATALOSS": True,
            "RETRY_TIMES": 1,
            "REDIRECT_MAX_TIMES": 2,
            "LOG_LEVEL": "WARNING",
            "TELNETCONSOLE_ENABLED": False,
            "ITEM_PIPELINES": {
                "green500.financial_sources.FinancialFactsPipeline": 100
            },
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        }
    )
    crawler = process.create_crawler(SecCompanyFactsSpider)
    process.crawl(
        crawler,
        task=task,
        company_ciks=company_ciks,
        settings_value=settings,
    )
    process.start()
    spider = crawler.spider
    if spider is None:
        raise RuntimeError("SEC Company Facts collection did not start.")
    completed_count = sum(spider.status_counts.values())
    if completed_count != len(company_ciks):
        raise RuntimeError(
            "SEC Company Facts collection ended before every company received a status."
        )
    stats = crawler.stats.get_stats()
    if stats.get("spider_exceptions/count", 0) or stats.get("item_error_count", 0):
        raise RuntimeError(
            "SEC Company Facts collection had an unrecorded spider or persistence failure."
        )
    result = {
        "requested_count": len(company_ciks),
        "request_count": stats.get("downloader/request_count", 0),
        "dataset_count": spider.dataset_count,
        "observation_count": spider.observation_count,
        "saved_observation_count": spider.saved_observation_count,
        "succeeded_count": spider.status_counts["succeeded"],
        "partial_count": spider.status_counts["partial"],
        "failed_count": spider.status_counts["failed"],
        "failed_ciks": spider.failed_ciks,
        "partial_ciks": spider.partial_ciks,
        "failures": spider.failures,
    }
    result["is_partial"] = bool(result["partial_count"] or result["failed_count"])
    return result
