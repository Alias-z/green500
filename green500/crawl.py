"""Thin Scrapy collectors for the index, report pages and source feeds."""

import json
import re
from urllib.parse import urlsplit

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import IgnoreRequest
from scrapy.http import TextResponse
from twisted.internet.threads import deferToThread

from green500 import db
from green500.config import load_settings
from green500.storage import save_bytes, validate_public_url

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def parse_constituents(response) -> list[dict]:
    """Read the complete named table; reject malformed rows and ambiguous identifiers."""
    table = response.css("table#constituents")
    if len(table) != 1:
        raise ValueError("Expected one complete constituents table.")
    headers = [
        cell.xpath("normalize-space(.)").get() for cell in table.css("tr")[0].css("th")
    ]
    required = {"Symbol", "Security", "CIK", "GICS Sector"}
    if not required.issubset(headers) or len(headers) != len(set(headers)):
        raise ValueError("Constituents table is missing required unique columns.")
    rows, symbols = [], set()
    for source_row, row in enumerate(table.css("tr")[1:], 1):
        cells = row.xpath("./td")
        if len(cells) != len(headers):
            raise ValueError(
                f"Constituents row {source_row} has an unexpected column count."
            )
        fields = dict(
            zip(
                headers,
                [c.xpath("normalize-space(.)").get() for c in cells],
                strict=True,
            )
        )
        if any(not fields[key] for key in required):
            raise ValueError(
                f"Constituents row {source_row} has a missing required value."
            )
        if not re.fullmatch(r"\d{1,10}", fields["CIK"]):
            raise ValueError(f"Constituents row {source_row} has an invalid CIK.")
        if fields["Symbol"] in symbols:
            raise ValueError("Constituents table contains duplicate stock symbols.")
        symbols.add(fields["Symbol"])
        rows.append(
            {
                "symbol": fields["Symbol"],
                "cik": fields["CIK"].zfill(10),
                "name": fields["Security"],
                "sector": fields["GICS Sector"],
                "source_row": source_row,
                "source_fields": fields,
            }
        )
    if not 400 <= len(rows) <= 800:
        raise ValueError(
            "Unexpected constituents count; inspect the source before publication."
        )
    sectors = {}
    for row in rows:
        if row["cik"] in sectors and sectors[row["cik"]] != row["sector"]:
            raise ValueError("One CIK has conflicting industry classifications.")
        sectors[row["cik"]] = row["sector"]
    return rows


class PublicSourceMiddleware:
    """Check every request, including redirects, before Scrapy contacts its destination."""

    def process_request(self, request, spider):
        """Reject local destinations without executing a source request."""
        def check_destination():
            """Perform bounded destination checks away from the reactor thread."""
            try:
                validate_public_url(request.url)
            except (ValueError, OSError) as error:
                raise IgnoreRequest(str(error)) from error

        return deferToThread(check_destination)


class DocumentPipeline:
    """Await durable source registration outside Scrapy's event-loop thread."""

    def process_item(self, item, spider):
        """Return the exact database write receipt before accepting an item."""
        return deferToThread(self.persist, item, spider)

    def persist(self, item, spider):
        """Commit document records and follow-up work after saving original bytes."""
        document = item["document"]
        if document["kind"] == "index":
            spider.result = db.publish_memberships(
                spider.settings_value, document, item["rows"], spider.task["id"]
            )
        else:
            with db.connect(spider.settings_value) as conn:
                db.require_active_task(conn, spider.task["id"])
                document_id = db.record_document(conn, **document)
            spider.result.setdefault("document_ids", []).append(document_id)
        return item


