"""Describe verified report sources and collected originals without exposing model records."""

from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from green500 import db
from green500.freshness import effective_source_review, validate_source_review
from green500.processed_financial import processing_summary
from green500.processed_reports import processing_summary as report_processing_summary
from green500.reference import attach_company_reference
from green500.score_catalog import empty_score, load_scores
from green500.storage import validate_public_url

CATEGORIES = [
    {"key": "financial_report", "label": "Financial reports"},
    {"key": "environment_report", "label": "Environmental reports"},
    {"key": "social_employee", "label": "Social & employees"},
    {"key": "financial_targets", "label": "Financial targets"},
    {"key": "climate_targets", "label": "Climate targets"},
]

SOURCE_QUERY = """SELECT s.id,s.company_cik,s.title,s.url,s.categories,s.reporting_period,
    s.discovered_via,s.evidence,s.review,d.id AS document_id,d.sha256,d.byte_count,d.content_type,
    d.last_checked_at,d.parse_status,d.acquisition_method,
    CASE WHEN s.download_status IS NOT NULL AND s.last_download_attempt_at>=COALESCE(t.created_at,'epoch'::timestamptz)
      THEN CASE WHEN s.download_status IN ('pending','running') AND b.status NOT IN ('pending','running') THEN 'unknown' ELSE s.download_status END
      ELSE t.status END AS task_status
    FROM report_sources s
    LEFT JOIN LATERAL (SELECT * FROM documents d
        WHERE d.company_cik=s.company_cik AND (d.url=s.url OR d.final_url=s.url)
        AND d.kind IN ('report','directory','feed') ORDER BY d.last_checked_at DESC,d.id DESC LIMIT 1) d ON true
    LEFT JOIN tasks b ON b.id=s.latest_download_task_id
    LEFT JOIN LATERAL (SELECT t.status,t.created_at FROM tasks t WHERE t.kind='collect_source'
        AND t.company_cik=s.company_cik AND t.input->>'url'=s.url ORDER BY t.id DESC LIMIT 1) t ON true
    WHERE (%s::text IS NULL OR s.company_cik=%s) ORDER BY s.company_cik,s.id"""


def register_report_source(settings, company_cik, source):
    """Save reviewed source categories independently of whether download has succeeded."""
    categories = source["categories"]
    if not categories or not set(categories).issubset(
        {item["key"] for item in CATEGORIES}
    ):
        raise ValueError("A report source requires supported, verified categories.")
    url = validate_public_url(source.get("final_url") or source["url"])
    has_review = "review" in source
    review = (
        validate_source_review(source["review"], check_public_urls=True)
        if has_review
        else {}
    )
    with db.connect(settings) as conn:
        row = conn.execute(
            """INSERT INTO report_sources(company_cik,url,title,categories,reporting_period,discovered_via,evidence,review)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(company_cik,url) DO UPDATE
            SET title=EXCLUDED.title,categories=EXCLUDED.categories,reporting_period=EXCLUDED.reporting_period,
            discovered_via=EXCLUDED.discovered_via,evidence=EXCLUDED.evidence,
            review=CASE WHEN %s THEN EXCLUDED.review ELSE report_sources.review END,
            updated_at=now() RETURNING id,url""",
            (
                company_cik,
                url,
                source["title"],
                categories,
                source.get("reporting_period"),
                source.get("discovered_via", "agent"),
                Jsonb(source.get("evidence", [])),
                Jsonb(review),
                has_review,
            ),
        ).fetchone()
    return row


def collection_status(source):
    """Keep refresh failures visible even when an earlier original remains available."""
    status = source.pop("task_status", None)
    source["collection_status"] = (
        status
        if status in {"pending", "running", "failed", "partial", "unknown", "cancelled"}
        else "available"
        if source["document_id"]
        else "not_downloaded"
    )
    return source


