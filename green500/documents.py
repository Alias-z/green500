"""Convert original HTML or PDF into stable, citable source lines."""

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from parsel import Selector

from green500 import db
from green500.storage import read_bytes, save_json

PARSER_VERSION = "source-lines-v4-pymupdf"
PROJECT_PYTHON = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
PROJECT_PACKAGE = Path(__file__).resolve().parent


def _parser_python():
    """Use Green500's own PDF dependencies when a shared LLM runner imports this package."""
    return str(PROJECT_PYTHON) if PROJECT_PYTHON.exists() else sys.executable


def html_lines(body: bytes) -> dict:
    """Retain document-order text, table rows and headings, excluding executable content."""
    selector = Selector(text=body.decode("utf-8", errors="replace"))
    for node in selector.xpath("//script|//style|//noscript|//svg"):
        node.root.getparent().remove(node.root)
    # Preserve every body text node. Selecting one article can silently omit siblings.
    root = selector.css("body")[0] if selector.css("body") else selector
    blocks = root.root.xpath(".//text()[normalize-space(.)]")
    texts, table_rows = [], set()
    for block in blocks:
        parent = block.getparent()
        row = parent
        while row is not None and row.tag != "tr":
            row = row.getparent()
        if row is not None:
            if row in table_rows:
                continue
            table_rows.add(row)
            text = " | ".join(
                " ".join(cell.itertext()) for cell in row if cell.tag in {"td", "th"}
            )
        else:
            text = str(block)
        text = " ".join(text.split())
        if text:
            texts.extend(text[i : i + 4000] for i in range(0, len(text), 4000))
    if not texts:
        texts = [" ".join(root.xpath(".//text()").getall()).strip()]
    return {
        "parser_version": PARSER_VERSION,
        "lines": [
            {"id": f"L{i}", "page": None, "text": text}
            for i, text in enumerate(texts, 1)
            if text
        ],
        "is_complete": True,
        "warnings": [],
    }


def pdf_lines(body: bytes) -> dict:
    """Use a bounded subprocess so malformed PDF processing cannot stall the worker."""
    result = subprocess.run(
        [_parser_python(), str(PROJECT_PACKAGE / "pdf_reader.py")],
        input=body,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode:
        raise ValueError("PDF could not be parsed within its resource limits.")
    return json.loads(result.stdout)


def pdf_tables(body: bytes, pages: list[int]) -> dict:
    """Extract tables from one to twenty selected PDF pages in a bounded subprocess."""
    if (
        not pages
        or len(pages) > 20
        or any(
            isinstance(page, bool) or not isinstance(page, int) or page < 1
            for page in pages
        )
    ):
        raise ValueError(
            "PDF table pages must contain one to twenty positive integers."
        )
    result = subprocess.run(
        [
            _parser_python(),
            str(PROJECT_PACKAGE / "pdf_table_reader.py"),
            "--pages",
            ",".join(str(page) for page in pages),
        ],
        input=body,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise ValueError("PDF tables could not be parsed within their resource limits.")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("PDF table parser returned invalid JSON.") from error
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != "pdf-tables-v2"
        or not isinstance(parsed.get("pages"), list)
        or [item.get("page") for item in parsed["pages"] if isinstance(item, dict)]
        != pages
    ):
        raise ValueError("PDF table parser returned an invalid page result.")
    return parsed


def feed_lines(body: bytes) -> dict:
    """Retain feed entries and embedded HTML text, including entries without links."""
    selector = Selector(text=body.decode("utf-8", errors="replace"), type="xml")
    selector.remove_namespaces()
    lines = []
    entries = selector.xpath("//item|//entry|//url")
    for index, entry in enumerate(entries, 1):
        for field in entry.xpath(
            "./title|./description|./content|./encoded|./summary|./pubDate|./published|./updated|./link|./loc"
        ):
            text = " ".join(field.xpath(".//text()").getall())
            if text:
                parsed = (
                    html_lines(text.encode())
                    if "<" in text
                    else {"lines": [{"text": text}]}
                )
                for line in parsed["lines"]:
                    lines.append(
                        {
                            "id": f"E{index}L{len(lines) + 1}",
                            "page": None,
                            "text": line["text"],
                        }
                    )
    return {
        "parser_version": PARSER_VERSION,
        "lines": lines,
        "is_complete": True,
        "warnings": [],
    }


def parse_document(settings, document: dict, task_id: int | None = None) -> dict:
    """Persist citable text and explicitly mark unreadable or incomplete content."""
    try:
        body = read_bytes(settings.data_dir, document["sha256"])
        filename = urlsplit(document["final_url"]).path.rsplit("/", 1)[-1]
        mime = document["content_type"].split(";", 1)[0].lower()
        if body.lstrip().startswith(b"%PDF-"):
            parsed = pdf_lines(body)
        elif document["kind"] == "feed":
            parsed = feed_lines(body)
        elif (
            mime in {"application/json", "text/csv", "text/plain"}
            or "openxmlformats-officedocument" in mime
            or filename.lower().endswith((".json", ".xlsx", ".csv", ".docx", ".txt"))
        ):
            from green500.source_text import build_source_text

            preview = build_source_text(body, document["content_type"], filename)
            lines = []
            for number, line in enumerate(preview["text"].splitlines(), 1):
                location, separator, text = line.partition("\t")
                lines.append(
                    {
                        "id": location if separator else f"L{number}",
                        "page": None,
                        "text": text if separator else line,
                    }
                )
            parsed = {
                "parser_version": "source-lines-structured-v1",
                "lines": lines,
                "is_complete": preview["is_complete"],
                "warnings": preview["warnings"],
            }
        else:
            parsed = html_lines(body)
        total = sum(len(line["text"]) for line in parsed["lines"])
        if not total:
            raise ValueError(
                "Source contains no readable text; OCR or a different source is required."
            )
        if total > settings.max_source_characters:
            raise ValueError(
                "Source exceeds the configured text allowance; it has not been silently truncated."
            )
        digest = save_json(settings.data_dir, parsed)
        with db.connect(settings) as conn:
            if task_id is not None:
                db.require_active_task(conn, task_id)
            conn.execute(
                "UPDATE documents SET parsed_sha256=%s,parse_status=%s,parse_error=%s WHERE id=%s",
                (
                    digest,
                    "succeeded" if parsed["is_complete"] else "incomplete",
                    "; ".join(parsed["warnings"]) or None,
                    document["id"],
                ),
            )
        return parsed
    except Exception as error:
        with db.connect(settings) as conn:
            conn.execute(
                "UPDATE documents SET parse_status='failed',parse_error=%s WHERE id=%s",
                (str(error)[:1000], document["id"]),
            )
        raise


def source_chunks(parsed: dict, maximum_characters: int) -> list[list[dict]]:
    """Split only between source lines and retain their original identifiers."""
    chunks, current, count = [], [], 0
    for line in parsed["lines"]:
        if len(line["text"]) > maximum_characters:
            raise ValueError(
                "One source block exceeds the model input allowance; inspect its table structure."
            )
        if current and count + len(line["text"]) > maximum_characters:
            chunks.append(current)
            current, count = [], 0
        current.append(line)
        count += len(line["text"])
    if current:
        chunks.append(current)
    return chunks
