"""Build bounded readable previews from immutable source bytes."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from io import BytesIO, StringIO
from pathlib import PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook

from green500.documents import html_lines, pdf_lines

MAX_TEXT_CHARACTERS = 500_000
MAX_INPUT_BYTES = 64_000_000
MAX_ZIP_FILES = 5_000
MAX_ZIP_UNCOMPRESSED_BYTES = 64_000_000
MAX_SHEETS, MAX_ROWS_PER_SHEET = 100, 10_000
MAX_CELLS, MAX_PARAGRAPHS = 200_000, 10_000
MAX_JSON_NODES, MAX_JSON_DEPTH = 100_000, 100
MAX_DOCX_TABLES = 1_000


def _format_name(content_type: str, filename: str, body: bytes) -> str:
    """Identify only formats with an explicit MIME type, extension, or signature."""
    mime = content_type.split(";", 1)[0].strip().casefold()
    suffix = PurePosixPath(filename).suffix.casefold()
    if body.lstrip().startswith(b"%PDF-") or mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if mime in {"application/json", "application/ld+json"} or suffix == ".json":
        return "json"
    if mime in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm"}:
        return "html"
    if mime == "text/plain" or suffix == ".txt": return "text"
    if mime == "text/csv" or suffix == ".csv": return "csv"
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or suffix == ".xlsx": return "xlsx"
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or suffix == ".docx": return "docx"
    raise ValueError("Unsupported source format; provide JSON, HTML, PDF, XLSX, CSV, or DOCX.")


def _bounded_text(lines: list[str], warnings: list[str]) -> tuple[str, int, bool]:
    """Join complete reference lines without exceeding the preview character limit."""
    kept, count, used = [], 0, 0
    for line in lines:
        separator = 1 if kept else 0
        remaining = MAX_TEXT_CHARACTERS - used - separator
        if remaining <= 0:
            warnings.append("Preview text was truncated at 500000 characters.")
            return "\n".join(kept), count, False
        if len(line) > remaining:
            kept.append(line[:remaining])
            count += 1
            warnings.append("Preview text was truncated at 500000 characters.")
            return "\n".join(kept), count, False
        kept.append(line)
        count += 1
        used += len(line) + separator
    return "\n".join(kept), count, True


def _parsed_lines(parsed: dict) -> dict:
    warnings = list(parsed.get("warnings") or [])
    lines = [f"{line['id']}\t{line['text']}" for line in parsed.get("lines", [])]
    text, line_count, fits = _bounded_text(lines, warnings)
    return {"text": text, "is_complete": bool(parsed.get("is_complete", False) and fits), "warnings": warnings, "line_count": line_count}


def _safe_zip(archive: ZipFile) -> None:
    """Reject oversized, encrypted, unsafe, macro-enabled, or damaged Office ZIPs."""
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_FILES or sum(info.file_size for info in infos) > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ValueError("Office ZIP exceeds its file-count or uncompressed-size limit.")
    for info in infos:
        path = PurePosixPath(info.filename)
        if info.flag_bits & 1 or path.is_absolute() or ".." in path.parts:
            raise ValueError("Office ZIP contains encrypted or unsafe entries.")
        if path.name.casefold() == "vbaproject.bin":
            raise ValueError("Macro-enabled Office documents are not supported.")
    damaged = archive.testzip()
    if damaged:
        raise ValueError(f"Office ZIP failed its CRC check: {damaged}")


def _json_preview(body: bytes) -> dict:
    try:
        value = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON source is not valid UTF-8 JSON.") from error
    warnings: list[str] = []
    lines, node_count = [], 0
    pending = [("", value, 0)]
    while pending:
        pointer, current, depth = pending.pop()
        node_count += 1
        if node_count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            warnings.append("JSON node or depth limit reached.")
            break
        if isinstance(current, dict) and current:
            for key, child in reversed(list(current.items())):
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                pending.append((pointer + "/" + escaped, child, depth + 1))
        elif isinstance(current, list) and current:
            for index in range(len(current) - 1, -1, -1):
                pending.append((pointer + f"/{index}", current[index], depth + 1))
        else:
            lines.append(f"pointer={json.dumps(pointer, ensure_ascii=False)}\t{json.dumps(current, ensure_ascii=False)}")
    return _make_result(lines, warnings)


def _csv_preview(body: bytes) -> dict:
    decoded = body.decode("utf-8-sig", errors="replace")
    warnings = ["Invalid UTF-8 bytes were replaced."] if "�" in decoded else []
    rows = []
    for number, row in enumerate(csv.reader(StringIO(decoded, newline="")), 1):
        if number > MAX_ROWS_PER_SHEET:
            warnings.append("CSV row limit reached.")
            break
        rows.append(f"row={number}\t{json.dumps(row, ensure_ascii=False)}")
    return _make_result(rows, warnings)


def _text_preview(body: bytes) -> dict:
    decoded = body.decode("utf-8-sig", errors="replace")
    warnings = ["Invalid UTF-8 bytes were replaced."] if "�" in decoded else []
    source_lines = decoded.splitlines()
    if len(source_lines) > MAX_ROWS_PER_SHEET:
        source_lines = source_lines[:MAX_ROWS_PER_SHEET]
        warnings.append("Text line limit reached.")
    return _make_result([f"L{number}\t{text}" for number, text in enumerate(source_lines, 1)], warnings)


def _office_value(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _xlsx_preview(body: bytes) -> dict:
    warnings: list[str] = []
    try:
        with ZipFile(BytesIO(body)) as archive:
            _safe_zip(archive)
        workbook = load_workbook(BytesIO(body), read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, KeyError, OSError, ValueError) as error:
        raise ValueError("XLSX source is not a safe readable workbook.") from error
    lines, cells = [], 0
    try:
        sheets = workbook.worksheets[:MAX_SHEETS]
        if len(workbook.worksheets) > MAX_SHEETS:
            warnings.append("Workbook sheet limit reached.")
        for sheet in sheets:
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                if row_number > MAX_ROWS_PER_SHEET or cells + len(row) > MAX_CELLS:
                    warnings.append(f"Workbook row or cell limit reached in sheet {sheet.title!r}.")
                    break
                cells += len(row)
                values = [_office_value(value) for value in row]
                while values and values[-1] is None:
                    values.pop()
                if values:
                    lines.append(f"sheet={json.dumps(sheet.title, ensure_ascii=False)} row={row_number}\t{json.dumps(values, ensure_ascii=False)}")
    finally:
        workbook.close()
    return _make_result(lines, warnings)


def _docx_preview(body: bytes) -> dict:
    warnings: list[str] = []
    try:
        with ZipFile(BytesIO(body)) as archive:
            _safe_zip(archive)
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError, ValueError) as error:
        raise ValueError("DOCX source is not a safe readable document.") from error
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    body_node = root.find(namespace + "body")
    if body_node is None:
        raise ValueError("DOCX source has no document body.")
    lines, paragraphs, tables, cells = [], 0, 0, 0
    for block in body_node:
        if block.tag == namespace + "p":
            paragraphs += 1
            if paragraphs > MAX_PARAGRAPHS:
                warnings.append("DOCX paragraph limit reached.")
                break
            text = "".join(node.text or "" for node in block.iter(namespace + "t"))
            if text.strip(): lines.append(f"paragraph={paragraphs}\t{text}")
        elif block.tag == namespace + "tbl":
            tables += 1
            if tables > MAX_DOCX_TABLES:
                warnings.append("DOCX table limit reached.")
                break
            for row_number, row in enumerate(block.findall(namespace + "tr"), 1):
                row_cells = row.findall(namespace + "tc")
                if row_number > MAX_ROWS_PER_SHEET or cells + len(row_cells) > MAX_CELLS:
                    warnings.append(f"DOCX row or cell limit reached in table {tables}.")
                    break
                cells += len(row_cells)
                values = ["".join(node.text or "" for node in cell.iter(namespace + "t")) for cell in row_cells]
                lines.append(f"table={tables} row={row_number}\t{json.dumps(values, ensure_ascii=False)}")
    return _make_result(lines, warnings)


def _make_result(value: str | list[str], warnings: list[str]) -> dict:
    lines = value.splitlines() if isinstance(value, str) else value
    text, line_count, fits = _bounded_text(lines, warnings)
    return {"text": text, "is_complete": fits and not warnings, "warnings": warnings, "line_count": line_count}


def build_source_text(body: bytes, content_type: str, filename: str) -> dict:
    """Return a bounded readable preview without writing files or executing source content."""
    if not body or len(body) > MAX_INPUT_BYTES:
        raise ValueError("Source is empty or exceeds the 64 MB preview input limit.")
    source_format = _format_name(content_type, filename, body)
    if source_format == "html":
        result = _parsed_lines(html_lines(body))
    elif source_format == "pdf":
        result = _parsed_lines(pdf_lines(body))
    elif source_format == "json":
        result = _json_preview(body)
    elif source_format == "csv":
        result = _csv_preview(body)
    elif source_format == "text":
        result = _text_preview(body)
    elif source_format == "xlsx":
        result = _xlsx_preview(body)
    else:
        result = _docx_preview(body)
    return {"format": source_format, **result}
