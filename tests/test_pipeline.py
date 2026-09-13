"""Regression checks for source completeness and evidence-preserving normalization."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from scrapy.http import HtmlResponse

from green500 import db
from green500.config import load_settings
from green500.crawl import parse_constituents
from green500.documents import feed_lines, html_lines, source_chunks
from green500.llm import (
    ExtractionResult,
    ModelOutcomeUnknown,
    ReportedMetric,
    call_model,
    normalize_quantity,
    review_metrics,
    validate_evidence,
)
from green500.processing import report_batch
from green500.processing.climate_schema import VERDEXClimateData
from green500.storage import read_bytes, save_bytes, validate_public_url


def table_body(count=400):
    """Create an index-shaped fixture with distinct securities and valid CIK values."""
    return (
        '<table id="constituents"><tr><th>Symbol</th><th>Security</th><th>CIK</th><th>GICS Sector</th></tr>'
        + "".join(
            f"<tr><td>T{i}</td><td>Company {i}</td><td>{i + 1}</td><td>Industrials</td></tr>"
            for i in range(count)
        )
        + "</table>"
    ).encode()


def response(body):
    """Create a parser input without any network call."""
    return HtmlResponse("https://example.com/list", body=body, encoding="utf-8")


def metric(**changes):
    """Create one fully cited source quantity and selectively corrupt its fields."""
    fields = {
        "metric_code": "scope_1_emissions",
        "raw_value": "100",
        "raw_unit": "tCO2e",
        "reporting_year": 2024,
        "reporting_period": "2024",
        "boundary": "global operations",
        "calculation_method": None,
        "value_kind": "actual",
        "evidence_quote": "In 2024, actual Scope 1 emissions from global operations were 100 tCO2e.",
        "evidence_line_ids": ["L1"],
    }
    fields.update(changes)
    return ReportedMetric(**fields)


def evidence(value):
    """Validate one result against a fixed evidence line."""
    return validate_evidence(
        ExtractionResult(metrics=[value]),
        [{"id": "L1", "page": 1, "text": metric().evidence_quote}],
    )


def test_index_full_rows_and_cik_padding():
    """All rows survive and SEC identifiers keep leading zeros."""
    rows = parse_constituents(response(table_body()))
    assert len(rows) == 400 and rows[0]["cik"] == "0000000001"


@pytest.mark.parametrize(
    "body",
    [
        table_body(399),
        table_body().replace(b"<td>T1</td>", b"<td>T0</td>"),
        table_body().replace(b"<th>CIK</th>", b"<th>Other</th>"),
        table_body().replace(b"<td>1</td>", b"<td>bad</td>", 1),
    ],
)
def test_invalid_index_is_rejected(body):
    """Counts, duplicate symbols, missing columns and invalid identifiers fail closed."""
    with pytest.raises(ValueError):
        parse_constituents(response(body))


def test_html_keeps_sibling_articles_and_long_content():
    """A second article and div-only content remain in the parsed document."""
    body = (
        b"<body><article><p>First source.</p></article><article><p>Second source.</p></article><div>"
        + b"x" * 20000
        + b"</div><script>ignore()</script></body>"
    )
    parsed = html_lines(body)
    text = " ".join(x["text"] for x in parsed["lines"])
    assert (
        "First source." in text and "Second source." in text and "ignore()" not in text
    )
    assert sum(len(c) for c in source_chunks(parsed, 18000)) == len(parsed["lines"])


def test_html_table_keeps_column_relationships():
    """A table row remains one citable line instead of unrelated cell fragments."""
    parsed = html_lines(
        b"<table><tr><th>Year</th><th>Scope 1 (tCO2e)</th></tr><tr><td>2024</td><td>100</td></tr></table>"
    )
    assert [line["text"] for line in parsed["lines"]] == [
        "Year | Scope 1 (tCO2e)",
        "2024 | 100",
    ]


def test_evidence_and_explicit_unit_conversion():
    """Preserve one cited metric and convert only known unit scales."""
    assert evidence(metric())[0]["value"] == "100"
    assert normalize_quantity("1,000", "kg CO2e")["value"] == "1.000"
    assert normalize_quantity("1.000,5", "tCO2e")["value"] is None
    assert (
        normalize_quantity("12", "mystery unit")["validation_status"] == "needs_review"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_unit": "MWh"},
        {"raw_value": "999"},
        {"evidence_quote": "Invented 100 tCO2e."},
        {"evidence_line_ids": ["L2"]},
        {"raw_value": "not available"},
    ],
)
def test_unsupported_model_claims_are_rejected(changes):
    """Wrong units, values, quotations and source references cannot become metrics."""
    with pytest.raises(ValueError):
        evidence(metric(**changes))


def test_invented_context_is_flagged():
    """A supported quantity does not prove a different year or corporate boundary."""
    result = evidence(metric(reporting_year=2030, boundary="Martian operations"))[0]
    assert (
        result["validation_status"] == "needs_review"
        and not result["context_supported"]
    )


def test_partial_model_result_retains_valid_neighbor():
    """A fabricated neighboring observation does not erase a supported quantity."""
    result = ExtractionResult(metrics=[metric(), metric(raw_value="999")])
    accepted, rejected = review_metrics(
        result, [{"id": "L1", "page": 1, "text": metric().evidence_quote}]
    )
    assert len(accepted) == 1 and accepted[0]["raw_value"] == "100"
    assert len(rejected) == 1 and rejected[0]["result_index"] == 1


def test_feed_embedded_content_without_links():
    """A feed entry's text is retained even if it has no separate article URL."""
    parsed = feed_lines(
        b"<rss><channel><item><title>Environmental update</title><description><![CDATA[<p>Water use was 100 m3.</p>]]></description></item></channel></rss>"
    )
    assert "Water use was 100 m3." in " ".join(line["text"] for line in parsed["lines"])


