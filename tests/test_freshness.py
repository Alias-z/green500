"""Verify that freshness claims remain bounded, evidenced and tied to exact bytes."""

from datetime import UTC, datetime, timedelta

import pytest

from green500.freshness import effective_source_review, validate_source_review

DIGEST = "a" * 64
CHECKED_AT = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def review(**changes):
    """Build one complete review whose structured dates have explicit public evidence."""
    value = {
        "status": "latest_verified",
        "document_sha256": DIGEST,
        "checked_at": CHECKED_AT.isoformat(),
        "valid_until": (CHECKED_AT + timedelta(days=14)).isoformat(),
        "report_family": "annual_sustainability_report",
        "scope": "company_publisher_series",
        "evidence_urls": ["https://example.com/reports"],
        "publication_date": "2026-05-01",
        "report_period": {
            "start": "2025-01-01",
            "end": "2025-12-31",
            "label": "Fiscal 2025",
        },
    }
    value.update(changes)
    return value


def test_empty_review_is_unknown_and_legacy_text_cannot_make_it_current():
    result = effective_source_review({}, DIGEST, now=CHECKED_AT)
    assert result["status"] == "unknown"
    assert result["reason"] == "not_reviewed"
    assert result["checked_at"] is None
    assert result["report_period"] is None
    assert result["category_assessments"] == {}


def test_latest_review_is_valid_only_for_the_exact_document_and_time_window():
    current = effective_source_review(review(), DIGEST, now=CHECKED_AT)
    assert current["status"] == "latest_verified"
    assert current["reason"] == "valid"
    assert current["publication_date"] == "2026-05-01"

    changed = effective_source_review(review(), "b" * 64, now=CHECKED_AT)
    assert changed["status"] == "unknown"
    assert changed["reason"] == "content_changed"

    expired = effective_source_review(
        review(), DIGEST, now=CHECKED_AT + timedelta(days=15)
    )
    assert expired["status"] == "unknown"
    assert expired["reason"] == "verification_expired"


def test_review_window_cannot_exceed_thirty_days():
    with pytest.raises(ValueError, match="no more than 30 days"):
        validate_source_review(
            review(valid_until=(CHECKED_AT + timedelta(days=31)).isoformat())
        )


def test_newer_available_keeps_candidate_status_and_category_meaning():
    value = validate_source_review(
        review(
            status="newer_available",
            newer_candidate={
                "url": "https://example.com/reports/2026.pdf",
                "title": "2026 Sustainability Report",
                "publication_date": "2026-08-01",
                "report_period": {"label": "Fiscal 2026"},
                "download_status": "blocked",
                "block_reason": "Publisher download requires an interactive session.",
            },
            category_assessments={
                "climate_targets": {
                    "scope": "companywide_targets",
                    "target_status": "current",
                }
            },
        )
    )
    assert value["newer_candidate"]["download_status"] == "blocked"
    assert value["category_assessments"]["climate_targets"]["target_status"] == (
        "current"
    )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"document_sha256": None}, "document_sha256"),
        ({"checked_at": "2026-09-12T12:00:00"}, "time-zone"),
        (
            {"report_period": {"start": "2026-12-31", "end": "2026-01-01"}},
            "must not be after",
        ),
        ({"evidence_urls": ["http://127.0.0.1/report"]}, "private or reserved"),
        ({"status": "newer_available"}, "newer_candidate is required"),
    ],
)
def test_invalid_review_cannot_create_a_freshness_claim(changes, message):
    with pytest.raises((TypeError, ValueError), match=message):
        validate_source_review(review(**changes))
