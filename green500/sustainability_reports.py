"""Discover current sustainability reports from verified issuer websites."""

from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from urllib.parse import quote_plus, urlsplit

import scrapy
import tldextract
from pypdf import PdfReader
from scrapy.crawler import CrawlerProcess
from twisted.internet.threads import deferToThread

from green500 import db
from green500.config import load_settings
from green500.q4_sources import (
    parse_q4_content_asset_response,
    q4_content_asset_widgets,
)
from green500.storage import read_bytes, validate_public_url
from green500.target_sources import normalize_company_name

DIRECTORY_INDEX_URL = "https://www.responsibilityreports.com/Companies"
DIRECTORY_SEARCH_URL = "https://www.responsibilityreports.com/Companies?search={}"
MAX_ISSUER_HTML_PAGES = 6
ISSUER_DOMAIN_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=(),cache_dir=None,include_psl_private_domains=True)
SHARED_WEBSITE_HOSTS = {"q4web.com","q4cdn.com","q4ir.com","cloudfront.net","sharepoint.com","force.com"}
MAX_REPORT_PDFS = 2

CATEGORY_KEYS = (
    "financial_report",
    "environment_report",
    "social_employee",
    "financial_targets",
    "climate_targets",
)
REPORT_TOPIC_PATTERN = re.compile(
    r"sustainab|\besg\b|responsib|environment|climate|human[\s_-]*rights|"
    r"social|workforce|employee|people|diversity|impact",
    re.IGNORECASE,
)
REPORT_DOCUMENT_PATTERN = re.compile(
    r"report|update|statement|disclosure|facts?heet|data|download|pdf",
    re.IGNORECASE,
)
YEAR_PATTERN = re.compile(r"\b(20[1-3][0-9])\b")

ENVIRONMENT_EVIDENCE = (
    re.compile(r"\b(?:scope\s*[123]|greenhouse gas|ghg)\b", re.IGNORECASE),
    re.compile(r"\b(?:energy|water|waste|emissions?|environmental)\b", re.IGNORECASE),
)
SOCIAL_EVIDENCE = (
    re.compile(
        r"\b(?:employees?|workforce|workers?|labor|human rights)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(?:health and safety|wellbeing|well-being|diversity|inclusion|"
        r"training|workplace rights|collective bargaining)\b",
        re.IGNORECASE,
    ),
)
CLIMATE_TARGET_PATTERN = re.compile(
    r"(?:carbon neutral|net[ -]?zero|carbon negative|reduce.{0,80}emissions?|"
    r"science[ -]?based target).{0,100}\bby\s+20[2-5][0-9]\b",
    re.IGNORECASE | re.DOTALL,
)
REPORTING_PERIOD_PATTERN = re.compile(
    r"\b((?:fiscal|calendar|reporting) year(?: ended)?\s+(?:20[1-3][0-9]|"
    r"[A-Z][a-z]+\s+\d{1,2},\s+20[1-3][0-9])|FY\s?20[1-3][0-9])\b",
    re.IGNORECASE,
)


