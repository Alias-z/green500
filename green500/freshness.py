"""Validate source-review evidence and derive freshness for the current original."""

import ipaddress
import re
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from green500.storage import validate_public_url

REVIEW_STATUSES = {"unknown", "latest_verified", "newer_available"}
SOURCE_CATEGORIES = {
    "financial_report",
    "environment_report",
    "social_employee",
    "financial_targets",
    "climate_targets",
}
TARGET_STATUSES = {"current", "achieved", "retired", "unknown"}
DOWNLOAD_STATUSES = {"downloaded", "pending", "blocked", "unknown"}
MAX_REVIEW_AGE = timedelta(days=30)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def _bounded_text(value, name: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(
            f"{name} must be non-empty text of at most {maximum} characters."
        )
    return value.strip()


def _slug(value, name: str) -> str:
    value = _bounded_text(value, name, maximum=80)
    if not SLUG_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase underscore-separated name.")
    return value


def _iso_datetime(value, name: str) -> datetime:
    value = _bounded_text(value, name, maximum=40)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO 8601 date and time.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a time-zone offset.")
    return parsed.astimezone(UTC)


def _iso_date(value, name: str) -> str:
    value = _bounded_text(value, name, maximum=10)
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO 8601 calendar date.") from error
    return value


def _public_url_syntax(value, name: str) -> str:
    value = _bounded_text(value, name, maximum=2048)
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} must use a valid port.") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443 if parsed.scheme == "https" else 80}
    ):
        raise ValueError(f"{name} must be a public HTTP(S) URL without credentials.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError(f"{name} must not use a private or reserved address.")
    return value


def _evidence_urls(value, *, check_public_urls: bool) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 20:
        raise ValueError("evidence_urls must contain between 1 and 20 public URLs.")
    urls = []
    for index, item in enumerate(value):
        url = _public_url_syntax(item, f"evidence_urls[{index}]")
        urls.append(validate_public_url(url) if check_public_urls else url)
    return urls


def _report_period(value, name: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name} must be an object or null.")
    allowed = {"start", "end", "label"}
    if set(value) - allowed:
        raise ValueError(f"{name} contains unsupported fields.")
    result = {}
    for key in ("start", "end"):
        if value.get(key) is not None:
            result[key] = _iso_date(value[key], f"{name}.{key}")
        else:
            result[key] = None
    if value.get("label") is not None:
        result["label"] = _bounded_text(value["label"], f"{name}.label", maximum=120)
    else:
        result["label"] = None
    if result["start"] and result["end"] and result["start"] > result["end"]:
        raise ValueError(f"{name}.start must not be after {name}.end.")
    if not any(result.values()):
        raise ValueError(f"{name} must contain a date or label.")
    return result


def _notes(value, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError(f"{name} must be a list of at most 10 notes.")
    return [
        _bounded_text(item, f"{name}[{index}]", maximum=300)
        for index, item in enumerate(value)
    ]


def _category_assessments(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or not set(value).issubset(SOURCE_CATEGORIES):
        raise ValueError("category_assessments contains an unsupported category.")
    result = {}
    for category, assessment in value.items():
        if not isinstance(assessment, dict) or set(assessment) - {
            "scope",
            "target_status",
            "notes",
        }:
            raise ValueError(
                f"category_assessments.{category} contains unsupported fields."
            )
        if "scope" not in assessment:
            raise ValueError(f"category_assessments.{category}.scope is required.")
        target_status = assessment.get("target_status", "unknown")
        if target_status not in TARGET_STATUSES:
            raise ValueError(
                f"category_assessments.{category}.target_status is unsupported."
            )
        result[category] = {
            "scope": _slug(
                assessment["scope"], f"category_assessments.{category}.scope"
            ),
            "target_status": target_status,
            "notes": _notes(
                assessment.get("notes"), f"category_assessments.{category}.notes"
            ),
        }
    return result


def _newer_candidate(value, *, check_public_urls: bool) -> dict:
    if not isinstance(value, dict):
        raise TypeError("newer_candidate is required when a newer source is available.")
    allowed = {
        "url",
        "title",
        "publication_date",
        "report_period",
        "download_status",
        "block_reason",
    }
    if set(value) - allowed:
        raise ValueError("newer_candidate contains unsupported fields.")
    url = _public_url_syntax(value.get("url"), "newer_candidate.url")
    result = {
        "url": validate_public_url(url) if check_public_urls else url,
        "title": _bounded_text(
            value.get("title"), "newer_candidate.title", maximum=300
        ),
        "publication_date": (
            _iso_date(value["publication_date"], "newer_candidate.publication_date")
            if value.get("publication_date") is not None
            else None
        ),
        "report_period": _report_period(
            value.get("report_period"), "newer_candidate.report_period"
        ),
        "download_status": value.get("download_status", "unknown"),
    }
    if result["download_status"] not in DOWNLOAD_STATUSES:
        raise ValueError("newer_candidate.download_status is unsupported.")
    block_reason = value.get("block_reason")
    result["block_reason"] = (
        _bounded_text(block_reason, "newer_candidate.block_reason", maximum=300)
        if block_reason is not None
        else None
    )
    if result["download_status"] == "blocked" and not result["block_reason"]:
        raise ValueError("A blocked newer_candidate requires block_reason.")
    return result


def validate_source_review(review, *, check_public_urls: bool = False) -> dict:
    """Return a bounded normalized review or raise before it can make a freshness claim."""
    if not isinstance(review, dict) or not review:
        raise ValueError("A source review must be a non-empty object.")
    allowed = {
        "status",
        "document_sha256",
        "checked_at",
        "valid_until",
        "report_family",
        "scope",
        "evidence_urls",
        "publication_date",
        "report_period",
        "newer_candidate",
        "category_assessments",
        "notes",
    }
    if set(review) - allowed:
        raise ValueError("Source review contains unsupported fields.")
    status = review.get("status")
    if status not in REVIEW_STATUSES:
        raise ValueError("Source review status is unsupported.")
    digest = review.get("document_sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("document_sha256 must be a lowercase SHA-256 digest.")
    checked_at = _iso_datetime(review.get("checked_at"), "checked_at")
    valid_until = _iso_datetime(review.get("valid_until"), "valid_until")
    if valid_until <= checked_at or valid_until - checked_at > MAX_REVIEW_AGE:
        raise ValueError(
            "valid_until must be after checked_at and no more than 30 days later."
        )
    result = {
        "status": status,
        "document_sha256": digest,
        "checked_at": checked_at.isoformat(),
        "valid_until": valid_until.isoformat(),
        "report_family": _slug(review.get("report_family"), "report_family"),
        "scope": _slug(review.get("scope"), "scope"),
        "evidence_urls": _evidence_urls(
            review.get("evidence_urls"), check_public_urls=check_public_urls
        ),
        "publication_date": (
            _iso_date(review["publication_date"], "publication_date")
            if review.get("publication_date") is not None
            else None
        ),
        "report_period": _report_period(review.get("report_period"), "report_period"),
        "newer_candidate": None,
        "category_assessments": _category_assessments(
            review.get("category_assessments")
        ),
        "notes": _notes(review.get("notes"), "notes"),
    }
    if status == "newer_available":
        result["newer_candidate"] = _newer_candidate(
            review.get("newer_candidate"), check_public_urls=check_public_urls
        )
    elif review.get("newer_candidate") is not None:
        raise ValueError("newer_candidate is only valid for newer_available reviews.")
    return result


def effective_source_review(review, document_sha256: str | None, *, now=None) -> dict:
    """Derive a non-throwing freshness result tied to the currently served document bytes."""
    labels = {
        "unknown": "Not verified",
        "latest_verified": "Latest verified",
        "newer_available": "Newer available",
    }
    unknown = {
        "status": "unknown",
        "reason": "not_reviewed",
        "label": labels["unknown"],
        "document_sha256": None,
        "checked_at": None,
        "valid_until": None,
        "report_family": None,
        "scope": None,
        "evidence_urls": [],
        "publication_date": None,
        "report_period": None,
        "newer_candidate": None,
        "category_assessments": {},
        "notes": [],
    }
    if not review:
        return unknown
    try:
        result = deepcopy(validate_source_review(review))
    except (TypeError, ValueError):
        return dict(unknown, reason="invalid_review")
    if result["document_sha256"] != document_sha256:
        result.update(
            status="unknown", reason="content_changed", label=labels["unknown"]
        )
        return result
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must include a time-zone offset.")
    if _iso_datetime(result["valid_until"], "valid_until") <= current_time.astimezone(
        UTC
    ):
        result.update(
            status="unknown", reason="verification_expired", label=labels["unknown"]
        )
        return result
    result.update(reason="valid", label=labels[result["status"]])
    return result
