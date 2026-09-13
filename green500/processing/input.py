"""Prepare citable source blocks and select financial evidence before model calls."""

import hashlib
import re
from collections import deque
from decimal import Decimal, InvalidOperation
from html import escape

from lxml import html

from green500.source_text import build_source_text

VERSION = "source-blocks-v6-positioned-inline-xbrl"

_ANNUAL_REPORT_COVER = re.compile(
    r"\bANNUAL REPORT PURSUANT\b.{0,600}?"
    r"(?P<quote>For the (?:fiscal )?year ended\s+"
    r"(?:[A-Za-z]+\s+\d{1,2},\s+)?(?P<year>(?:19|20)\d{2}))(?!\d)",
    re.IGNORECASE | re.DOTALL,
)


def _clean(text):
    return " ".join(text.split())


def _inline_numeric_facts(cell):
    """Keep auditable Inline XBRL scale and unit attributes for one table cell."""
    facts = []
    for element in cell.iter():
        if not str(element.tag).casefold().endswith("nonfraction"):
            continue
        scale = element.get("scale", "0")
        if not re.fullmatch(r"-?\d{1,2}", scale):
            continue
        text = _clean("".join(element.itertext()))
        if not text:
            continue
        fact = {
            "text": text,
            "scale": int(scale),
            "scale_is_explicit": "scale" in element.attrib,
            "unit_ref": element.get("unitref"),
            "name": element.get("name"),
            "context_ref": element.get("contextref"),
            "fact_id": element.get("id"),
        }
        if element.get("sign") in {"-", "+"}:
            fact["sign"] = element.get("sign")
        facts.append(fact)
    return facts


def _style_position(element, name):
    """Read one absolute pixel coordinate without accepting computed guesses."""
    match = re.search(
        rf"(?:^|;)\s*{name}\s*:\s*(-?\d+(?:\.\d+)?)px\b",
        element.get("style", ""),
        re.IGNORECASE,
    )
    return float(match.group(1)) if match else None


def _positioned_page_table(page, table_identifier):
    """Rebuild one visual page from exact absolute positions and Inline XBRL facts."""
    fragments = []
    for element in page.xpath("./div"):
        left = _style_position(element, "left")
        top = _style_position(element, "top")
        text = _clean("".join(element.itertext()))
        if left is None or top is None or not text:
            continue
        fragments.append(
            {
                "left": left,
                "top": top,
                "text": text,
                "source_id": element.get("id"),
                "source_path": element.getroottree().getpath(element),
                "numeric_facts": _inline_numeric_facts(element),
            }
        )
    page_facts = [
        element
        for element in page.iter()
        if str(element.tag).casefold().endswith("nonfraction")
    ]
    captured_fact_ids = {
        fact.get("fact_id")
        for fragment in fragments
        for fact in fragment["numeric_facts"]
        if fact.get("fact_id")
    }
    page_fact_ids = {element.get("id") for element in page_facts if element.get("id")}
    if (
        len(page_facts) < 4
        or len(fragments) < 8
        or len(page_fact_ids) != len(page_facts)
        or len(captured_fact_ids) != len(page_facts)
        or captured_fact_ids != page_fact_ids
    ):
        return None

    grouped = []
    for fragment in sorted(fragments, key=lambda item: (item["top"], item["left"])):
        if not grouped or fragment["top"] - grouped[-1]["top"] > 1.0:
            grouped.append({"top": fragment["top"], "fragments": [fragment]})
        else:
            grouped[-1]["fragments"].append(fragment)
            grouped[-1]["top"] = min(grouped[-1]["top"], fragment["top"])

    rendered_rows = []
    source_rows = []
    for row_number, grouped_row in enumerate(grouped):
        row_identifier = f"{table_identifier}R{row_number:04d}"
        cells = sorted(grouped_row["fragments"], key=lambda item: item["left"])
        cell_text = [cell["text"] for cell in cells]
        numeric_facts = [
            fact for cell in cells for fact in cell.get("numeric_facts", [])
        ]
        rendered_rows.append(
            f'<tr data-row-id="{row_identifier}">'
            + "".join(
                f'<td data-left="{cell["left"]:g}">{escape(cell["text"])}</td>'
                for cell in cells
            )
            + "</tr>"
        )
        source_row = {
            "id": row_identifier,
            "text": " | ".join(cell_text),
            "top": grouped_row["top"],
            "layout_cells": cells,
        }
        if numeric_facts:
            source_row["numeric_facts"] = numeric_facts
        source_rows.append(source_row)
    heading_rows = [
        row["text"]
        for row in source_rows
        if row["top"] < 200 and row["text"]
    ]
    return {
        "id": table_identifier,
        "kind": "table",
        "location": f"{page.getroottree().getpath(page)} absolute layout",
        "context": "\n".join(heading_rows[-12:]),
        "text": "<table>\n" + "\n".join(rendered_rows) + "\n</table>",
        "requires_table_review": False,
        "rows": source_rows,
        "layout": "absolute_positioned_divs",
        "page_id": page.get("id"),
    }


