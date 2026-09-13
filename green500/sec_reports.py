"""Collect each company's latest SEC annual, quarterly and proxy originals."""

from __future__ import annotations

import html
import json
import re
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import scrapy
from psycopg.types.json import Jsonb

from green500 import db
from green500.config import load_settings
from green500.financial_sources import SEC_USER_AGENT, normalize_cik
from green500.freshness import validate_source_review
from green500.storage import read_bytes, save_bytes

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{company_cik}.json"
ARCHIVE_URL = (
    "https://www.sec.gov/Archives/edgar/data/{numeric_cik}/{accession}/{document}"
)
FORM_GROUPS = {
    "annual": {"10-K", "20-F", "40-F"},
    "quarterly": {"10-Q"},
    "proxy": {"DEF 14A"},
}
REPORT_FAMILIES = {
    "annual": "sec_annual_filing",
    "quarterly": "sec_quarterly_filing",
    "proxy": "sec_proxy_statement",
}
SEC_LISTING_NOTE_PREFIX = "SEC submissions listing SHA-256: "


def _read_date(value: object, field: str, *, allow_empty: bool = False) -> date | None:
    """Read one exact SEC date while allowing an explicitly optional report date."""
    if allow_empty and value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise TypeError(f"SEC submissions {field} must be an ISO date.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"SEC submissions {field} must be an ISO date.") from error
    if parsed.isoformat() != value:
        raise ValueError(f"SEC submissions {field} must be an ISO date.")
    return parsed


def _primary_document_url(company_cik: str, accession: str, document: str) -> str:
    """Build one archive URL from validated SEC identifiers and a relative filename."""
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError("SEC submissions contains an invalid accession number.")
    if (
        not isinstance(document, str)
        or not document
        or document.startswith("/")
        or ".." in document.split("/")
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", document)
    ):
        raise ValueError("SEC submissions contains an unsafe primary document name.")
    return ARCHIVE_URL.format(
        numeric_cik=int(company_cik),
        accession=accession.replace("-", ""),
        document=quote(document, safe="/._-"),
    )


def parse_latest_report_sources(body: bytes, expected_cik: str) -> dict[str, Any]:
    """Select only the latest current filing from each requested form group."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "SEC submissions returned malformed or truncated JSON."
        ) from error
    if not isinstance(payload, dict):
        raise TypeError("SEC submissions must be one JSON object.")
    company_cik = normalize_cik(expected_cik)
    try:
        response_cik = normalize_cik(payload["cik"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("SEC submissions has no valid entity CIK.") from error
    if response_cik != company_cik:
        raise ValueError(
            f"SEC submissions returned CIK {response_cik} for requested CIK {company_cik}."
        )
    entity_name = payload.get("name")
    if not isinstance(entity_name, str) or not entity_name.strip():
        raise ValueError("SEC submissions has no entity name.")
    try:
        recent = payload["filings"]["recent"]
    except (KeyError, TypeError) as error:
        raise ValueError("SEC submissions has no recent filings object.") from error
    if not isinstance(recent, dict):
        raise TypeError("SEC submissions recent filings must be an object.")
    required = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "primaryDocument",
    )
    columns = []
    for field in required:
        column = recent.get(field)
        if not isinstance(column, list):
            raise TypeError(f"SEC submissions recent {field} must be a list.")
        columns.append(column)
    if len({len(column) for column in columns}) != 1:
        raise ValueError(
            "SEC submissions recent filing columns have different lengths."
        )

    selected: dict[str, dict[str, Any]] = {}
    for values in zip(*columns, strict=True):
        row = dict(zip(required, values, strict=True))
        filing_form = row["form"]
        groups = [group for group, forms in FORM_GROUPS.items() if filing_form in forms]
        if not groups:
            continue
        filed = _read_date(row["filingDate"], "filing date")
        report_date = _read_date(row["reportDate"], "report date", allow_empty=True)
        accession = row["accessionNumber"]
        url = _primary_document_url(company_cik, accession, row["primaryDocument"])
        candidate = {
            "form_group": groups[0],
            "form": filing_form,
            "accession_number": accession,
            "filed_date": filed,
            "report_date": report_date,
            "url": url,
        }
        current = selected.get(groups[0])
        if current is None or (filed, accession) > (
            current["filed_date"],
            current["accession_number"],
        ):
            selected[groups[0]] = candidate

    sources = []
    for group in ("annual", "quarterly", "proxy"):
        filing = selected.get(group)
        if not filing:
            continue
        form = filing["form"]
        filed_text = filing["filed_date"].isoformat()
        report_text = (
            filing["report_date"].isoformat() if filing["report_date"] else None
        )
        title_kind = "Proxy statement" if group == "proxy" else f"SEC {form}"
        sources.append(
            {
                "url": filing["url"],
                "title": f"{entity_name.strip()} {title_kind} filed {filed_text}",
                "categories": ["financial_report"],
                "reporting_period": (
                    f"Report date {report_text}; filed {filed_text}"
                    if report_text
                    else f"Filed {filed_text}; report date not supplied"
                ),
                "evidence": [
                    {
                        "section": "SEC submissions metadata",
                        "form": form,
                        "accession_number": filing["accession_number"],
                        "filed_date": filed_text,
                        "report_date": report_text,
                    }
                ],
                "document_type": group,
                "filed_date": filed_text,
                "report_date": report_text,
            }
        )
    return {
        "company_cik": company_cik,
        "entity_name": entity_name.strip(),
        "sources": sources,
        "missing_forms": [group for group in FORM_GROUPS if group not in selected],
    }


def proxy_employee_content(body: bytes) -> list[str]:
    """Return concrete employee-pay or labor topics found in a proxy document."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", body.decode("utf-8", "ignore")))
    text = " ".join(text.split()).casefold()
    matches = []
    if re.search(r"\bpay ratio\b", text):
        matches.append("pay ratio")
    if re.search(r"median (?:annual )?employee.{0,240}(?:compensation|pay)", text):
        matches.append("median employee compensation")
    if re.search(r"\b(?:collective bargaining|labor relations)\b", text):
        matches.append("labor relations")
    return matches


