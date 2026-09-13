"""Regression checks for safe, immutable standalone batch artifacts."""

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest

from green500.processing import (
    image_report_batch,
    image_series_batch,
    markdown_report_batch,
    social_image_batch,
)
from green500.processing.social_features import FIELDS as SOCIAL_FIELDS


@dataclass
class FakeClient:
    """Return one deterministic response while exposing fingerprinted request settings."""

    base_url: str = "https://models.example/v1"
    model: str = "test-model"
    extra_body: dict = field(default_factory=lambda: {"temperature": 0})
    calls: int = 0
    should_fail: bool = False
    outcome_unknown: bool = False

    async def complete(self, _messages, *, operation):
        self.calls += 1
        if self.outcome_unknown:
            raise httpx.RemoteProtocolError("response interrupted")
        if self.should_fail:
            raise RuntimeError("provider failed")
        return {"marker": f"{self.base_url}:{self.extra_body}", "operation": operation}


def company(company_id="SAFE"):
    """Return the manifest fields used by one mocked image extraction."""
    return {
        "company_id": company_id,
        "company_name": "Safe Company",
        "company_cik": "0000000001",
        "document_id": 1,
        "reporting_year": 2025,
        "source_sha256": "a" * 64,
        "source_url": "https://example.com/report.pdf",
        "pages": [
            {
                "page_number": 1,
                "image_sha256": "b" * 64,
                "image_byte_count": 10,
            }
        ],
    }


def configure_mocked_environment_extraction(monkeypatch):
    """Replace image parsing while retaining filesystem and fingerprint behavior."""
    monkeypatch.setattr(
        image_report_batch, "validate_company_pages", lambda _value: None
    )
    monkeypatch.setattr(image_report_batch, "extraction_messages", lambda _value: [])
    monkeypatch.setattr(
        image_report_batch,
        "request_summary",
        lambda value: {"pages": value["pages"]},
    )
    monkeypatch.setattr(
        image_report_batch,
        "completion_text",
        lambda response: response["marker"],
    )
    monkeypatch.setattr(
        image_report_batch,
        "validate_result",
        lambda text, _company, _expected: (
            {"marker": text, "metadata": {}},
            {
                "status": "passed",
                "populated_fields": 0,
                "ground_truth_checks": None,
            },
        ),
    )


def test_success_is_reused_and_endpoint_or_options_change_the_fingerprint(
    tmp_path, monkeypatch
):
    """Keep successful evidence immutable and isolate different request settings."""
    configure_mocked_environment_extraction(monkeypatch)
    first_client = FakeClient()
    first = asyncio.run(
        image_report_batch.extract(
            company(), first_client, tmp_path, provider="safe-provider"
        )
    )
    result_root = tmp_path / "environment_report" / "images" / "safe-provider" / "SAFE"
    first_directory = result_root / first["fingerprint"]
    evidence = (first_directory / "evidence.json").read_bytes()
    assert first_client.calls == 1
    assert len(list((first_directory / "attempts").iterdir())) == 1

    reused = asyncio.run(
        image_report_batch.extract(
            company(), first_client, tmp_path, provider="safe-provider"
        )
    )
    assert reused["reused"] is True
    assert first_client.calls == 1
    assert (first_directory / "evidence.json").read_bytes() == evidence
    assert len(list((first_directory / "attempts").iterdir())) == 1

    endpoint_client = FakeClient(base_url="https://other.example/v1")
    options_client = FakeClient(extra_body={"temperature": 0, "max_tokens": 100})
    asyncio.run(
        image_report_batch.extract(
            company(), endpoint_client, tmp_path, provider="safe-provider"
        )
    )
    asyncio.run(
        image_report_batch.extract(
            company(), options_client, tmp_path, provider="safe-provider"
        )
    )
    assert len(list(result_root.iterdir())) == 3


def test_each_failed_retry_has_its_own_attempt_directory(tmp_path, monkeypatch):
    """A retry retains the complete first failure instead of overwriting it."""
    configure_mocked_environment_extraction(monkeypatch)
    client = FakeClient(should_fail=True)
    first = asyncio.run(
        image_report_batch.extract(
            company(), client, tmp_path, provider="safe-provider"
        )
    )
    second = asyncio.run(
        image_report_batch.extract(
            company(), client, tmp_path, provider="safe-provider"
        )
    )
    result_directory = (
        tmp_path
        / "environment_report"
        / "images"
        / "safe-provider"
        / "SAFE"
        / first["fingerprint"]
    )
    attempts = sorted((result_directory / "attempts").iterdir())
    assert first["status"] == second["status"] == "failed"
    assert first["attempt_id"] != second["attempt_id"]
    assert len(attempts) == 2
    assert all((path / "receipt.json").is_file() for path in attempts)
    assert json.loads((attempts[0] / "receipt.json").read_text())["status"] == "failed"


def test_unknown_image_outcome_is_retained_without_another_paid_call(
    tmp_path, monkeypatch
):
    """A response interruption stays unknown until an operator reconciles it."""
    configure_mocked_environment_extraction(monkeypatch)
    client = FakeClient(outcome_unknown=True)
    first = asyncio.run(
        image_report_batch.extract(
            company(), client, tmp_path, provider="safe-provider"
        )
    )
    second = asyncio.run(
        image_report_batch.extract(
            company(), client, tmp_path, provider="safe-provider"
        )
    )
    assert first["status"] == second["status"] == "unknown"
    assert second["reused"] is True
    assert client.calls == 1


@pytest.mark.parametrize("unsafe_value", ["../escape", "a/b", r"a\b", "", "."])
def test_every_batch_rejects_unsafe_provider_or_company_segments(
    tmp_path, unsafe_value
):
    """No batch may turn an external identifier into multiple output path segments."""
    client = FakeClient()
    with pytest.raises(ValueError):
        asyncio.run(
            image_report_batch.extract(
                company(), client, tmp_path, provider=unsafe_value
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            social_image_batch.extract(
                company(unsafe_value), client, tmp_path, provider="safe-provider"
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            image_series_batch.extract(
                company(), [company()["pages"]], {unsafe_value: client}, tmp_path
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            markdown_report_batch.extract(
                {
                    "company_id": "SAFE",
                    "category": "environment_report",
                },
                tmp_path / "absent.json",
                client,
                tmp_path,
                provider=unsafe_value,
            )
        )
    with pytest.raises(ValueError):
        markdown_report_batch.prepare(
            {"company_id": unsafe_value}, tmp_path / "prepared"
        )
    assert not any(tmp_path.iterdir())


def test_markdown_prepare_rejects_an_unsafe_source_hash_before_writing(tmp_path):
    """The source digest embedded in a prepared filename cannot add path segments."""
    with pytest.raises(ValueError):
        markdown_report_batch.prepare(
            {"company_id": "SAFE", "sha256": "../escape"},
            tmp_path / "prepared",
        )
    assert not any(tmp_path.iterdir())


def test_social_validation_marks_absent_ground_truth_as_not_run():
    """Overall source validation can pass while an omitted optional check stays null."""
    payload = {
        "company": {"name": "Safe Company", "ticker": "SAFE"},
        "reporting_year": 2025,
        "boundary": None,
        "limitations": [],
        "values": dict.fromkeys(SOCIAL_FIELDS),
        "metadata": dict.fromkeys(SOCIAL_FIELDS),
    }
    _, validation = social_image_batch.validate_result(
        json.dumps(payload), company(), expected_values=None
    )
    assert validation["status"] == "passed"
    assert validation["ground_truth_checks"] is None
