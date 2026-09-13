"""Build bounded, source-addressed evidence for report extraction."""

import hashlib
import re
from pathlib import Path

from green500.processing.input import prepare_input


def parse_reported_number(raw_value):
    """Parse one copied numeric token without accepting arithmetic expressions."""
    if not isinstance(raw_value, str) or not raw_value.strip() or len(raw_value) > 100:
        raise ValueError("Environmental raw_value must be one short source token.")
    match = re.fullmatch(
        r"\s*[$€£¥]?\s*(?P<number>-?\d[\d,\s]*(?:\.\d+)?)"
        r"(?P<suffix>\s*(?:%|[xX]|[-–—]?[A-Za-z][A-Za-z ._/-]*))?\s*",
        raw_value,
    )
    if not match:
        raise ValueError(
            "Environmental raw_value must contain one number, not a calculation."
        )
    compact = re.sub(r"\s+", "", match.group("number"))
    if not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", compact):
        raise ValueError("Environmental raw_value has unsupported digit grouping.")
    return float(compact.replace(",", ""))


def source_quote_around_number(block_text, raw_value):
    """Return bounded exact source context around one unambiguous raw token."""
    starts = [match.start() for match in re.finditer(re.escape(raw_value), block_text)]
    if not starts:
        raise ValueError("Environmental raw_value is absent from the referenced block.")
    if len(starts) != 1:
        raise ValueError(
            "Environmental raw_value is ambiguous in the referenced block."
        )
    start = starts[0]
    end = start + len(raw_value)
    line_start = block_text.rfind("\n", 0, start) + 1
    line_end = block_text.find("\n", end)
    if line_end < 0:
        line_end = len(block_text)
    for _ in range(2):
        previous = block_text.rfind("\n", 0, max(0, line_start - 1))
        line_start = 0 if previous < 0 else previous + 1
        following = block_text.find("\n", min(len(block_text), line_end + 1))
        line_end = len(block_text) if following < 0 else following
    quote = block_text[line_start:line_end].strip()
    if len(quote) > 1_600:
        relative_start = start - line_start
        left = max(0, relative_start - 700)
        right = min(len(quote), relative_start + len(raw_value) + 700)
        quote = quote[left:right].strip()
    if raw_value not in quote:
        raise ValueError("Environmental source context did not retain raw_value.")
    return quote


def normalize_pdf_page_text(text):
    """Replace PDF layout padding with visible column separators before budgeting."""
    lines = []
    for line in text.splitlines():
        compact = re.sub(r"[^\S\n]{2,}", " | ", line.strip()).strip()
        if compact:
            lines.append(compact)
    return "\n".join(lines)


def line_preserving_fragments(text, maximum_characters=12_000):
    """Split bounded evidence without cutting ordinary table or text rows."""
    fragments = []
    start = 0
    current = []
    current_count = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        if current and current_count + len(line) > maximum_characters:
            fragments.append((start, "".join(current)))
            current, current_count = [], 0
            start = offset
        if len(line) > maximum_characters:
            if current:
                fragments.append((start, "".join(current)))
                current, current_count = [], 0
            for position in range(0, len(line), maximum_characters):
                fragments.append(
                    (offset + position, line[position : position + maximum_characters])
                )
            start = offset + len(line)
        else:
            if not current:
                start = offset
            current.append(line)
            current_count += len(line)
        offset += len(line)
    if current:
        fragments.append((start, "".join(current)))
    return fragments or [(0, text)]