def test_immutable_source_hash(tmp_path):
    """A changed stored source is detected before replay."""
    digest = save_bytes(tmp_path, b"original")
    assert read_bytes(tmp_path, digest) == b"original"
    (tmp_path / "objects" / digest[:2] / digest).write_bytes(b"changed")
    with pytest.raises(ValueError):
        read_bytes(tmp_path, digest)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1",
        "http://[::1]",
        "https://user:secret@example.com",
        "http://169.254.169.254",
        "https://example.com:5432",
    ],
)
def test_local_source_urls_are_rejected(url):
    """The source collector cannot target files or private network endpoints."""
    with pytest.raises(ValueError):
        validate_public_url(url)


@pytest.fixture
def database_document():
    """Use the real dedicated database and remove only this test's exact records."""
    settings = load_settings()
    cik = "9999999999"
    with db.connect(settings) as conn:
        conn.execute(
            "INSERT INTO companies(cik,name,sector,symbols,is_current) VALUES (%s,'Pipeline test','Test',ARRAY['TEST'],false)",
            (cik,),
        )
        document_id = db.record_document(
            conn,
            company_cik=cik,
            url="https://example.com/test",
            final_url="https://example.com/test",
            title="Pipeline test",
            kind="report",
            digest=save_bytes(settings.data_dir, b"test"),
            byte_count=4,
            content_type="text/html",
        )
    task_id = db.enqueue(settings, "extract", cik, {"document_id": document_id})
    task = db.claim(settings, task_id)
    with db.connect(settings) as conn:
        document = conn.execute(
            "SELECT * FROM documents WHERE id=%s", (document_id,)
        ).fetchone()
    yield settings, task, document
    with db.connect(settings) as conn:
        conn.execute("DELETE FROM metrics WHERE company_cik=%s", (cik,))
        conn.execute("DELETE FROM extractions WHERE document_id=%s", (document_id,))
        conn.execute(
            "DELETE FROM task_attempts WHERE task_id IN (SELECT id FROM tasks WHERE company_cik=%s)",
            (cik,),
        )
        conn.execute("DELETE FROM tasks WHERE company_cik=%s", (cik,))
        conn.execute("DELETE FROM documents WHERE company_cik=%s", (cik,))
        conn.execute("DELETE FROM companies WHERE cik=%s", (cik,))


def test_unknown_api_outcome_persists(database_document, monkeypatch):
    """A response interruption remains unknown, with zero invented successful metrics."""
    settings, task, document = database_document

    def fail(*args, **kwargs):
        raise httpx.RemoteProtocolError("response interrupted")

    monkeypatch.setattr(httpx.Client, "post", fail)
    with pytest.raises(ModelOutcomeUnknown):
        call_model(
            replace(settings, llm_api_key="test-only"),
            task,
            document,
            1,
            [{"id": "L1", "page": 1, "text": metric().evidence_quote}],
            True,
        )
    with db.connect(settings) as conn:
        assert (
            conn.execute(
                "SELECT status FROM extractions WHERE task_id=%s", (task["id"],)
            ).fetchone()["status"]
            == "unknown"
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM metrics WHERE company_cik=%s",
                (document["company_cik"],),
            ).fetchone()["n"]
            == 0
        )