def _requested_ciks(settings, task: dict[str, Any]) -> list[str]:
    """Resolve one task company or a stable limited current-company list."""
    if task.get("company_cik"):
        return [normalize_cik(task["company_cik"])]
    task_input = task.get("input") or {}
    if not isinstance(task_input, dict):
        raise TypeError("SEC report task input must be an object.")
    limit = task_input.get("limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise ValueError("SEC report task limit must be a positive integer.")
    query = "SELECT cik FROM companies WHERE is_current ORDER BY name,cik"
    parameters: tuple[object, ...] = ()
    if limit is not None:
        query += " LIMIT %s"
        parameters = (limit,)
    with db.connect(settings) as connection:
        rows = connection.execute(query, parameters).fetchall()
    if not rows:
        raise ValueError("SEC report task resolved to no current companies.")
    return [normalize_cik(row["cik"]) for row in rows]


def _verified_existing_report(
    settings, company_cik: str, url: str
) -> dict[str, Any] | None:
    """Return a stored report only after its immutable bytes pass SHA-256 verification."""
    with db.connect(settings) as connection:
        rows = connection.execute(
            """SELECT id,sha256,byte_count,content_type,final_url
            FROM documents
            WHERE company_cik=%s AND url=%s AND kind='report'
              AND byte_count > 0 AND (%s=0 OR byte_count<=%s)
            ORDER BY id DESC""",
            (company_cik, url, settings.max_document_bytes, settings.max_document_bytes),
        ).fetchall()
    for row in rows:
        try:
            body = read_bytes(settings.data_dir, row["sha256"])
        except (FileNotFoundError, ValueError):
            continue
        if len(body) != row["byte_count"]:
            continue
        return {
            "document_id": row["id"],
            "sha256": row["sha256"],
            "byte_count": row["byte_count"],
            "content_type": row["content_type"],
            "final_url": row["final_url"],
        }
    return None


def _find_reusable_reports(
    settings, company_cik: str, sources: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Verify every selected SEC archive URL and return the reusable originals."""
    reusable = {}
    for source in sources:
        existing = _verified_existing_report(settings, company_cik, source["url"])
        if existing:
            reusable[source["url"]] = existing
    return reusable


def partition_report_sources_for_resume(
    sources: list[dict[str, Any]], reusable_by_url: dict[str, dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]]]:
    """Separate verified originals from sources that still require a network request."""
    reused = []
    downloads = []
    for source in sources:
        existing = reusable_by_url.get(source["url"])
        if existing:
            reused.append((source, existing))
        else:
            downloads.append(source)
    return reused, downloads


def _save_submissions_listing(
    settings,
    task_id: int,
    company_cik: str,
    entity_name: str,
    url: str,
    final_url: str,
    body: bytes,
    content_type: str,
) -> dict[str, Any]:
    """Store the exact parsed SEC listing and return its review identity."""
    if not body or (settings.max_document_bytes and len(body) > settings.max_document_bytes):
        raise ValueError("SEC submissions listing is empty or exceeds the byte limit.")
    digest = save_bytes(settings.data_dir, body)
    checked_at = datetime.now(UTC)
    with db.connect(settings) as connection:
        db.require_active_task(connection, task_id)
        document_id = db.record_document(
            connection,
            company_cik=company_cik,
            url=url,
            final_url=final_url,
            title=f"{entity_name} SEC current submissions listing",
            kind="directory",
            digest=digest,
            byte_count=len(body),
            content_type=content_type or "application/json",
        )
        connection.execute(
            """UPDATE documents SET source_key='sec_submissions',parse_status='succeeded'
            WHERE id=%s""",
            (document_id,),
        )
    return {
        "document_id": document_id,
        "sha256": digest,
        "checked_at": checked_at.isoformat(),
        "evidence_url": url,
    }


def _sec_category_assessment(
    source: dict[str, Any], stored_categories: list[str]
) -> dict[str, dict[str, Any]]:
    """Limit SEC latest-series evidence to the category that the filing proves."""
    document_type = source["document_type"]
    if (
        document_type in {"annual", "quarterly"}
        and "financial_report" in stored_categories
    ):
        return {
            "financial_report": {
                "scope": "sec_financial_filing_series",
                "target_status": "unknown",
            }
        }
    if document_type == "proxy" and "social_employee" in stored_categories:
        return {
            "social_employee": {
                "scope": "sec_proxy_employee_disclosure",
                "target_status": "unknown",
            }
        }
    return {}


def build_sec_source_review(
    source: dict[str, Any],
    stored_categories: list[str],
    document_sha256: str,
    submissions_listing: dict[str, Any],
    previous_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one seven-day review without replacing curated notes or assessments."""
    previous_review = previous_review if isinstance(previous_review, dict) else {}
    previous_notes = previous_review.get("notes")
    if not isinstance(previous_notes, list):
        previous_notes = []
    notes = [
        note
        for note in previous_notes
        if isinstance(note, str) and not note.startswith(SEC_LISTING_NOTE_PREFIX)
    ]
    notes.append(SEC_LISTING_NOTE_PREFIX + submissions_listing["sha256"])
    assessments = previous_review.get("category_assessments")
    assessments = dict(assessments) if isinstance(assessments, dict) else {}
    assessments.update(_sec_category_assessment(source, stored_categories))
    checked_at = datetime.fromisoformat(submissions_listing["checked_at"])
    report_date = source.get("report_date")
    review = {
        "status": "latest_verified",
        "document_sha256": document_sha256,
        "checked_at": checked_at.isoformat(),
        "valid_until": (checked_at + timedelta(days=7)).isoformat(),
        "report_family": REPORT_FAMILIES[source["document_type"]],
        "scope": "sec_current_submissions_form_group",
        "evidence_urls": [submissions_listing["evidence_url"]],
        "publication_date": source["filed_date"],
        "report_period": (
            {"start": None, "end": report_date, "label": None} if report_date else None
        ),
        "category_assessments": assessments,
        "notes": notes,
    }
    return validate_source_review(review)


def _write_sec_report_reviews(
    settings,
    task_id: int,
    company_cik: str,
    reports: list[tuple[dict[str, Any], dict[str, Any]]],
    submissions_listing: dict[str, Any],
) -> None:
    """Publish hash-bound reviews while preserving existing source descriptions."""
    if not reports:
        return
    with db.connect(settings) as connection:
        db.require_active_task(connection, task_id)
        for source, document in reports:
            row = connection.execute(
                """SELECT categories,review FROM report_sources
                WHERE company_cik=%s AND url=%s FOR UPDATE""",
                (company_cik, source["url"]),
            ).fetchone()
            stored_categories = row["categories"] if row else source["categories"]
            previous_review = row["review"] if row else {}
            review = build_sec_source_review(
                source,
                stored_categories,
                document["sha256"],
                submissions_listing,
                previous_review,
            )
            if row:
                connection.execute(
                    """UPDATE report_sources SET review=%s,updated_at=now()
                    WHERE company_cik=%s AND url=%s""",
                    (Jsonb(review), company_cik, source["url"]),
                )
            else:
                connection.execute(
                    """INSERT INTO report_sources
                    (company_cik,url,title,categories,reporting_period,discovered_via,evidence,review)
                    VALUES (%s,%s,%s,%s,%s,'sec_submissions',%s,%s)""",
                    (
                        company_cik,
                        source["url"],
                        source["title"],
                        source["categories"],
                        source["reporting_period"],
                        Jsonb(source["evidence"]),
                        Jsonb(review),
                    ),
                )


def _document_identity(settings, document_id: int) -> dict[str, Any]:
    """Return the exact stored version used by a newly downloaded report review."""
    with db.connect(settings) as connection:
        row = connection.execute(
            "SELECT id AS document_id,sha256 FROM documents WHERE id=%s",
            (document_id,),
        ).fetchone()
    if not row:
        raise ValueError("Saved SEC report document no longer exists.")
    return row


class SecReportPipeline:
    """Persist pending sources, original bytes and explicit failures off the event loop."""

    def process_item(self, item, spider):
        """Await one report-intake operation before accepting the item."""
        from twisted.internet.threads import deferToThread

        return deferToThread(self.persist, item, spider)

    def persist(self, item, spider):
        """Call the shared intake contract and update bounded task counters."""
        from green500 import report_intake

        company_cik = item["company_cik"]
        if item["kind"] == "report":
            document_id = report_intake.save_report(
                spider.settings_value,
                spider.task_id,
                company_cik,
                item["source"],
                item["body"],
                item["content_type"],
                item["final_url"],
            )
            document = _document_identity(spider.settings_value, document_id)
            _write_sec_report_reviews(
                spider.settings_value,
                spider.task_id,
                company_cik,
                [(item["source"], document)],
                item["source"]["submissions_listing"],
            )
            spider.record(company_cik, "downloaded")
        else:
            report_intake.record_report_failure(
                spider.settings_value,
                spider.task_id,
                company_cik,
                item["url"],
                item["error"][:1000],
            )
            spider.record(company_cik, "failed", item["error"][:1000])
        return item


class SecReportSpider(scrapy.Spider):
    """Fetch one submissions index and at most three current reports per company."""

    name = "green500_sec_reports"
    allowed_domains = ("data.sec.gov", "www.sec.gov")

    def __init__(self, task, company_ciks, settings_value, **kwargs):
        """Bind the fixed CIK list and per-company outcome records."""
        super().__init__(**kwargs)
        self.task_id = task.get("id")
        self.company_ciks = company_ciks
        self.settings_value = settings_value
        self.lock = threading.Lock()
        self.results = {
            cik: {
                "company_cik": cik,
                "submissions_status": "pending",
                "selected_report_count": 0,
                "pending_registered_count": 0,
                "downloaded_count": 0,
                "reused_document_count": 0,
                "failed_count": 0,
                "missing_forms": [],
                "errors": [],
                "submissions_document_id": None,
                "submissions_sha256": None,
            }
            for cik in company_ciks
        }

    async def start(self):
        """Start with the SEC's stable per-company current-submissions endpoint."""
        for company_cik in self.company_ciks:
            url = SUBMISSIONS_URL.format(company_cik=company_cik)
            yield scrapy.Request(
                url,
                callback=self.parse_submissions,
                errback=self.request_failed,
                cb_kwargs={"expected_cik": company_cik},
                meta={"company_cik": company_cik, "source_url": url},
            )

    async def parse_submissions(self, response, expected_cik):
        """Select current sources and issue their bounded primary-document requests."""
        from scrapy.utils.defer import deferred_to_future
        from twisted.internet.threads import deferToThread

        from green500 import report_intake

        try:
            parsed = parse_latest_report_sources(response.body, expected_cik)
        except (TypeError, ValueError) as error:
            self.results[expected_cik]["submissions_status"] = "failed"
            yield self.failure_item(expected_cik, response.url, str(error))
            return
        company_result = self.results[expected_cik]
        submissions_url = SUBMISSIONS_URL.format(company_cik=expected_cik)
        content_type = response.headers.get("Content-Type", b"").decode("latin1")
        submissions_listing = await deferred_to_future(
            deferToThread(
                _save_submissions_listing,
                self.settings_value,
                self.task_id,
                expected_cik,
                parsed["entity_name"],
                submissions_url,
                response.url,
                response.body,
                content_type,
            )
        )
        company_result["submissions_status"] = "succeeded"
        company_result["submissions_document_id"] = submissions_listing["document_id"]
        company_result["submissions_sha256"] = submissions_listing["sha256"]
        company_result["selected_report_count"] = len(parsed["sources"])
        company_result["missing_forms"] = parsed["missing_forms"]
        reusable_by_url = await deferred_to_future(
            deferToThread(
                _find_reusable_reports,
                self.settings_value,
                expected_cik,
                parsed["sources"],
            )
        )
        reused, downloads = partition_report_sources_for_resume(
            parsed["sources"], reusable_by_url
        )
        await deferred_to_future(
            deferToThread(
                _write_sec_report_reviews,
                self.settings_value,
                self.task_id,
                expected_cik,
                reused,
                submissions_listing,
            )
        )
        for _source, _existing in reused:
            self.record(expected_cik, "reused_document")
        for source in downloads:
            source["submissions_listing"] = submissions_listing
            pending_source = {
                key: source[key]
                for key in (
                    "url",
                    "title",
                    "categories",
                    "reporting_period",
                    "evidence",
                )
            }
            await deferred_to_future(
                deferToThread(
                    report_intake.register_pending_report,
                    self.settings_value,
                    self.task_id,
                    expected_cik,
                    pending_source,
                )
            )
            self.record(expected_cik, "pending_registered")
            yield scrapy.Request(
                source["url"],
                callback=self.parse_report,
                errback=self.request_failed,
                cb_kwargs={"source": source},
                meta={
                    "company_cik": expected_cik,
                    "source_url": source["url"],
                },
            )

    def parse_report(self, response, source):
        """Confirm proxy employee content and send the original to persistence."""
        stored_source = dict(source)
        stored_source["evidence"] = list(source["evidence"])
        if source["document_type"] == "proxy":
            matches = proxy_employee_content(response.body)
            if matches:
                stored_source["categories"] = ["social_employee"]
                stored_source["evidence"].append(
                    {
                        "section": "Primary proxy document content check",
                        "confirmed_topics": matches,
                    }
                )
            else:
                stored_source["title"] += " (employee content not confirmed)"
        content_type = response.headers.get("Content-Type", b"").decode("latin1")
        yield {
            "kind": "report",
            "company_cik": response.meta["company_cik"],
            "source": stored_source,
            "body": response.body,
            "content_type": content_type,
            "final_url": response.url,
        }

    def request_failed(self, failure):
        """Return one persistence item for an HTTP or download failure."""
        return self.failure_item(
            failure.request.meta["company_cik"],
            failure.request.meta["source_url"],
            failure.getErrorMessage(),
        )

    @staticmethod
    def failure_item(company_cik: str, url: str, error: str) -> dict[str, Any]:
        """Build one plain failure item without inventing report content."""
        return {
            "kind": "failure",
            "company_cik": company_cik,
            "url": url,
            "error": error,
        }

    def record(self, company_cik: str, outcome: str, error: str | None = None) -> None:
        """Update one company's counters after its intake write completes."""
        with self.lock:
            result = self.results[company_cik]
            result[outcome + "_count"] += 1
            if error:
                result["errors"].append(error)


def collect_sec_reports_task(task: dict) -> dict:
    """Collect current SEC originals in one rate-limited Scrapy process."""
    from scrapy.crawler import CrawlerProcess

    settings = load_settings()
    company_ciks = _requested_ciks(settings, task)
    process = CrawlerProcess(
        {
            "USER_AGENT": SEC_USER_AGENT,
            "DEFAULT_REQUEST_HEADERS": {
                "Accept": "application/json,text/html,application/xhtml+xml,application/pdf",
                "Accept-Encoding": "gzip, deflate",
            },
            "ROBOTSTXT_OBEY": True,
            "CONCURRENT_REQUESTS": 2,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "DOWNLOAD_DELAY": 0.75,
            "RANDOMIZE_DOWNLOAD_DELAY": False,
            "DOWNLOAD_TIMEOUT": 45,
            "DOWNLOAD_MAXSIZE": settings.max_document_bytes,
            "DOWNLOAD_WARNSIZE": settings.max_document_bytes,
            "DOWNLOAD_FAIL_ON_DATALOSS": True,
            "RETRY_TIMES": 1,
            "REDIRECT_MAX_TIMES": 3,
            "LOG_LEVEL": "WARNING",
            "TELNETCONSOLE_ENABLED": False,
            "ITEM_PIPELINES": {"green500.sec_reports.SecReportPipeline": 100},
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        }
    )
    crawler = process.create_crawler(SecReportSpider)
    process.crawl(
        crawler,
        task=task,
        company_ciks=company_ciks,
        settings_value=settings,
    )
    process.start()
    spider = crawler.spider
    if spider is None:
        raise RuntimeError("SEC report collection did not start.")
    stats = crawler.stats.get_stats()
    if stats.get("spider_exceptions/count", 0) or stats.get("item_error_count", 0):
        raise RuntimeError(
            "SEC report collection had an unrecorded processing failure."
        )
    company_results = list(spider.results.values())
    for result in company_results:
        result["completed_report_count"] = (
            result["downloaded_count"] + result["reused_document_count"]
        )
        result["is_partial"] = bool(
            result["submissions_status"] == "failed"
            or result["errors"]
            or result["completed_report_count"] != result["selected_report_count"]
        )
    failed_ciks = [
        result["company_cik"]
        for result in company_results
        if result["submissions_status"] == "failed" or result["failed_count"]
    ]
    output = {
        "requested_company_count": len(company_ciks),
        "request_count": stats.get("downloader/request_count", 0),
        "submissions_succeeded_count": sum(
            result["submissions_status"] == "succeeded" for result in company_results
        ),
        "selected_report_count": sum(
            result["selected_report_count"] for result in company_results
        ),
        "pending_registered_count": sum(
            result["pending_registered_count"] for result in company_results
        ),
        "downloaded_report_count": sum(
            result["downloaded_count"] for result in company_results
        ),
        "reused_document_count": sum(
            result["reused_document_count"] for result in company_results
        ),
        "completed_report_count": sum(
            result["completed_report_count"] for result in company_results
        ),
        "report_failure_count": sum(
            result["failed_count"] for result in company_results
        ),
        "missing_form_count": sum(
            len(result["missing_forms"]) for result in company_results
        ),
        "failed_ciks": failed_ciks,
        "companies": company_results,
    }
    output["is_partial"] = bool(
        failed_ciks
        or output["completed_report_count"] != output["selected_report_count"]
    )
    return output