def describe_source(source, *, now=None):
    """Add collection and hash-bound freshness without changing the stored review evidence."""
    collection_status(source)
    source["review"] = source.get("review") or {}
    source["freshness"] = effective_source_review(
        source["review"], source.get("sha256"), now=now
    )
    return source


def category_freshness_status(source, category: str) -> str:
    """Apply a latest-source review only to categories covered by that review."""
    freshness = source["freshness"]
    if freshness["status"] == "unknown":
        return "unknown"
    assessments = freshness.get("category_assessments") or {}
    report_family = freshness.get("report_family") or ""
    if report_family.startswith("sec_"):
        allowed = {
            "sec_annual_filing": {"financial_report"},
            "sec_quarterly_filing": {"financial_report"},
            "sec_proxy_statement": {"social_employee"},
        }.get(report_family, set())
        if category not in allowed or category not in assessments:
            return "unknown"
    elif assessments and category not in assessments:
        return "unknown"
    assessment = assessments.get(category) or {}
    if (
        category in {"financial_targets", "climate_targets"}
        and assessment.get("target_status") != "current"
    ):
        return "unknown"
    return freshness["status"]


def company_catalog(settings):
    """Return one compact company row and collection progress for the report page."""
    with db.connect(settings) as conn:
        companies = conn.execute(
            'SELECT cik,name,symbols,sector FROM companies WHERE is_current ORDER BY LOWER(name) COLLATE "C",cik'
        ).fetchall()
        sources = [
            describe_source(row)
            for row in conn.execute(SOURCE_QUERY, (None, None)).fetchall()
        ]
        datasets = conn.execute(
            "SELECT company_cik,count(DISTINCT url) AS count,max(last_checked_at) AS last_checked_at FROM documents WHERE source_key='sec_companyfacts' GROUP BY company_cik"
        ).fetchall()
        target_checks = conn.execute(
            "SELECT company_cik,status,details,checked_at FROM source_status WHERE source_key='sbti'"
        ).fetchall()
        tasks = conn.execute(
            "SELECT id,kind,company_cik,status,created_at,started_at,finished_at FROM tasks ORDER BY id DESC LIMIT 30"
        ).fetchall()
        counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status,count(*) FROM tasks GROUP BY status"
            ).fetchall()
        }
        is_paused = conn.execute(
            "SELECT is_paused FROM controls WHERE id=1"
        ).fetchone()["is_paused"]
    attach_company_reference(companies)
    spglobal_scores, score_updated_at = load_scores()
    financial_processing = processing_summary(settings)
    report_processing = report_processing_summary(settings)
    rows = {
        company["cik"]: dict(
            company,
            spglobal_score=spglobal_scores.get(company["cik"], empty_score()),
            financial_processing=financial_processing.get(company["cik"], {"status":"not_processed","has_result":False}),
            report_processing=report_processing.get(company["cik"], {}),
            coverage={
                c["key"]: {
                    "available": 0,
                    "pending": 0,
                    "failed": 0,
                    "latest_verified": 0,
                    "newer_available": 0,
                    "unknown": 0,
                }
                for c in CATEGORIES
            },
            financial_data_count=0,
            sbti_match_status=None,
            last_checked_at=None,
            collection_status="not_downloaded",
            source_count=0,
        )
        for company in companies
    }
    available_documents = set()
    companies_with_reports = set()
    for dataset in datasets:
        if dataset["company_cik"] in rows:
            rows[dataset["company_cik"]].update(
                financial_data_count=dataset["count"],
                last_checked_at=dataset["last_checked_at"],
            )
    for check in target_checks:
        if check["company_cik"] in rows:
            rows[check["company_cik"]]["sbti_match_status"] = (
                check["details"].get("match_method", check["status"])
                if check["status"] == "succeeded"
                else check["status"]
            )
    for source in sources:
        if source["company_cik"] not in rows:
            continue
        row = rows[source["company_cik"]]
        row["source_count"] += 1
        status = source["collection_status"]
        if source["document_id"]:
            available_documents.add(source["document_id"])
            companies_with_reports.add(source["company_cik"])
        for category in source["categories"]:
            row["coverage"][category]["available"] += int(
                source["document_id"] is not None
            )
            row["coverage"][category]["failed"] += int(
                status in {"failed", "partial", "unknown", "cancelled"}
            )
            row["coverage"][category]["pending"] += int(
                status in {"pending", "running", "not_downloaded"}
            )
            if source["document_id"] is not None:
                freshness_status = category_freshness_status(source, category)
                row["coverage"][category][freshness_status] += 1
        priority = {
            "not_downloaded": 0,
            "available": 1,
            "cancelled": 2,
            "failed": 3,
            "unknown": 3,
            "partial": 3,
            "pending": 4,
            "running": 5,
        }
        if priority[status] > priority[row["collection_status"]]:
            row["collection_status"] = status
        if source["last_checked_at"] and (
            not row["last_checked_at"]
            or source["last_checked_at"] > row["last_checked_at"]
        ):
            row["last_checked_at"] = source["last_checked_at"]
    return {
        "companies": list(rows.values()),
        "categories": CATEGORIES,
        "tasks": tasks,
        "updated_at": datetime.now(UTC),
        "score_updated_at": score_updated_at,
        "summary": {
            "company_count": len(rows),
            "companies_with_reports": len(companies_with_reports),
            "available_reports": len(available_documents),
            "score_count": sum(
                row["spglobal_score"]["status"] == "available"
                and isinstance(row["spglobal_score"]["esg_score"], (int, float))
                and not isinstance(row["spglobal_score"]["esg_score"], bool)
                for row in rows.values()
            ),
            "ai_completed_companies": sum(row["financial_processing"]["status"] == "completed" for row in rows.values()),
            "ai_processing_companies": sum(row["financial_processing"]["status"] == "processing" for row in rows.values()),
            "ai_review_companies": sum(row["financial_processing"]["status"] == "needs_review" for row in rows.values()),
            "pending_tasks": counts.get("pending", 0),
            "running_tasks": counts.get("running", 0),
            "failed_tasks": counts.get("failed", 0),
            "is_paused": is_paused,
        },
    }