def test_execute_command_dispatches_extract_task(monkeypatch, capsys):
    """The queued extract kind reaches the LLM extraction entry point."""
    import sys

    from green500 import __main__ as command_line
    from green500 import llm

    task = {
        "id": 42,
        "kind": "extract",
        "status": "running",
        "company_cik": "0000000042",
        "input": {"document_id": 7},
    }
    finished = []
    monkeypatch.setattr(sys, "argv", ["green500", "execute", "42"])
    monkeypatch.setattr(command_line, "load_settings", object)
    monkeypatch.setattr(command_line.db, "get_task", lambda _settings, _task_id: task)
    monkeypatch.setattr(
        command_line.db,
        "finish",
        lambda _settings, task_id, status, **values: finished.append(
            (task_id, status, values)
        ),
    )
    monkeypatch.setattr(
        llm,
        "extract_task",
        lambda value: {"task_id": value["id"], "is_partial": False},
    )

    assert command_line.main() == 0
    assert finished == [
        (
            42,
            "succeeded",
            {"result": {"task_id": 42, "is_partial": False}, "error": None},
        )
    ]
    assert json.loads(capsys.readouterr().out) == {
        "task_id": 42,
        "is_partial": False,
    }


def test_execute_command_preserves_unknown_model_outcome(monkeypatch, capsys):
    """A missing provider receipt remains unknown at the task boundary."""
    import sys

    from green500 import __main__ as command_line
    from green500 import llm

    task = {
        "id": 43,
        "kind": "extract",
        "status": "running",
        "company_cik": "0000000043",
        "input": {"document_id": 8},
    }
    finished = []
    monkeypatch.setattr(sys, "argv", ["green500", "execute", "43"])
    monkeypatch.setattr(command_line, "load_settings", object)
    monkeypatch.setattr(command_line.db, "get_task", lambda _settings, _task_id: task)
    monkeypatch.setattr(
        command_line.db,
        "finish",
        lambda _settings, task_id, status, **values: finished.append(
            (task_id, status, values)
        ),
    )

    def unknown(_task):
        raise llm.ModelOutcomeUnknown("Provider receipt is unavailable.")

    monkeypatch.setattr(llm, "extract_task", unknown)

    assert command_line.main() == 1
    assert finished == [(43, "unknown", {"error": "Provider receipt is unavailable."})]
    assert json.loads(capsys.readouterr().out) == {
        "error": "Provider receipt is unavailable."
    }


def test_expired_task_cannot_publish(database_document):
    """Late results cannot mutate a released task's data."""
    settings, task, _ = database_document
    with db.connect(settings) as conn:
        conn.execute(
            "UPDATE tasks SET expires_at=now()-interval '1 minute' WHERE id=%s",
            (task["id"],),
        )
    with db.connect(settings) as conn, pytest.raises(ValueError):
        db.require_active_task(conn, task["id"])


def test_semantic_rejection_prevents_analysis_publication(
    database_document, monkeypatch
):
    """A quantity with valid citations still requires an accepted meaning judgment."""
    settings, task, document = database_document
    replies = [
        {
            "model": "test-model",
            "usage": {"total_tokens": 10},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": ExtractionResult(
                            metrics=[metric()]
                        ).model_dump_json()
                    },
                }
            ],
        },
        {
            "model": "test-model",
            "usage": {"total_tokens": 5},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "checks": [
                                    {
                                        "index": 0,
                                        "accepted": False,
                                        "reason": "The proposed metric meaning is unsupported.",
                                    }
                                ]
                            }
                        )
                    },
                }
            ],
        },
    ]

    def reply(*args, **kwargs):
        return httpx.Response(200, json=replies.pop(0))

    monkeypatch.setattr(httpx.Client, "post", reply)
    result = call_model(
        replace(settings, llm_api_key="test-only"),
        task,
        document,
        1,
        [{"id": "L1", "page": 1, "text": metric().evidence_quote}],
        True,
    )
    assert result["metric_count"] == 0 and result["rejected_count"] == 1
    with db.connect(settings) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM analysis_metrics WHERE company_cik=%s",
                (document["company_cik"],),
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM extractions WHERE task_id=%s AND response_sha256 IS NOT NULL",
                (task["id"],),
            ).fetchone()["n"]
            == 2
        )


