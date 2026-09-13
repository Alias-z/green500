"""Create compact report input with intact page text and Markdown tables."""

import hashlib
import re
from pathlib import Path

from green500.documents import html_lines, pdf_lines, pdf_tables

VERSION = "targeted-report-markdown-v7"

_ENVIRONMENT_PAGE_FAMILIES = (
    ("greenhouse gas", "ghg", "scope 1", "scope 2", "scope 3"),
    ("energy consumption", "total energy", "renewable energy", "renewable electricity"),
    ("water withdrawal", "water consumption", "water discharge"),
    (
        "waste generated",
        "total waste",
        "hazardous waste",
        "non-hazardous waste",
        "recycled waste",
        "waste recycled",
        "recycling rate",
    ),
    ("land restored", "land affected"),
    ("nox", "sox", "mercury"),
)


def _page_score(text, keywords):
    """Rank pages by category words and numeric table density."""
    lowered = text.casefold()
    keyword_score = sum(
        8 + min(lowered.count(term.casefold()), 8)
        for term in keywords
        if term.casefold() in lowered
    )
    table_lines = sum(
        len(re.findall(r"[-+]?\d[\d,.%]*", line)) >= 3 for line in text.splitlines()
    )
    intact_decimals = len(re.findall(r"\b\d+\.\d+\b", text))
    split_decimals = len(re.findall(r"\b\d+\s+\d\b", text))
    reference_penalty = 0
    if "gri" in lowered and "disclosure reference" in lowered:
        reference_penalty += 60
    if "sasb matrix" in lowered or "table of contents" in lowered:
        reference_penalty += 40
    return (
        keyword_score
        + min(table_lines, 20)
        + min(intact_decimals * 2, 12)
        - min(split_decimals, 12)
        - reference_penalty
    )


def _family_pages(page_text, keywords):
    """Select the strongest page for each requested environmental metric family."""
    requested = {keyword.casefold() for keyword in keywords}
    selected = []
    for family in _ENVIRONMENT_PAGE_FAMILIES:
        terms = tuple(term for term in family if term in requested)
        candidates = [
            page
            for page, text in page_text.items()
            if any(term in text.casefold() for term in terms)
        ]
        if not candidates:
            continue
        page = max(
            candidates,
            key=lambda value: (_page_score(page_text[value], terms), -value),
        )
        if page not in selected:
            selected.append(page)
    return selected


def _markdown_table(rows):
    """Render rectangular table cells without changing their source text."""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]

    def cell(value):
        return value.replace("|", "\\|").replace("\n", "<br>")

    header = normalized[0]
    body = normalized[1:]
    return "\n".join(
        [
            "| " + " | ".join(cell(value) for value in header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
            *("| " + " | ".join(cell(value) for value in row) + " |" for row in body),
        ]
    )


def prepare_report_markdown(
    item, keywords, *, maximum_characters=45_000, maximum_pages=10
):
    """Select relevant original pages and retain tables plus reading-order text."""
    path = Path(item["path"])
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest != item["sha256"]:
        raise ValueError("Saved original differs from the report manifest.")
    if body.lstrip().startswith(b"%PDF-"):
        parsed = pdf_lines(body)
        pages = {}
        for line in parsed["lines"]:
            pages.setdefault(line["page"], []).append(line["text"])
        page_text = {page: "\n".join(lines) for page, lines in pages.items()}
        ranked = sorted(
            page_text,
            key=lambda page: (_page_score(page_text[page], keywords), -page),
            reverse=True,
        )
        selected = _family_pages(page_text, keywords)
        for page in ranked:
            if page not in selected:
                selected.append(page)
        ordered_pages = selected[:maximum_pages]
        table_warnings = []
        extracted_tables = {}
        try:
            for page_result in pdf_tables(body, ordered_pages)["pages"]:
                page_number = page_result["page"]
                extracted_tables[page_number] = page_result
        except ValueError as error:
            table_warnings.append(f"Selected PDF pages: {error}")
        sections = []
        source_blocks = []
        selected_pages = []
        for page in ordered_pages:
            parts = [f"## PDF page {page}"]
            page_tables = extracted_tables.get(page, {})
            for index, table in enumerate(page_tables.get("tables", []), 1):
                parts.extend(
                    (
                        f"### Extracted table {index} ({page_tables['strategy']})",
                        _markdown_table(table),
                    )
                )
            parts.extend(("### Reading-order text", page_text[page]))
            section = "\n\n".join(parts)
            used = sum(len(value) for value in sections)
            if used + len(section) <= maximum_characters:
                sections.append(section)
            else:
                section = "\n\n".join(
                    (
                        f"## PDF page {page}",
                        "### Reading-order text",
                        page_text[page],
                    )
                )
                if used + len(section) > maximum_characters:
                    continue
                sections.append(section)
            selected_pages.append(page)
            source_blocks.append(
                {
                    "block_id": page,
                    "text": section,
                    "locator": {
                        "original_block_id": f"PDF page {page}",
                        "location": f"PDF page {page}",
                        "page": page,
                        "text_offset": 0,
                    },
                }
            )
        markdown = "\n\n".join(sections)
        return {
            "version": VERSION,
            "source_sha256": digest,
            "source_url": item["source_url"],
            "markdown": markdown,
            "blocks": source_blocks,
            "selected_pages": sorted(selected_pages),
            "warnings": [*parsed["warnings"], *table_warnings],
        }
    parsed = html_lines(body)
    lines = parsed["lines"]
    ranked = sorted(
        lines,
        key=lambda line: (_page_score(line["text"], keywords), line["id"]),
        reverse=True,
    )
    selected = []
    source_blocks = []
    used = 0
    for line in ranked:
        section = f"## HTML row {line['id']}\n\n{line['text']}"
        if used + len(section) > maximum_characters:
            continue
        selected.append(section)
        source_blocks.append(
            {
                "block_id": len(source_blocks) + 1,
                "text": section,
                "locator": {
                    "original_block_id": line["id"],
                    "location": f"HTML row {line['id']}",
                    "page": None,
                    "text_offset": 0,
                },
            }
        )
        used += len(section)
    return {
        "version": VERSION,
        "source_sha256": digest,
        "source_url": item["source_url"],
        "markdown": "\n\n".join(selected),
        "blocks": source_blocks,
        "selected_pages": [],
        "warnings": parsed["warnings"],
    }
