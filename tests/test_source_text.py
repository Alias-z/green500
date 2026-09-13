"""Verify bounded, non-executing previews for supported source formats."""

from io import BytesIO
from zipfile import ZIP_STORED, ZipFile

import pytest
from openpyxl import Workbook

from green500 import source_text
from green500.source_text import MAX_TEXT_CHARACTERS, build_source_text


def xlsx_bytes() -> bytes:
    """Create a workbook with a formula that must remain literal."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Targets"
    sheet.append(["company", "formula"])
    sheet.append(["Example", "=1+1"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def docx_bytes(*, external=False, macro=False, table=False) -> bytes:
    """Create a minimal DOCX-shaped ZIP with optional unsafe entries."""
    output = BytesIO()
    table_xml = ""
    if table:
        table_xml = """<w:tbl>
          <w:tr><w:tc><w:p><w:r><w:t>Salary</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>Bonus</w:t></w:r></w:p></w:tc></w:tr>
          <w:tr><w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>20</w:t></w:r></w:p></w:tc></w:tr>
        </w:tbl>"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Employee safety policy</w:t></w:r></w:p>{table_xml}</w:body>
    </w:document>""".encode()
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        archive.writestr("word/document.xml", document)
        if external:
            archive.writestr(
                "word/_rels/document.xml.rels",
                '<Relationship TargetMode="External" Target="https://example.com"/>',
            )
        if macro:
            archive.writestr("word/vbaProject.bin", b"macro")
    return output.getvalue()


def test_json_is_pretty_and_truncation_is_explicit():
    """JSON Pointer locations retain exact values and expose truncation."""
    result = build_source_text(
        b'{"value": 1, "units": {"mass/kg": "kg", "escaped~key": true}}',
        "application/json",
        "source.json",
    )
    assert result == {
        "format": "json",
        "text": 'pointer="/value"\t1\npointer="/units/mass~1kg"\t"kg"\npointer="/units/escaped~0key"\ttrue',
        "is_complete": True,
        "warnings": [],
        "line_count": 3,
    }
    large = build_source_text(
        ("{\"value\":\"" + "x" * 600_000 + "\"}").encode(),
        "application/json",
        "source.json",
    )
    assert len(large["text"]) == MAX_TEXT_CHARACTERS
    assert large["is_complete"] is False
    assert "truncated" in large["warnings"][0]


def test_html_and_pdf_keep_existing_source_line_references(monkeypatch):
    """HTML and PDF previews expose existing citable IDs and remove executable HTML."""
    html = build_source_text(
        b"<html><body><h1>Report</h1><script>secret()</script><p>Text</p></body></html>",
        "text/html",
        "report.html",
    )
    assert html["format"] == "html"
    assert "L1\tReport" in html["text"] and "secret" not in html["text"]

    monkeypatch.setattr(
        source_text,
        "pdf_lines",
        lambda body: {
            "lines": [{"id": "P2L4", "page": 2, "text": "Scope 1"}],
            "is_complete": False,
            "warnings": ["Page 1 has no text layer."],
        },
    )
    pdf = build_source_text(b"%PDF-fixture", "application/pdf", "report.pdf")
    assert pdf["text"] == "P2L4\tScope 1"
    assert pdf["is_complete"] is False
    assert pdf["warnings"] == ["Page 1 has no text layer."]


def test_csv_and_xlsx_preserve_rows_sheets_and_literal_formulas():
    """Tabular previews retain source coordinates and never evaluate formulas."""
    csv_result = build_source_text(
        b"company,value\nExample,42\n", "text/csv", "facts.csv"
    )
    assert csv_result["text"].splitlines() == [
        'row=1\t["company", "value"]',
        'row=2\t["Example", "42"]',
    ]
    workbook = build_source_text(
        xlsx_bytes(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "targets.xlsx",
    )
    assert 'sheet="Targets" row=2\t["Example", "=1+1"]' in workbook["text"]
    assert workbook["is_complete"] is True


def test_plain_text_keeps_line_references_without_rendering():
    """Plain text remains inert and keeps its original line positions."""
    result = build_source_text(b"First line\n<script>still text</script>\n", "text/plain", "notes.txt")
    assert result["format"] == "text"
    assert result["text"] == "L1\tFirst line\nL2\t<script>still text</script>"


def test_docx_reads_internal_text_while_ignoring_external_links_and_macros():
    """DOCX reads local XML, ignores external hyperlinks, and rejects macros."""
    result = build_source_text(
        docx_bytes(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "policy.docx",
    )
    assert result["text"] == "paragraph=1\tEmployee safety policy"
    external = build_source_text(
        docx_bytes(external=True),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "policy.docx",
    )
    assert external["text"] == result["text"]
    assert "https://example.com" not in external["text"]
    with pytest.raises(ValueError, match="safe readable"):
        build_source_text(
            docx_bytes(macro=True),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "policy.docx",
        )


def test_docx_table_preserves_row_and_cell_relationships():
    """Table cells stay in one source row and are not repeated as paragraphs."""
    result = build_source_text(
        docx_bytes(table=True, external=True),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "proxy.docx",
    )
    assert result["text"].splitlines() == [
        "paragraph=1\tEmployee safety policy",
        'table=1 row=1\t["Salary", "Bonus"]',
        'table=1 row=2\t["100", "20"]',
    ]


def test_office_zip_limits_and_crc_failure_are_rejected(monkeypatch):
    """Declared expansion and damaged stored content fail before document XML is trusted."""
    monkeypatch.setattr(source_text, "MAX_ZIP_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(ValueError, match="safe readable"):
        build_source_text(
            docx_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "policy.docx",
        )

    monkeypatch.setattr(source_text, "MAX_ZIP_UNCOMPRESSED_BYTES", 64_000_000)
    damaged = bytearray(docx_bytes())
    marker = damaged.find(b"Employee safety policy")
    assert marker > 0
    damaged[marker] ^= 1
    with pytest.raises(ValueError, match="safe readable"):
        build_source_text(
            bytes(damaged),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "policy.docx",
        )


def test_unsupported_binary_is_never_treated_as_html():
    """Unknown binary sources require an explicit supported format."""
    with pytest.raises(ValueError, match="Unsupported source format"):
        build_source_text(b"\x00\x01binary", "application/octet-stream", "source.bin")