def prepare_report(
    item,
    profile,
    *,
    max_characters=120_000,
    include_leading_blocks=True,
    balanced_term_groups=(),
    require_keyword_match=False,
):
    """Keep source locations, then select bounded relevant text for one model call."""
    path = Path(item["path"])
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        signature = handle.read(8)
        handle.seek(0)
        for chunk in iter(lambda: handle.read(4_194_304), b""):
            digest.update(chunk)
    source_hash = digest.hexdigest()
    if item.get("sha256") and source_hash != item["sha256"]:
        raise ValueError("Saved report differs from its source manifest.")
    if signature.lstrip().startswith(b"%PDF-"):
        from green500.documents import pdf_lines

        parsed = pdf_lines(path.read_bytes())
        blocks, warnings = [], []
        for line in parsed["lines"]:
            page = line["page"]
            if not blocks or blocks[-1]["page"] != page:
                blocks.append(
                    {
                        "id": f"PDF{page:05d}",
                        "text": "",
                        "context": "",
                        "location": f"page {page}",
                        "page": page,
                        "kind": "page",
                    }
                )
            blocks[-1]["text"] += line["text"] + "\n"
        for block in blocks:
            block["text"] = normalize_pdf_page_text(block["text"])
        warnings = parsed["warnings"]
        complete = parsed["is_complete"]
    else:
        body = path.read_bytes()
        document = prepare_input(
            body, item["content_type"], item["filename"], item["source_url"]
        )
        blocks = document["blocks"]
        warnings = document["warnings"]
        complete = document["is_complete"]
        if not blocks and "html" in item["content_type"].casefold():
            from green500.documents import html_lines

            parsed = html_lines(body)
            blocks = [
                {
                    "id": f"HTML{index:05d}",
                    "text": line["text"],
                    "context": "",
                    "location": f"HTML text block {index}",
                    "page": None,
                    "kind": "text",
                }
                for index, line in enumerate(parsed["lines"], 1)
            ]
            warnings = parsed["warnings"]
            complete = parsed["is_complete"]
    if not blocks:
        raise ValueError(
            "Report has no readable text; an original with a text layer is needed."
        )
    fragments = []
    for block in blocks:
        text = block.get("context", "") + "\n" + block["text"]
        for fragment_index, (offset, fragment) in enumerate(
            line_preserving_fragments(text)
        ):
            page = block.get("page")
            fragments.append(
                {
                    "block_id": (
                        page
                        if fragment_index == 0
                        else 100_000 + page * 1000 + fragment_index
                    )
                    if page is not None
                    else len(fragments) + 1,
                    "text": fragment,
                    "locator": {
                        "original_block_id": block["id"],
                        "location": block["location"],
                        "page": page,
                        "text_offset": offset,
                    },
                }
            )
    terms = tuple(term.casefold() for term in profile.KEYWORDS)

    def score(block):
        text = block["text"].casefold()
        return sum(
            4 + min(text.count(term), 8) for term in terms if term in text
        ) + min(len(re.findall(r"\d", text)) / 100, 6)

    ranked = sorted(
        (
            block
            for block in fragments
            if not require_keyword_match
            or any(term in block["text"].casefold() for term in terms)
        ),
        key=lambda block: (score(block), -block["block_id"]),
        reverse=True,
    )
    selected = []
    used = 0
    ordered = list(fragments[:2]) if include_leading_blocks else []
    uncovered = [
        tuple(term.casefold() for term in group) for group in balanced_term_groups
    ]
    remaining = list(fragments)
    while uncovered:
        candidates = []
        for block in remaining:
            if used + len(block["text"]) > max_characters:
                continue
            text = block["text"].casefold()
            covered = [
                group for group in uncovered if any(term in text for term in group)
            ]
            if covered:
                candidates.append(
                    (len(covered), score(block), -block["block_id"], block, covered)
                )
        if not candidates:
            break
        _, _, _, block, covered = max(candidates, key=lambda candidate: candidate[:3])
        ordered.append(block)
        remaining.remove(block)
        uncovered = [group for group in uncovered if group not in covered]
        used += len(block["text"])
    used = 0
    for block in ordered + ranked:
        if any(previous["block_id"] == block["block_id"] for previous in selected):
            continue
        if used + len(block["text"]) > max_characters:
            continue
        selected.append(block)
        used += len(block["text"])
    selected.sort(
        key=lambda block: (
            block["locator"].get("page") or 0,
            block["locator"]["text_offset"],
            block["block_id"],
        )
    )
    return {
        "sha256": source_hash,
        "source_url": item["source_url"],
        "text_format_version": "report-evidence-v4-pdf-page-identifiers",
        "offset_basis": "normalized evidence block text",
        "document_name": item["source_document"],
        "blocks": selected,
        "source_text_complete": complete,
        "all_text_selected": len(selected) == len(fragments),
        "available_block_count": len(fragments),
        "selected_characters": used,
        "warnings": warnings,
    }