def _positioned_page_tables(root, starting_table_number):
    """Return synthetic tables only for filings with no semantic table elements."""
    if root.xpath("//table"):
        return []
    blocks = []
    for page in root.xpath("//div[starts-with(@id,'Page')]"):
        identifier = f"T{starting_table_number + len(blocks):05d}"
        block = _positioned_page_table(page, identifier)
        if block is not None:
            blocks.append(block)
    return blocks


def _display_number(value):
    """Return one comparable displayed number without applying any scale."""
    match = re.fullmatch(r"\s*\(?\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*\)?\s*", value)
    if match is None:
        return None
    try:
        number = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return format(number.normalize(), "f")


def _row_display_numbers(value):
    """Return distinct displayed quantities while excluding standalone years."""
    numbers = []
    for match in re.finditer(r"(?<![A-Za-z0-9])\(?[+-]?\d[\d,]*(?:\.\d+)?\)?", value):
        number = _display_number(match.group(0))
        if number is None:
            continue
        numeric = Decimal(number)
        if numeric == numeric.to_integral() and 1900 <= numeric <= 2100:
            continue
        if number not in numbers:
            numbers.append(number)
    return numbers


def _is_currency_unit_reference(value):
    """Exclude explicit ratio, share, count and percent units from currency evidence."""
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.casefold()
    return not any(
        term in normalized
        for term in ("share", "pure", "percent", "number", "employee")
    )


def _attach_matching_scale_evidence(blocks):
    """Link factless display rows to duplicate Inline XBRL rows by source numbers."""
    fact_rows = []
    for block in blocks:
        for row in block.get("rows", []):
            facts = [
                fact
                for fact in row.get("numeric_facts", [])
                if _is_currency_unit_reference(fact.get("unit_ref"))
            ]
            scales = {
                fact["scale"]
                for fact in facts
                if isinstance(fact.get("scale"), int)
            }
            numbers = {
                number
                for fact in facts
                if (number := _display_number(str(fact.get("text", "")))) is not None
            }
            if len(scales) == 1 and len(numbers) >= 2:
                fact_rows.append(
                    {
                        "block_id": block["id"],
                        "row_id": row["id"],
                        "scale": next(iter(scales)),
                        "unit_refs": sorted(
                            {fact["unit_ref"] for fact in facts if fact.get("unit_ref")}
                        ),
                        "numbers": numbers,
                    }
                )
    financial_label = re.compile(
        r"sales|revenue|income|profit|loss|asset|cash|debt|borrow|"
        r"capital|property|equipment|cost|expense",
        re.IGNORECASE,
    )
    for block in blocks:
        for row in block.get("rows", []):
            if row.get("numeric_facts") or not financial_label.search(row.get("text", "")):
                continue
            numbers = set(_row_display_numbers(row.get("text", "")))
            candidates = [
                candidate
                for candidate in fact_rows
                if len(numbers & candidate["numbers"]) >= 2
            ]
            scales = {candidate["scale"] for candidate in candidates}
            if len(scales) != 1:
                continue
            best_overlap = max(len(numbers & candidate["numbers"]) for candidate in candidates)
            best = [
                candidate
                for candidate in candidates
                if len(numbers & candidate["numbers"]) == best_overlap
            ]
            row["source_scale_evidence"] = {
                "scale": next(iter(scales)),
                "unit_refs": sorted(
                    {unit for candidate in best for unit in candidate["unit_refs"]}
                ),
                "basis": "matching_inline_xbrl_source_rows",
                "matching_values": sorted(
                    numbers & set.intersection(*(candidate["numbers"] for candidate in best))
                ),
                "source_rows": [
                    {
                        "block_id": candidate["block_id"],
                        "row_id": candidate["row_id"],
                    }
                    for candidate in best[:8]
                ],
            }