def test_completed_extraction_is_reused_without_api(database_document, monkeypatch):
    """Unchanged accepted work returns its existing receipt with no second API call."""
    settings, task, document = database_document
    replies = [
        {
            "model": "test-model",
            "usage": {"total_tokens": 10},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": ExtractionResult(
                            metrics=[metric()]
                        ).model_dump_json()
                    },
                }
            ],
        },
        {
            "model": "test-model",
            "usage": {"total_tokens": 5},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "checks": [
                                    {
                                        "index": 0,
                                        "accepted": True,
                                        "reason": "Supported by the supplied source.",
                                    }
                                ]
                            }
                        )
                    },
                }
            ],
        },
    ]

    def reply(*args, **kwargs):
        return httpx.Response(200, json=replies.pop(0))

    monkeypatch.setattr(httpx.Client, "post", reply)
    lines = [{"id": "L1", "page": 1, "text": metric().evidence_quote}]
    first = call_model(
        replace(settings, llm_api_key="test-only"), task, document, 1, lines, False
    )
    second = call_model(
        replace(settings, llm_api_key="test-only"), task, document, 1, lines, False
    )
    assert first["metric_count"] == 1 and second["reused"]
    assert first["extraction_id"] == second["extraction_id"] and not replies


def climate_item():
    """Return one report manifest row for deterministic climate batch tests."""
    return {
        "category": "climate_targets",
        "company_id": "TEST",
        "company_cik": "0000000042",
        "company_name": "Test Company",
        "document_id": 42,
        "sha256": "a" * 64,
        "source_document": "Test climate report",
        "source_url": "https://example.com/climate.pdf",
    }


def climate_payload(value=123):
    """Return one climate result whose numeric fields cite PDF page two."""
    return {
        "company_id": "TEST",
        "company_name": "Test Company",
        "reporting_year": 2024,
        "emissions": {
            "scope_1_tco2e": {
                "value": value,
                "unit": "tCO2e",
                "year": 2024,
                "status": "reported",
                "geographic_scope": None,
                "organizational_boundary": "company operations",
                "source_document": "Test climate report",
                "source_url": "https://example.com/climate.pdf",
                "source_page": 2,
                "source_section": None,
                "externally_assured": None,
                "confidence": 0.9,
                "notes": None,
            }
        },
    }


def climate_evidence():
    """Return one selected source block with a literal year and measurement."""
    return {
        "sha256": "a" * 64,
        "blocks": [
            {
                "block_id": 2,
                "text": "Scope 1 emissions were 123 tCO2e in 2024 for company operations.",
                "locator": {
                    "original_block_id": "PDF2",
                    "location": "page 2",
                    "page": 2,
                    "text_offset": 0,
                },
            }
        ],
    }


def test_climate_numeric_values_require_literal_declared_source_evidence():
    """Climate numbers pass only when the declared report block contains them."""
    profile = SimpleNamespace(MODEL=VERDEXClimateData)
    result, validation = report_batch.validate_result(
        json.dumps(climate_payload()), climate_item(), climate_evidence(), profile
    )
    assert result["emissions"]["scope_1_tco2e"]["value"] == 123
    assert validation["numeric_evidence_checks"] == {
        "count": 2,
        "coverage": "all_retained_climate_numeric_fields",
        "omitted_count": 0,
    }
    with pytest.raises(ValueError, match="absent from its declared source evidence"):
        report_batch.validate_result(
            json.dumps(climate_payload(999)),
            climate_item(),
            climate_evidence(),
            profile,
        )


def test_transport_outcome_is_unknown_and_retry_flag_does_not_replay(
    tmp_path, monkeypatch
):
    """A response interruption is durable and cannot trigger a second paid call."""
    monkeypatch.setattr(
        report_batch,
        "prepare_report",
        lambda *_args, **_kwargs: climate_evidence(),
    )

    class InterruptedClient:
        model = "test-model"
        base_url = "https://example.com/v1"

        def __init__(self):
            self.extra_body = {}
            self.calls = 0

        async def complete(self, _messages, *, operation):
            self.calls += 1
            raise httpx.RemoteProtocolError("response interrupted")

    client = InterruptedClient()
    first = asyncio.run(
        report_batch.extract_report(climate_item(), client, tmp_path, retry_failed=True)
    )
    second = asyncio.run(
        report_batch.extract_report(climate_item(), client, tmp_path, retry_failed=True)
    )
    assert first["status"] == "unknown"
    assert first["error"] == (
        "Model request ended without a provider response; outcome is unknown."
    )
    assert second == first
    assert client.calls == 1


def test_paused_report_batch_writes_terminal_progress(tmp_path):
    """A pause before admission leaves a durable paused progress receipt."""

    class UnusedClient:
        async def complete(self, _messages, *, operation):
            raise AssertionError("A paused item must not call the provider.")

    (tmp_path / "PAUSE").write_text("pause\n", encoding="utf-8")
    results = asyncio.run(
        report_batch.run_batch(
            [climate_item()], UnusedClient(), tmp_path, concurrency=1
        )
    )
    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert results == []
    assert progress["batch_status"] == "paused"
    assert progress["completed"] == 0
    assert progress["total"] == 1