def _normalized_ticker(value: str) -> str:
    """Compare exchange tickers while ignoring display punctuation."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def directory_entries(response) -> list[dict]:
    """Read every named company link from the public directory index."""
    entries = []
    seen_urls = set()
    for link in response.css("span.companyName a"):
        name = link.xpath("normalize-space(.)").get() or ""
        url = response.urljoin(link.attrib.get("href", ""))
        if not name or url in seen_urls:
            continue
        seen_urls.add(url)
        entries.append(
            {
                "company_name": name,
                "normalized_name": normalize_company_name(name),
                "profile_url": url,
            }
        )
    if not entries:
        raise ValueError("Responsibility Reports returned no company directory entries.")
    return entries


def match_directory_entries(
    current_companies: list[dict], entries: list[dict]
) -> tuple[dict[str, dict], list[dict]]:
    """Use only names that are unique in both the current index and directory."""
    directory_by_name = defaultdict(list)
    current_by_name = defaultdict(list)
    for entry in entries:
        directory_by_name[entry["normalized_name"]].append(entry)
    for company in current_companies:
        current_by_name[normalize_company_name(company["name"])].append(company)

    matched = {}
    unmatched = []
    for company in current_companies:
        normalized_name = normalize_company_name(company["name"])
        candidates = directory_by_name.get(normalized_name, [])
        if len(candidates) == 1 and len(current_by_name[normalized_name]) == 1:
            matched[company["cik"]] = candidates[0]
        else:
            unmatched.append(company)
    return matched, unmatched


def parse_directory_profile(response) -> dict:
    """Read provider ticker, company name and official website from JSON-LD."""
    profile = None
    for raw_value in response.css('script[type="application/ld+json"]::text').getall():
        try:
            candidate = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        if candidate.get("@type") == "Corporation":
            profile = candidate
            break
    if not profile:
        raise ValueError("Directory company page has no Corporation JSON-LD profile.")
    ticker = response.css("span.ticker_name::text").get()
    official_url = profile.get("url")
    if not ticker or not official_url:
        raise ValueError("Directory company page is missing ticker or official website.")
    return {
        "ticker": ticker.strip(),
        "company_name": str(profile.get("name") or "").strip(),
        "official_url": validate_public_url(str(official_url)),
        "profile_url": response.url,
        "identity_provider": "ResponsibilityReports.com",
        "discovered_via": "issuer_site_via_responsibilityreports_directory",
    }


def issuer_website_seeds(task: dict, current_companies: list[dict]) -> tuple[bool, dict[str, dict]]:
    """Validate explicit reviewed issuer websites without using the directory."""
    task_input = task.get("input") or {}
    if "issuer_websites" not in task_input:
        return False, {}
    raw_seeds = task_input["issuer_websites"]
    if isinstance(raw_seeds, dict):
        records = []
        for key, value in raw_seeds.items():
            if not isinstance(value, dict):
                raise TypeError("Every issuer website seed must be an object.")
            records.append({**value, "company_cik": key})
    elif isinstance(raw_seeds, list):
        records = raw_seeds
    else:
        raise TypeError("issuer_websites must be a list or a CIK-to-record object.")
    companies_by_cik = {company["cik"]: company for company in current_companies}
    seeds = {}
    for record in records:
        if not isinstance(record, dict):
            raise TypeError("Every issuer website seed must be an object.")
        company_cik = str(record.get("company_cik") or "")
        if company_cik not in companies_by_cik or company_cik in seeds:
            raise ValueError("Issuer website seed has an unknown or duplicate current company CIK.")
        source_document_id = record.get("source_document_id")
        evidence_context = str(record.get("evidence_context") or "").strip()
        confidence = str(record.get("confidence") or "").strip()
        identity_provider = str(record.get("identity_provider") or "SEC filing")
        if identity_provider not in {"SEC filing", "Reviewed official issuer hub"}:
            raise ValueError(
                "Issuer website seed identity_provider must be SEC filing or "
                "Reviewed official issuer hub."
            )
        if not isinstance(source_document_id, int) or source_document_id <= 0 or not evidence_context or not confidence:
            raise ValueError("Issuer website seed requires source_document_id, evidence_context, and confidence.")
        company = companies_by_cik[company_cik]
        official_url = str(record.get("url") or "")
        parsed_url = urlsplit(official_url)
        if parsed_url.scheme not in {"http","https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.port not in {None,80,443}:
            raise ValueError("Issuer website seed requires a plain public HTTP(S) URL.")
        # DNS and private-address checks belong to each request's middleware;
        # one unreachable company must not abort the entire seed batch.
        seeds[company_cik] = {
            "ticker": min(company.get("symbols") or [""]),
            "company_name": company["name"],
            "official_url": official_url,
            "profile_url": f"document:{source_document_id}",
            "identity_provider": identity_provider,
            "discovered_via": (
                "issuer_site_via_reviewed_official_hub"
                if identity_provider == "Reviewed official issuer hub"
                else "issuer_site_via_sec_filing"
            ),
            "source_document_id": source_document_id,
            "evidence_context": evidence_context,
            "confidence": confidence,
        }
    return True, seeds


def _issuer_domain(hostname: str | None) -> str:
    """Keep issuer subdomains together without following unrelated HTML hosts."""
    hostname = (hostname or "").casefold().strip(".")
    domain = ISSUER_DOMAIN_EXTRACTOR(hostname).top_domain_under_public_suffix
    if domain and domain not in SHARED_WEBSITE_HOSTS:
        return domain
    return hostname.removeprefix("www.")


def _same_issuer_host(url: str, issuer_domain: str) -> bool:
    hostname = (urlsplit(url).hostname or "").casefold()
    return hostname == issuer_domain or hostname.endswith("." + issuer_domain)


def _link_text(link) -> str:
    return " ".join(
        filter(
            None,
            [
                link.xpath("normalize-space(.)").get(),
                link.attrib.get("title"),
                link.attrib.get("aria-label"),
            ],
        )
    ).strip()


def _report_topics(description: str) -> set[str]:
    """Use explicit link language to reserve environmental and social report slots."""
    lowered = description.casefold()
    topics = set()
    if re.search(r"sustainab|\besg\b|responsib|environment|climate", lowered):
        topics.add("environment")
    if re.search(
        r"sustainab|\besg\b|responsib|human[\s_-]*rights|social|workforce|"
        r"employee|people|diversity|impact",
        lowered,
    ):
        topics.add("social")
    return topics


def relevant_issuer_links(
    response, issuer_domain: str
) -> tuple[list[dict], list[dict]]:
    """Select bounded same-issuer topic pages and report-shaped PDF links."""
    html_links = []
    pdf_links = []
    seen = set()
    current_page_is_relevant = bool(REPORT_TOPIC_PATTERN.search(response.url))
    page_title = (
        response.css("title::text").get()
        or response.css("h1").xpath("normalize-space(.)").get()
        or response.url
    )
    for link in response.css("a[href]"):
        url = response.urljoin(link.attrib["href"]).split("#", 1)[0]
        if url in seen:
            continue
        seen.add(url)
        try:
            url = validate_public_url(url)
        except (OSError, ValueError):
            continue
        description = " ".join((_link_text(link), url))
        path = urlsplit(url).path.casefold()
        same_issuer = _same_issuer_host(url, issuer_domain)
        explicit_report = bool(
            REPORT_TOPIC_PATTERN.search(description)
            and REPORT_DOCUMENT_PATTERN.search(description)
        )
        is_pdf_candidate = path.endswith(".pdf") or (
            not same_issuer and explicit_report
        )
        if is_pdf_candidate:
            candidate_description = (
                description + " " + page_title
                if current_page_is_relevant
                else description
            )
            if REPORT_TOPIC_PATTERN.search(candidate_description) and (
                REPORT_DOCUMENT_PATTERN.search(description) or current_page_is_relevant
            ):
                years = [
                    int(value) for value in YEAR_PATTERN.findall(candidate_description)
                ]
                link_title = _link_text(link)
                title = (
                    link_title
                    if REPORT_TOPIC_PATTERN.search(link_title)
                    else f"{page_title} {link_title}".strip()
                )
                pdf_links.append(
                    {
                        "url": url,
                        "title": title or path.rsplit("/", 1)[-1],
                        "year": max(years) if years else None,
                        "topics": sorted(_report_topics(candidate_description)),
                        "discovered_on": response.url,
                    }
                )
            continue
        if (
            same_issuer
            and REPORT_TOPIC_PATTERN.search(description)
        ):
            years = [int(value) for value in YEAR_PATTERN.findall(description)]
            html_links.append(
                {
                    "url": url,
                    "score": (max(years) if years else 0),
                }
            )
    html_links.sort(key=lambda item: (-item["score"], item["url"]))
    pdf_links.sort(
        key=lambda item: (-(item["year"] or 0), item["title"].casefold(), item["url"])
    )
    return html_links, pdf_links


def _extract_report_pages(body: bytes) -> list[tuple[int, str]]:
    """Extract a bounded set of PDF pages for deterministic category evidence."""
    if not body.lstrip().startswith(b"%PDF-"):
        raise ValueError("Discovered report response is not a PDF file.")
    reader = PdfReader(BytesIO(body), strict=False)
    if reader.is_encrypted:
        raise ValueError("Discovered report PDF is encrypted.")
    page_indexes = list(range(min(40, len(reader.pages))))
    if len(reader.pages) > 40:
        page_indexes.extend(range(max(40, len(reader.pages) - 10), len(reader.pages)))
    return [
        (index + 1, reader.pages[index].extract_text() or "")
        for index in sorted(set(page_indexes))
    ]


def _first_evidence(
    pages: list[tuple[int, str]], patterns: tuple[re.Pattern, ...]
) -> dict | None:
    """Return one short source line only when every required pattern is present."""
    for page_number, text in pages:
        normalized = " ".join(text.split())
        if all(pattern.search(normalized) for pattern in patterns):
            matches = [pattern.search(normalized) for pattern in patterns]
            start = max(0, min(match.start() for match in matches if match) - 80)
            end = min(
                len(normalized), max(match.end() for match in matches if match) + 180
            )
            return {"page": page_number, "text": normalized[start:end]}
    return None


def classify_report_pages(pages: list[tuple[int, str]], title: str) -> dict:
    """Assign categories only when link description and PDF text both support them."""
    topics = _report_topics(title)
    environment = (
        _first_evidence(pages, ENVIRONMENT_EVIDENCE)
        if "environment" in topics
        else None
    )
    social = _first_evidence(pages, SOCIAL_EVIDENCE) if "social" in topics else None
    climate = None
    if "environment" in topics:
        for page_number, text in pages:
            match = CLIMATE_TARGET_PATTERN.search(" ".join(text.split()))
            if match:
                climate = {"page": page_number, "text": match.group(0)[:360]}
                break
    categories = []
    if environment:
        categories.append("environment_report")
    if social:
        categories.append("social_employee")
    if climate:
        categories.append("climate_targets")
    evidence = []
    for category, value in (
        ("environment_report", environment),
        ("social_employee", social),
        ("climate_targets", climate),
    ):
        if value:
            evidence.append({"category": category, **value})
    reporting_period = None
    for _, text in pages[:10]:
        matched = REPORTING_PERIOD_PATTERN.search(" ".join(text.split()))
        if matched:
            reporting_period = matched.group(1)
            break
    return {
        "categories": categories,
        "evidence": evidence,
        "reporting_period": reporting_period,
    }


def _resume_since(task: dict) -> datetime | None:
    """Parse an explicit timezone-aware acquisition cutoff without inventing a default."""
    raw_value = (task.get("input") or {}).get("resume_since")
    if raw_value in (None, ""):
        return None
    if not isinstance(raw_value, str):
        raise TypeError("resume_since must be an ISO 8601 timestamp string.")
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as error:
        raise ValueError("resume_since must be a valid ISO 8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise ValueError("resume_since must include a timezone.")
    return parsed


def _load_reusable_reports(settings, companies: list[dict], since: datetime | None):
    """Verify recent immutable bytes and index their source and final URLs without writes."""
    if since is None:
        return {}
    company_ciks = [company["cik"] for company in companies]
    with db.connect(settings) as conn:
        rows = conn.execute(
            """SELECT s.id AS report_source_id,s.company_cik,s.url AS source_url,
            s.title,s.categories,s.reporting_period,d.id AS document_id,d.url AS document_url,
            d.final_url,d.sha256,d.byte_count,d.fetched_at
            FROM report_sources s
            JOIN LATERAL (
                SELECT id,url,final_url,sha256,byte_count,fetched_at
                FROM documents d
                WHERE d.company_cik=s.company_cik AND d.kind='report'
                  AND (d.url=s.url OR d.final_url=s.url)
                  AND d.fetched_at >= %s
                ORDER BY d.fetched_at DESC,d.id DESC LIMIT 1
            ) d ON true
            WHERE s.company_cik=ANY(%s)""",
            (since, company_ciks),
        ).fetchall()
    reusable = defaultdict(dict)
    for row in rows:
        try:
            body = read_bytes(settings.data_dir, row["sha256"])
        except (OSError, ValueError):
            continue
        if len(body) != row["byte_count"]:
            continue
        receipt = {
            "report_source_id": row["report_source_id"],
            "document_id": row["document_id"],
            "source_url": row["source_url"],
            "final_url": row["final_url"],
            "title": row["title"],
            "categories": list(row["categories"]),
            "reporting_period": row["reporting_period"],
            "sha256": row["sha256"],
            "byte_count": row["byte_count"],
            "fetched_at": row["fetched_at"].isoformat(),
        }
        for url in {row["source_url"], row["document_url"], row["final_url"]}:
            if url:
                reusable[row["company_cik"]][url] = receipt
    return {company_cik: dict(by_url) for company_cik, by_url in reusable.items()}


def report_source_from_item(item: dict, classified: dict) -> dict:
    """Build report metadata with the exact provider that established issuer identity."""
    evidence = [
        {
            "category": "source_identity",
            "provider": item["identity_provider"],
            "profile_url": item["profile_url"],
            "provider_company_name": item["provider_company_name"],
            "provider_ticker": item["provider_ticker"],
            "official_website": item["official_website"],
            "source_document_id": item.get("source_document_id"),
            "evidence_context": item.get("identity_evidence_context"),
            "confidence": item.get("identity_confidence"),
            "discovered_on": item["discovered_on"],
        },
        *([item["discovery_evidence"]] if item.get("discovery_evidence") else []),
        *classified["evidence"],
    ]
    return {
        "url": item["source_url"],
        "title": item["title"],
        "categories": classified["categories"],
        "reporting_period": classified["reporting_period"],
        "evidence": evidence,
        "discovered_via": item["identity_discovered_via"],
    }


class SustainabilityReportPipeline:
    """Classify and persist each verified issuer PDF outside the event-loop thread."""

    def process_item(self, item, spider):
        return deferToThread(self.persist, item, spider)

    def persist(self, item, spider):
        from green500 import report_intake

        company_cik = item["company_cik"]
        try:
            classification_description = " ".join(
                [item["title"], *item.get("topic_hints", [])]
            )
            classified = classify_report_pages(
                _extract_report_pages(item["body"]), classification_description
            )
            if not classified["categories"]:
                raise ValueError(
                    "PDF text does not support an environmental, employee, or climate category."
                )
            source = report_source_from_item(item, classified)
            report_id = report_intake.save_report(
                spider.settings_value,
                spider.task["id"],
                company_cik,
                source,
                item["body"],
                item["content_type"],
                item["final_url"],
            )
            with spider.result_lock:
                spider.states[company_cik]["saved_report_ids"].append(report_id)
        except Exception as error:  # noqa: BLE001 - preserve any parser or persistence failure per company.
            with spider.result_lock:
                spider.states[company_cik]["download_failures"].append(
                    {"url": item["source_url"], "error": str(error)[:1000]}
                )
        return item


class SustainabilityReportsSpider(scrapy.Spider):
    """Bridge directory-verified tickers to bounded official-site report discovery."""

    name = "green500_sustainability_reports"

    def __init__(
        self, task, settings_value, companies, reusable_reports=None,
        seed_mode=False, website_seeds=None, **kwargs
    ):
        super().__init__(**kwargs)
        self.task = task
        self.settings_value = settings_value
        self.companies = companies
        self.reusable_reports = reusable_reports or {}
        self.seed_mode = seed_mode
        self.website_seeds = website_seeds or {}
        self.states = {
            company["cik"]: {
                "company": company,
                "profile": None,
                "visited_html": set(),
                "scheduled_pdfs": set(),
                "scheduled_topics": set(),
                "q4_api_urls": set(),
                "q4_api_failures": [],
                "q4_api_issues": [],
                "saved_report_ids": [],
                "reused_document_ids": set(),
                "reused_reports": [],
                "download_failures": [],
                "pending_candidates": [],
                "status": "awaiting_directory",
            }
            for company in companies
        }
        self.result_lock = threading.Lock()
        self.directory_failure = None

    async def start(self):
        if self.seed_mode:
            for company in self.companies:
                profile = self.website_seeds.get(company["cik"])
                if not profile:
                    self.states[company["cik"]]["status"] = "missing_official_website_seed"
                    continue
                state = self.states[company["cik"]]
                state["profile"] = profile
                state["status"] = "searching_official_website"
                yield self._issuer_request(company, profile)
            return
        yield scrapy.Request(
            DIRECTORY_INDEX_URL,
            callback=self.parse_directory,
            errback=self.record_directory_failure,
        )

    def parse_directory(self, response):
        """Match the global list once, then search by ticker only for name misses."""
        try:
            matched, unmatched = match_directory_entries(
                self.companies, directory_entries(response)
            )
        except (OSError, ValueError) as error:
            self.directory_failure = str(error)[:1000]
            return
        for company in self.companies:
            entry = matched.get(company["cik"])
            if entry:
                yield self._profile_request(company, entry["profile_url"])
        for company in unmatched:
            symbol = min(company.get("symbols") or [""])
            if not symbol:
                self.states[company["cik"]]["status"] = "missing_directory_identity"
                continue
            url = DIRECTORY_SEARCH_URL.format(quote_plus(symbol))
            yield scrapy.Request(
                url,
                callback=self.parse_directory_search,
                errback=self.record_company_failure,
                cb_kwargs={"company": company},
                meta={"company_cik": company["cik"], "purpose": "directory_search"},
                dont_filter=True,
            )

    def parse_directory_search(self, response, company):
        """Use a ticker search only to select a profile that will verify the ticker."""
        try:
            entries = directory_entries(response)
        except ValueError:
            self.states[company["cik"]]["status"] = "missing_directory_identity"
            return
        normalized_name = normalize_company_name(company["name"])
        named = [entry for entry in entries if entry["normalized_name"] == normalized_name]
        candidates = named if len(named) == 1 else entries
        if len(candidates) != 1:
            self.states[company["cik"]]["status"] = "ambiguous_directory_identity"
            return
        yield self._profile_request(company, candidates[0]["profile_url"])

    def _profile_request(self, company, url):
        return scrapy.Request(
            url,
            callback=self.parse_profile,
            errback=self.record_company_failure,
            cb_kwargs={"company": company},
            meta={"company_cik": company["cik"], "purpose": "directory_profile"},
            dont_filter=True,
        )

    def parse_profile(self, response, company):
        """Require the provider ticker before contacting the official website."""
        state = self.states[company["cik"]]
        try:
            profile = parse_directory_profile(response)
        except (OSError, ValueError) as error:
            state["status"] = "invalid_directory_profile"
            state["download_failures"].append(
                {"url": response.url, "error": str(error)[:1000]}
            )
            return
        expected_tickers = {
            _normalized_ticker(symbol) for symbol in company.get("symbols") or []
        }
        if _normalized_ticker(profile["ticker"]) not in expected_tickers:
            state["status"] = "directory_ticker_mismatch"
            return
        state["profile"] = profile
        state["status"] = "searching_official_website"
        yield self._issuer_request(company, profile)

    def _issuer_request(self, company, profile):
        """Start one bounded issuer crawl from either verified identity provider."""
        return scrapy.Request(
            profile["official_url"],
            callback=self.parse_issuer_page,
            errback=self.record_company_failure,
            meta={"company_cik": company["cik"], "purpose": "issuer_html"},
            dont_filter=True,
        )

    def parse_issuer_page(self, response):
        """Follow at most six relevant issuer pages and two latest report PDFs."""
        company_cik = response.meta["company_cik"]
        state = self.states[company_cik]
        state["visited_html"].add(response.url)
        issuer_domain = _issuer_domain(urlsplit(response.url).hostname)
        html_links, pdf_links = relevant_issuer_links(response, issuer_domain)
        widgets = q4_content_asset_widgets(response.text, response.url)
        if widgets:
            for widget in widgets:
                if widget["api_url"] in state["q4_api_urls"]:
                    continue
                state["q4_api_urls"].add(widget["api_url"])
                yield scrapy.Request(
                    widget["api_url"],
                    callback=self.parse_q4_content_assets,
                    errback=self.record_q4_failure,
                    cb_kwargs={"widget": widget, "static_candidates": pdf_links},
                    headers={"Accept": "application/json"},
                    meta={"company_cik": company_cik, "purpose": "q4_content_asset_api"},
                    dont_filter=True,
                    priority=100,
                )
        else:
            yield from self._schedule_report_candidates(company_cik, pdf_links)

        remaining_pages = MAX_ISSUER_HTML_PAGES - len(state["visited_html"])
        for candidate in html_links:
            if remaining_pages <= 0:
                break
            if candidate["url"] in state["visited_html"]:
                continue
            state["visited_html"].add(candidate["url"])
            remaining_pages -= 1
            yield scrapy.Request(
                candidate["url"],
                callback=self.parse_issuer_page,
                errback=self.record_company_failure,
                meta={"company_cik": company_cik, "purpose": "issuer_html"},
                dont_filter=True,
            )

    def _schedule_report_candidates(self, company_cik, candidates):
        """Reserve bounded topic slots and request candidate PDF bodies."""
        state = self.states[company_cik]
        for candidate in candidates:
            if len(state["scheduled_pdfs"]) >= MAX_REPORT_PDFS:
                break
            topics = set(candidate["topics"])
            if not topics or topics.issubset(state["scheduled_topics"]):
                continue
            if candidate["url"] in state["scheduled_pdfs"]:
                continue
            state["scheduled_pdfs"].add(candidate["url"])
            state["scheduled_topics"].update(topics)
            reusable = self.reusable_reports.get(company_cik, {}).get(
                candidate["url"]
            )
            if reusable:
                if reusable["document_id"] not in state["reused_document_ids"]:
                    state["reused_document_ids"].add(reusable["document_id"])
                    state["reused_reports"].append(reusable)
                state["status"] = "recent_report_reused"
                continue
            yield scrapy.Request(
                candidate["url"],
                callback=self.parse_report,
                errback=self.record_company_failure,
                cb_kwargs={"candidate": candidate},
                meta={"company_cik": company_cik, "purpose": "issuer_pdf"},
                dont_filter=True,
            )

    def _q4_fallback_candidates(self, candidates, widget, error):
        """Attach the API failure to static links used as a bounded fallback."""
        fallback = []
        for original in candidates:
            candidate = dict(original)
            candidate["discovery_evidence"] = {
                "category": "q4_content_asset_api_fallback",
                "text": "Q4 ContentAsset lookup failed; this static issuer-page link was used as fallback.",
                "source_page_url": widget["source_page_url"],
                "api_url": widget["api_url"],
                "api_error": error,
            }
            fallback.append(candidate)
        return fallback

    def parse_q4_content_assets(self, response, widget, static_candidates):
        """Prefer ranked publisher assets, falling back to visible static links."""
        company_cik = response.meta["company_cik"]
        state = self.states[company_cik]
        content_type = response.headers.get("Content-Type", b"").decode("latin1")
        try:
            candidates, issues = parse_q4_content_asset_response(
                response.body, content_type, widget
            )
        except ValueError as error:
            message = str(error)[:1000]
            state["q4_api_failures"].append(
                {"url": widget["api_url"], "error": message}
            )
            if not static_candidates:
                state["download_failures"].append(
                    {"url": widget["api_url"], "error": message}
                )
            yield from self._schedule_report_candidates(
                company_cik,
                self._q4_fallback_candidates(static_candidates, widget, message),
            )
            return
        state["q4_api_issues"].extend(issues)
        if candidates:
            yield from self._schedule_report_candidates(company_cik, candidates)
            return
        message = "Q4 ContentAsset response returned no valid relevant PDF rows."
        state["q4_api_failures"].append({"url": widget["api_url"], "error": message})
        if not static_candidates:
            state["download_failures"].append(
                {"url": widget["api_url"], "error": message}
            )
        yield from self._schedule_report_candidates(
            company_cik,
            self._q4_fallback_candidates(static_candidates, widget, message),
        )

    def record_q4_failure(self, failure):
        """Record transport failure and resume from links present in the issuer HTML."""
        company_cik = failure.request.meta["company_cik"]
        state = self.states[company_cik]
        widget = failure.request.cb_kwargs["widget"]
        static_candidates = failure.request.cb_kwargs["static_candidates"]
        error = failure.getErrorMessage()[:1000]
        state["q4_api_failures"].append({"url": widget["api_url"], "error": error})
        if not static_candidates:
            state["download_failures"].append(
                {"url": widget["api_url"], "error": error}
            )
        yield from self._schedule_report_candidates(
            company_cik,
            self._q4_fallback_candidates(static_candidates, widget, error),
        )

    def parse_report(self, response, candidate):
        """Send exact issuer bytes to deterministic classification and persistence."""
        company_cik = response.meta["company_cik"]
        state = self.states[company_cik]
        profile = state["profile"]
        state["status"] = "report_downloaded"
        yield {
            "company_cik": company_cik,
            "source_url": candidate["url"],
            "final_url": response.url,
            "title": candidate["title"],
            "body": response.body,
            "content_type": response.headers.get("Content-Type", b"").decode("latin1")
            or "application/pdf",
            "discovered_on": candidate["discovered_on"],
            "profile_url": profile["profile_url"],
            "provider_company_name": profile["company_name"],
            "provider_ticker": profile["ticker"],
            "official_website": profile["official_url"],
            "identity_provider": profile["identity_provider"],
            "identity_discovered_via": profile["discovered_via"],
            "source_document_id": profile.get("source_document_id"),
            "identity_evidence_context": profile.get("evidence_context"),
            "identity_confidence": profile.get("confidence"),
            "discovery_evidence": candidate.get("discovery_evidence"),
            "topic_hints": candidate.get("topics", []),
        }

    def record_directory_failure(self, failure):
        self.directory_failure = failure.getErrorMessage()[:1000]

    def record_company_failure(self, failure):
        company_cik = failure.request.meta.get("company_cik")
        if not company_cik:
            return
        state = self.states[company_cik]
        error = failure.getErrorMessage()[:1000]
        state["download_failures"].append(
            {"url": failure.request.url, "error": error}
        )
        if failure.request.meta.get("purpose") == "issuer_pdf":
            state["status"] = "report_download_failed"
            candidate = failure.request.cb_kwargs.get("candidate")
            if candidate:
                state["pending_candidates"].append(
                    {"candidate": candidate, "error": error}
                )


def _current_companies(settings, task: dict) -> list[dict]:
    company_cik = task.get("company_cik")
    company_ciks = (task.get("input") or {}).get("ciks")
    if company_ciks is not None and (not isinstance(company_ciks,list) or not company_ciks or any(not isinstance(cik,str) or not re.fullmatch(r"[0-9]{10}",cik) for cik in company_ciks)):
        raise ValueError("ciks must be a nonempty list of ten-digit current company identifiers.")
    limit = min(500, max(1, int((task.get("input") or {}).get("limit", 500))))
    with db.connect(settings) as conn:
        return conn.execute(
            """SELECT cik,name,symbols FROM companies
            WHERE is_current AND (%s::text IS NULL OR cik=%s)
            AND (%s::text[] IS NULL OR cik=ANY(%s::text[]))
            ORDER BY name,cik LIMIT %s""",
            (company_cik, company_cik, company_ciks, company_ciks, limit),
        ).fetchall()


def _run_discovery(task, settings, companies, reusable_reports, seed_mode, website_seeds):
    process = CrawlerProcess(
        {
            "USER_AGENT": "Green500/0.1 (public environmental research)",
            "ROBOTSTXT_OBEY": True,
            "CONCURRENT_REQUESTS": 12,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
            "DOWNLOAD_DELAY": 0.75,
            "DOWNLOAD_TIMEOUT": 30,
            "DOWNLOAD_MAXSIZE": settings.max_document_bytes,
            "DOWNLOAD_WARNSIZE": settings.max_document_bytes,
            "DOWNLOAD_FAIL_ON_DATALOSS": True,
            "RETRY_TIMES": 1,
            "REDIRECT_MAX_TIMES": 5,
            "LOG_LEVEL": "WARNING",
            "TELNETCONSOLE_ENABLED": False,
            "DOWNLOADER_MIDDLEWARES": {
                "green500.crawl.PublicSourceMiddleware": 50
            },
            "ITEM_PIPELINES": {
                "green500.sustainability_reports.SustainabilityReportPipeline": 100
            },
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        }
    )
    crawler = process.create_crawler(SustainabilityReportsSpider)
    process.crawl(
        crawler,
        task=task,
        settings_value=settings,
        companies=companies,
        reusable_reports=reusable_reports,
        seed_mode=seed_mode,
        website_seeds=website_seeds,
    )
    process.start()
    if not crawler.spider:
        raise RuntimeError("Sustainability report discovery did not start.")
    return crawler.spider


def _pending_source(state: dict, pending: dict) -> dict:
    profile = state.get("profile") or {}
    candidate = pending["candidate"]
    categories = []
    if "environment" in candidate["topics"]:
        categories.append("environment_report")
    if "social" in candidate["topics"]:
        categories.append("social_employee")
    return {
        "url": candidate["url"],
        "title": candidate["title"],
        "categories": categories,
        "reporting_period": None,
        "discovered_via": profile.get("discovered_via", "issuer_site_discovery"),
        "evidence": [
            {
                "category": "candidate_only",
                "text": "Issuer-linked report candidate could not be downloaded or verified.",
                "identity_provider": profile.get("identity_provider"),
                "source_document_id": profile.get("source_document_id"),
                "evidence_context": profile.get("evidence_context"),
                "confidence": profile.get("confidence"),
                "discovered_on": candidate["discovered_on"],
                "download_error": pending["error"],
            },
            *(
                [candidate["discovery_evidence"]]
                if candidate.get("discovery_evidence")
                else []
            ),
        ],
        "listing": {
            "provider": profile.get("identity_provider"),
            "profile_url": profile.get("profile_url"),
            "provider_company_name": profile.get("company_name"),
            "provider_ticker": profile.get("ticker"),
            "official_website": profile.get("official_url"),
            "source_document_id": profile.get("source_document_id"),
            "evidence_context": profile.get("evidence_context"),
            "confidence": profile.get("confidence"),
        },
    }


def collect_sustainability_reports_task(task: dict) -> dict:
    """Attempt every selected company and retain downloaded, pending and missing outcomes."""
    from green500 import report_intake

    settings = load_settings()
    companies = _current_companies(settings, task)
    if not companies:
        raise ValueError("No current companies are available for report discovery.")
    resume_since = _resume_since(task)
    reusable_reports = _load_reusable_reports(settings, companies, resume_since)
    seed_mode, website_seeds = issuer_website_seeds(task, companies)
    spider = _run_discovery(
        task, settings, companies, reusable_reports, seed_mode, website_seeds
    )
    if spider.directory_failure:
        for company in companies:
            report_intake.record_report_failure(
                settings,
                task["id"],
                company["cik"],
                DIRECTORY_INDEX_URL,
                spider.directory_failure,
            )
        raise RuntimeError(
            "Responsibility Reports company identity directory failed: "
            + spider.directory_failure
        )

    downloaded = []
    reused = []
    pending = []
    missing = []
    q4_api_failures = []
    q4_api_issues = []
    for company in companies:
        state = spider.states[company["cik"]]
        if state["q4_api_failures"]:
            q4_api_failures.append(
                {"cik": company["cik"], "failures": state["q4_api_failures"]}
            )
        if state["q4_api_issues"]:
            q4_api_issues.append(
                {"cik": company["cik"], "issues": state["q4_api_issues"]}
            )
        has_report_outcome = False
        if state["reused_reports"]:
            reused.append(
                {
                    "cik": company["cik"],
                    "reports": state["reused_reports"],
                }
            )
            has_report_outcome = True
        if state["saved_report_ids"]:
            downloaded.append(
                {
                    "cik": company["cik"],
                    "report_ids": state["saved_report_ids"],
                }
            )
            has_report_outcome = True
        if state["pending_candidates"]:
            for pending_candidate in state["pending_candidates"][:MAX_REPORT_PDFS]:
                source = _pending_source(state, pending_candidate)
                pending_id = report_intake.register_pending_report(
                    settings,
                    task["id"],
                    company["cik"],
                    source,
                )
                pending.append(
                    {
                        "cik": company["cik"],
                        "pending_report_id": pending_id,
                        "url": source["url"],
                    }
                )
            has_report_outcome = True
        if has_report_outcome:
            continue
        if state["download_failures"]:
            failure = state["download_failures"][0]
            report_intake.record_report_failure(
                settings,
                task["id"],
                company["cik"],
                failure["url"],
                failure["error"],
            )
            missing.append(
                {
                    "cik": company["cik"],
                    "status": state["status"],
                    "error": failure["error"],
                }
            )
            continue
        error = {
            "missing_directory_identity": "No unique directory company identity was found.",
            "ambiguous_directory_identity": "Directory company identity was ambiguous.",
            "directory_ticker_mismatch": "Directory ticker did not match the current company.",
            "invalid_directory_profile": "Directory company profile was incomplete.",
            "missing_official_website_seed": "No verified official website seed was supplied.",
        }.get(state["status"], "No relevant current PDF was found on bounded issuer pages.")
        url = (
            (state.get("profile") or {}).get("official_url") or DIRECTORY_INDEX_URL
        )
        report_intake.record_report_failure(
            settings, task["id"], company["cik"], url, error
        )
        missing.append(
            {
                "cik": company["cik"],
                "status": state["status"],
                "error": error,
            }
        )

    return {
        "attempted_company_count": len(companies),
        "downloaded": downloaded,
        "reused": reused,
        "pending": pending,
        "missing": missing,
        "q4_api_failures": q4_api_failures,
        "q4_api_issues": q4_api_issues,
        "resume_since": resume_since.isoformat() if resume_since else None,
        "identity_source": (
            "explicit reviewed issuer website seeds"
            if seed_mode
            else "ResponsibilityReports.com directory"
        ),
        "directory_url": None if seed_mode else DIRECTORY_INDEX_URL,
        "directory_pdf_access": "not_used" if seed_mode else "not_used; robots.txt disallows /HostedData/*.pdf and /Click/",
    }