class SourceSpider(scrapy.Spider):
    """Collect a named source or a bounded directory without topic-wide web crawling."""

    name = "green500_sources"

    def __init__(self, task, settings_value, **kwargs):
        """Bind one persisted task and its fixed page allowance."""
        super().__init__(**kwargs)
        self.task, self.settings_value = task, settings_value
        self.result, self.failures = {}, []
        self.request_count = 1
        self.page_limit = min(25, max(1, int(task["input"].get("page_limit", 10))))
        self.source_kind = task["input"].get("source_kind", "report")

    async def start(self):
        """Start from the configured index or explicit company source URL."""
        url = self.task["input"].get("url", SP500_URL)
        url = validate_public_url(url)
        yield scrapy.Request(
            url,
            callback=self.parse,
            errback=self.record_failure,
            meta={"original_url": url, "source_kind": self.source_kind},
        )

    def record_failure(self, failure):
        """Keep failed requests visible even when neighboring documents succeeded."""
        self.failures.append(
            {"url": failure.request.url, "error": failure.getErrorMessage()[:500]}
        )

    def parse(self, response):
        """Save the exact body before parsing, then optionally follow observed links."""
        digest = save_bytes(self.settings_value.data_dir, response.body)
        content_type = response.headers.get("Content-Type", b"").decode("latin1")
        kind = (
            "index"
            if self.task["kind"] == "collect_sp500"
            else response.meta.get("source_kind", "report")
        )
        is_pdf = response.body.lstrip().startswith(b"%PDF-")
        if is_pdf:
            kind = "report"
        title = (
            response.url.rsplit("/", 1)[-1]
            if is_pdf or not isinstance(response, TextResponse)
            else (response.css("title::text").get() or response.url)
        )
        document = {
            "company_cik": self.task["company_cik"],
            "url": response.meta["original_url"],
            "final_url": response.url,
            "title": title,
            "kind": kind,
            "digest": digest,
            "byte_count": len(response.body),
            "content_type": content_type,
            "year": self.task["input"].get("year"),
            "source_updated_at": response.headers.get("Last-Modified", b"").decode(
                "latin1"
            )
            or None,
        }
        if kind == "index":
            document["revision"] = response.css('a[href*="oldid="]::attr(href)').get()
            document["source_updated_at"] = (
                response.css("#footer-info-lastmod").xpath("normalize-space(.)").get()
            )
            yield {"document": document, "rows": parse_constituents(response)}
            return
        yield {"document": document}
        if kind not in {"directory", "feed"} or is_pdf or not isinstance(response, TextResponse):
            return
        if kind == "feed":
            response.selector.remove_namespaces()
            links = response.xpath(
                "//item/link/text() | //entry/link[@rel='alternate' or not(@rel)]/@href | //url/loc/text()"
            ).getall()
        else:
            links = response.css("a::attr(href)").getall()
        selected = []
        for href in links:
            url = response.urljoin(href)
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or url in selected:
                continue
            if kind == "directory" and not (
                parsed.path.lower().endswith(".pdf")
                or parsed.hostname == urlsplit(response.url).hostname
            ):
                self.result.setdefault("outside_directory_scope", []).append(url)
                continue
            selected.append(url)
        self.result["discovered_link_count"] = len(selected)
        for url in selected:
            if self.request_count >= self.page_limit:
                self.result.setdefault("unread_urls", []).append(url)
                continue
            self.request_count += 1
            yield scrapy.Request(
                url,
                callback=self.parse,
                errback=self.record_failure,
                meta={"original_url": url, "source_kind": "report"},
            )


def collect_task(task: dict) -> dict:
    """Run one Scrapy process with bounded requests and explicit item failures."""
    settings = load_settings()
    process = CrawlerProcess(
        {
            "USER_AGENT": "Green500/0.1 (public environmental research)",
            "ROBOTSTXT_OBEY": True,
            "CONCURRENT_REQUESTS": 4,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
            "DOWNLOAD_DELAY": 0.25,
            "DOWNLOAD_TIMEOUT": 30,
            "DOWNLOAD_MAXSIZE": settings.max_document_bytes,
            "DOWNLOAD_WARNSIZE": settings.max_document_bytes,
            "DOWNLOAD_FAIL_ON_DATALOSS": True,
            "RETRY_TIMES": 2,
            "REDIRECT_MAX_TIMES": 5,
            "LOG_LEVEL": "WARNING",
            "TELNETCONSOLE_ENABLED": False,
            "DOWNLOADER_MIDDLEWARES": {"green500.crawl.PublicSourceMiddleware": 50},
            "ITEM_PIPELINES": {"green500.crawl.DocumentPipeline": 100},
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        }
    )
    crawler = process.create_crawler(SourceSpider)
    process.crawl(crawler, task=task, settings_value=settings)
    process.start()
    spider = crawler.spider
    stats = crawler.stats.get_stats()
    if (
        not spider
        or not spider.result
        or stats.get("spider_exceptions/count", 0)
        or stats.get("item_error_count", 0)
    ):
        raise RuntimeError(
            "Source collection or item validation failed: " + (json_safe_failures(spider.failures) if spider and spider.failures else "inspect the task log and saved source.")
        )
    result = {
        **spider.result,
        "request_count": stats.get("downloader/request_count", 0),
        "failures": spider.failures,
    }
    result["is_partial"] = bool(spider.failures or spider.result.get("unread_urls"))
    if spider.failures or spider.result.get("unread_urls"):
        with db.connect(settings) as conn:
            conn.execute(
                "UPDATE tasks SET result=%s::jsonb WHERE id=%s",
                (json.dumps(result), task["id"]),
            )
    return result


def json_safe_failures(failures: list[dict]) -> str:
    """Return a short error summary without source bodies."""
    return "; ".join(item["error"] for item in failures)[:1000]