def _table_text(table, table_identifier):
    """Keep semantic HTML and assign stable identifiers to source rows."""
    rendered_rows = []
    source_rows = []
    for row_number, row in enumerate(table.xpath(".//tr")):
        row_identifier = f"{table_identifier}R{row_number:04d}"
        cells = []
        cell_text = []
        numeric_facts = []
        for cell in row.xpath("./th|./td"):
            value = _clean("".join(cell.itertext()))
            cell_text.append(value)
            numeric_facts.extend(_inline_numeric_facts(cell))
            attributes = "".join(
                f' {name}="{escape(cell.get(name), quote=True)}"'
                for name in ("colspan", "rowspan")
                if cell.get(name)
            )
            cells.append(f"<{cell.tag}{attributes}>{escape(value)}</{cell.tag}>")
        rendered_rows.append(
            f'<tr data-row-id="{row_identifier}">' + "".join(cells) + "</tr>"
        )
        source_row = {"id": row_identifier, "text": " | ".join(cell_text)}
        if numeric_facts:
            source_row["numeric_facts"] = numeric_facts
        source_rows.append(source_row)
    return "<table>\n" + "\n".join(rendered_rows) + "\n</table>", source_rows


def _annual_report_cover(raw_text, blocks):
    """Retain the exact annual-report cover phrase before hidden markup is removed."""
    match = _ANNUAL_REPORT_COVER.search(raw_text)
    if match is None:
        return None
    cover_block = next(
        (
            block["id"]
            for block in blocks[:120]
            if "annual report pursuant" in _clean(block.get("text", "")).casefold()
        ),
        None,
    )
    if cover_block is None:
        return None
    return {
        "block_id": cover_block,
        "quote": _clean(match.group("quote")),
        "fiscal_year": int(match.group("year")),
    }


