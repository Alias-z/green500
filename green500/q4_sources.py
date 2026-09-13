"""Discover PDF candidates exposed by a detected Q4 ContentAsset widget."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

Q4_SCRIPT_PATTERN = re.compile(
    r"(?:widgets\.q4app\.com|q4[._-]?api(?:\.min)?\.js)", re.IGNORECASE
)
DOWNLOADS_WIDGET_PATTERN = re.compile(
    r"\.downloads\s*\(\s*\{(?P<options>.*?)\}\s*\)", re.DOTALL
)
DOWNLOAD_TYPE_PATTERN = re.compile(
    r"downloadType\s*:\s*(['\"])(?P<value>[^'\"]+)\1", re.IGNORECASE
)
MIN_YEAR_PATTERN = re.compile(r"minYear\s*:\s*(?P<value>20[1-3][0-9])")
RELEVANT_ASSET_TYPE_PATTERN = re.compile(
    r"sustainab|\besg\b|responsib|environment|climate|human[\s_-]*rights|"
    r"social|workforce|employee|people|diversity|impact",
    re.IGNORECASE,
)
YEAR_PATTERN = re.compile(r"\b(20[1-3][0-9])\b")
MAX_CONTENT_ASSET_ROWS = 2_000
MAX_CONTENT_ASSET_BYTES = 5_000_000
FAMILY_PRIORITY = {
    "sustainability_update": 0,
    "human_rights": 1,
    "esg_factsheet": 2,
    "california_ab_1305": 3,
    "other_esg_asset": 4,
}


def _plain_public_url(url: str) -> str | None:
    """Reject malformed, local, credentialed, and non-web asset URLs without DNS."""
    try:
        parsed = urlsplit(str(url or ""))
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or (port is not None and port != (443 if parsed.scheme == "https" else 80))
    ):
        return None
    hostname = parsed.hostname.casefold().strip(".")
    if hostname == "localhost" or hostname.endswith(".local"):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return urlunsplit((parsed.scheme, hostname, parsed.path or "/", parsed.query, ""))


def q4_content_asset_widgets(html: str, page_url: str) -> list[dict]:
    """Build public API requests only for relevant Q4 download widgets in issuer HTML."""
    if not Q4_SCRIPT_PATTERN.search(html):
        return []
    normalized_page_url = _plain_public_url(page_url)
    if not normalized_page_url:
        return []
    page = urlsplit(normalized_page_url)
    widgets = []
    seen_types = set()
    for match in DOWNLOADS_WIDGET_PATTERN.finditer(html):
        options = match.group("options")
        type_match = DOWNLOAD_TYPE_PATTERN.search(options)
        if not type_match:
            continue
        asset_type = type_match.group("value").strip()
        type_key = asset_type.casefold()
        if not RELEVANT_ASSET_TYPE_PATTERN.search(asset_type) or type_key in seen_types:
            continue
        seen_types.add(type_key)
        min_year_match = MIN_YEAR_PATTERN.search(options)
        params = [
            ("assetType", asset_type),
            ("pageSize", -1),
            ("pageNumber", 0),
            ("tagList", ""),
            ("includeTags", "true"),
            ("year", -1),
            ("excludeSelection", 1),
            ("LanguageId", 1),
        ]
        origin = urlunsplit((page.scheme, page.netloc, "", "", ""))
        widgets.append(
            {
                "asset_type": asset_type,
                "api_url": origin
                + "/feed/ContentAsset.svc/GetContentAssetList?"
                + urlencode(params),
                "source_page_url": normalized_page_url,
                "fetch_all_years": bool(
                    re.search(r"fetchAllYears\s*:\s*true", options, re.IGNORECASE)
                ),
                "min_year": int(min_year_match.group("value"))
                if min_year_match
                else None,
            }
        )
    return widgets


def _report_family(title: str, url: str) -> str:
    description = f"{title} {urlsplit(url).path.rsplit('/', 1)[-1]}".casefold()
    if re.search(r"ab[\s_-]*1305|california.*statement", description):
        return "california_ab_1305"
    if re.search(r"human[\s_-]*rights", description):
        return "human_rights"
    if re.search(r"facts?[\s_-]*sheet", description):
        return "esg_factsheet"
    if re.search(r"sustainab|\besg\b|impact|\bupdate\b", description):
        return "sustainability_update"
    return "other_esg_asset"


def _report_year(title: str, url: str) -> int | None:
    values = YEAR_PATTERN.findall(title)
    if not values:
        values = YEAR_PATTERN.findall(urlsplit(url).path.rsplit("/", 1)[-1])
    return max(map(int, values)) if values else None


def _candidate_topics(asset_type: str, title: str, url: str) -> list[str]:
    description = f"{asset_type} {title} {url}".casefold()
    topics = []
    if re.search(
        r"sustainab|\besg\b|responsib|environment|climate|impact|ab[\s_-]*1305",
        description,
    ):
        topics.append("environment")
    if re.search(
        r"sustainab|\besg\b|responsib|human[\s_-]*rights|social|workforce|employee|people|diversity|impact",
        description,
    ):
        topics.append("social")
    return topics


def parse_q4_content_asset_response(
    body: bytes, content_type: str, widget: dict
) -> tuple[list[dict], list[str]]:
    """Parse and rank publisher asset rows without claiming a report period or category."""
    if len(body) > MAX_CONTENT_ASSET_BYTES:
        raise ValueError("Q4 ContentAsset response exceeds the 5,000,000-byte limit.")
    if "json" not in content_type.casefold() or body.lstrip().startswith(b"<"):
        raise ValueError("Q4 ContentAsset response is not JSON.")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Q4 ContentAsset response is invalid JSON.") from error
    rows = value.get("GetContentAssetListResult") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_CONTENT_ASSET_ROWS:
        raise ValueError("Q4 ContentAsset response has an invalid result list.")
    candidates = []
    issues = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            issues.append(f"row {index}: expected an object")
            continue
        if str(row.get("Type") or "").casefold() != widget["asset_type"].casefold():
            continue
        url = _plain_public_url(row.get("FilePath"))
        title = str(row.get("Title") or "").strip()
        file_type = str(row.get("FileType") or "").casefold()
        try:
            asset_date = datetime.strptime(
                str(row.get("ContentAssetDate") or ""), "%m/%d/%Y %H:%M:%S"
            ).replace(tzinfo=UTC)
        except ValueError:
            issues.append(f"row {index}: invalid ContentAssetDate")
            continue
        if (
            not url
            or file_type != "pdf"
            or not urlsplit(url).path.casefold().endswith(".pdf")
            or not title
        ):
            issues.append(f"row {index}: invalid public PDF record")
            continue
        family = _report_family(title, url)
        candidates.append(
            {
                "url": url,
                "title": title,
                "year": _report_year(title, url),
                "topics": _candidate_topics(widget["asset_type"], title, url),
                "discovered_on": widget["source_page_url"],
                "publisher_asset_date": asset_date.date().isoformat(),
                "reporting_period": None,
                "report_family": family,
                "discovery_evidence": {
                    "category": "q4_content_asset_listing",
                    "text": "Publisher Q4 listing candidate; the downloaded body must establish categories and reporting period.",
                    "source_page_url": widget["source_page_url"],
                    "api_url": widget["api_url"],
                    "asset_type": widget["asset_type"],
                    "content_asset_id": row.get("ContentAssetId"),
                    "publisher_asset_date": asset_date.date().isoformat(),
                    "publisher_file_size": row.get("FileSize"),
                    "publisher_file_type": row.get("FileType"),
                    "revision_number": row.get("RevisionNumber"),
                    "workflow_id": row.get("WorkflowId"),
                    "report_family": family,
                    "reporting_period": None,
                },
            }
        )
    candidates.sort(
        key=lambda item: (
            FAMILY_PRIORITY[item["report_family"]],
            -datetime.fromisoformat(item["publisher_asset_date"]).timestamp(),
            -(item["year"] or 0),
            item["title"].casefold(),
        )
    )
    return candidates, issues