def catalog_detail(settings, cik):
    """Return reviewed reports and provider datasets with original-file references."""
    with db.connect(settings) as conn:
        company = conn.execute(
            "SELECT cik,name,symbols,sector FROM companies WHERE cik=%s", (cik,)
        ).fetchone()
        sources = [
            describe_source(row)
            for row in conn.execute(SOURCE_QUERY, (cik, cik)).fetchall()
        ]
        datasets = conn.execute(
            """SELECT DISTINCT d.id,d.title,d.url,d.source_key,d.sha256,d.byte_count,d.content_type,d.last_checked_at,d.acquisition_method
            FROM documents d WHERE (d.company_cik=%s AND d.source_key='sec_companyfacts')
            OR (d.source_key='sbti' AND EXISTS(SELECT 1 FROM latest_observations o WHERE o.company_cik=%s AND o.dataset_id=d.id))
            ORDER BY d.id DESC""",
            (cik, cik),
        ).fetchall()
        checks = conn.execute(
            "SELECT source_key,status,checked_at FROM source_status WHERE company_cik=%s",
            (cik,),
        ).fetchall()
    if company:
        attach_company_reference([company])
    return {
        "sources": sources,
        "datasets": datasets,
        "source_checks": checks,
        "classification": company.get("classification") if company else None,
    }


def catalog_document(settings, document_id):
    """Allow viewer access to catalog originals only, excluding prompts and model receipts."""
    with db.connect(settings) as conn:
        return conn.execute(
            """SELECT d.* FROM documents d WHERE d.id=%s AND
            ((d.kind='dataset' AND d.source_key IN ('sec_companyfacts','sbti')) OR
            EXISTS(SELECT 1 FROM report_sources s WHERE s.company_cik=d.company_cik AND (s.url=d.url OR s.url=d.final_url)))""",
            (document_id,),
        ).fetchone()