def prepare_input(body: bytes, content_type: str, filename: str, source_url: str):
    """Retain originals by hash and make source-location blocks without model calls."""
    if not body or len(body) > 64_000_000:
        raise ValueError("Source must contain between 1 and 64000000 bytes.")
    source_hash = hashlib.sha256(body).hexdigest()
    blocks, warnings = [], []
    if "html" in content_type or filename.lower().endswith((".htm", ".html")):
        root = html.fromstring(body)
        raw_text = _clean("".join(root.itertext()))
        for element in root.xpath(
            "//script|//style|//noscript|//nav|//iframe|//*[@hidden]|//*[contains(translate(@style,' ',''),'display:none')]"
        ):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
        previous = deque(maxlen=5)
        table_number = 0
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            if element.xpath("ancestor::table"):
                continue
            tag = element.tag.lower()
            if tag == "table":
                identifier = f"T{table_number:05d}"
                table_number += 1
                text, source_rows = _table_text(element, identifier)
                context = "\n".join(previous)
                ambiguous = bool(element.xpath(".//table"))
                if ambiguous:
                    warnings.append(
                        f"{identifier} contains nested tables; numeric selection requires original-file review."
                    )
            elif tag in {
                "p",
                "div",
                "h1",
                "h2",
                "h3",
                "h4",
                "li",
            } and not element.xpath(
                ".//p|.//div|.//table|.//h1|.//h2|.//h3|.//h4|.//li"
            ):
                text = _clean("".join(element.itertext()))
                identifier = f"P{len(blocks):05d}"
                context = ""
                ambiguous = False
                if text:
                    previous.append(text[-1000:])
            else:
                continue
            if not text:
                continue
            block = {
                "id": identifier,
                "kind": "table" if tag == "table" else "paragraph",
                "location": root.getroottree().getpath(element),
                "context": context,
                "text": text,
                "requires_table_review": ambiguous,
            }
            if tag == "table":
                block["rows"] = source_rows
            blocks.append(block)
        positioned_tables = _positioned_page_tables(root, table_number)
        if positioned_tables:
            blocks.extend(positioned_tables)
            warnings.append(
                "HTML has no semantic table tags; verified absolute page positions and "
                "Inline XBRL facts were reconstructed into synthetic source tables."
            )
        complete = not any(block.get("requires_table_review") for block in blocks)
        warnings.append(
            "HTML tables retain empty cells and rowspan/colspan and expose stable data-row-id values in sanitized semantic HTML."
        )
    else:
        parsed = build_source_text(body, content_type, filename)
        complete = parsed["is_complete"]
        warnings.extend(parsed["warnings"])
        for line_number, line in enumerate(parsed["text"].splitlines()):
            location, separator, text = line.partition("\t")
            if parsed["format"] == "pdf" and separator:
                page_match = re.fullmatch(r"P(\d+)L\d+", location)
                if page_match:
                    identifier = f"PDF{int(page_match[1]):05d}"
                    if not blocks or blocks[-1]["id"] != identifier:
                        blocks.append(
                            {
                                "id": identifier,
                                "kind": "page",
                                "location": f"page {page_match[1]}",
                                "context": "",
                                "text": "",
                            }
                        )
                    blocks[-1]["text"] += f"[{location}] {text}\n"
                    continue
            blocks.append(
                {
                    "id": f"L{line_number:06d}",
                    "kind": "text",
                    "location": location if separator else str(line_number),
                    "context": "",
                    "text": text if separator else line,
                }
            )
    _attach_matching_scale_evidence(blocks)
    result = {
        "version": VERSION,
        "source_sha256": source_hash,
        "source_url": source_url,
        "input_bytes": len(body),
        "is_complete": complete,
        "warnings": warnings,
        "blocks": blocks,
    }
    if "html" in content_type or filename.lower().endswith((".htm", ".html")):
        cover = _annual_report_cover(raw_text, blocks)
        if cover is not None:
            result["annual_report_cover"] = cover
    return result


def select_financial_blocks(document):
    """Select statement tables and workforce evidence by content, retaining neighbors."""
    selected = set()
    blocks = document["blocks"]
    for index, block in enumerate(blocks):
        text = (block["context"] + "\n" + block["text"]).casefold()
        matches = (
            ("total assets" in text and "total liabilities" in text)
            or ("revenue" in text and "income before" in text and "net income" in text)
            or (
                "operating activities" in text
                and "investing activities" in text
                and "financing activities" in text
            )
            or ("free cash flow" in text and ("margin" in text or "purchases" in text))
            or ("adjusted ebitda" in text and "margin" in text)
            or (
                "united states" in text
                and "international" in text
                and "total" in text
                and re.search(r"\d", text)
            )
            or (
                "employees" in text
                and ("as of" in text or "year ended" in text)
                and re.search(r"\d", text)
            )
        )
        if matches:
            selected.update(range(max(0, index - 1), min(len(blocks), index + 2)))
    result = dict(document)
    result["blocks"] = [
        block
        for index, block in enumerate(blocks)
        if index in selected and not block.get("requires_table_review")
    ]
    result["selection"] = {
        "version": "financial-statements-v2",
        "total_blocks": len(blocks),
        "selected_blocks": len(result["blocks"]),
        "is_exhaustive": False,
    }
    return result


def render_markdown(document):
    """Create provider-neutral Markdown with stable source IDs and context."""
    lines = [
        f"Source: {document['source_url']}",
        f"SHA256: {document['source_sha256']}",
    ]
    for block in document["blocks"]:
        lines.append(f"\n## [{block['id']}] {block['kind']}")
        if block["context"]:
            lines.append("Context: " + block["context"])
        lines.append(block["text"])
    return "\n\n".join(lines)
