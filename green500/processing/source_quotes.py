"""Locate literal source evidence using a reported row label and copied number."""

import html
import re

from green500.processing.document_evidence import source_quote_around_number


def normalize_source_text(text):
    """Ignore layout whitespace around percent signs and table separators."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(text)))
    text = " ".join(text.split())
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    text = re.sub(r"\s*%", "%", text)
    return re.sub(r"\s*\|\s*", " | ", text).casefold()


def ground_source_quote(block_text, model_quote, raw_value=None):
    """Return source text only when the supplied quote or labeled row identifies it."""
    normalized_quote = normalize_source_text(model_quote)
    lines = [line.strip() for line in block_text.splitlines() if line.strip()]
    plain_quote = normalized_quote.replace(" | ", " ")
    plain_block = normalize_source_text(block_text).replace(" | ", " ")
    if plain_quote and plain_quote in plain_block:
        matching_lines = [
            line
            for line in lines
            if plain_quote in normalize_source_text(line).replace(" | ", " ")
        ]
        if len(matching_lines) == 1:
            return matching_lines[0], "exact_source_row"
        return model_quote, "normalized_literal_quote"
    if raw_value is None:
        raise ValueError("The quoted evidence is absent from its source block.")
    sections = re.split(r"\.{2,}|…", model_quote)
    if len(sections) > 1:
        section = normalize_source_text(sections[0])
        label = normalize_source_text(sections[-1].split("|", 1)[0])
        if section in {"employees", "contractors"} and label:
            active_section = None
            section_start = 0
            candidates = []
            raw_pattern = re.compile(
                r"(?<![\d.,])"
                + re.escape(normalize_source_text(raw_value))
                + r"(?![\d.,])"
            )
            for index, line in enumerate(lines):
                cells = [normalize_source_text(cell) for cell in line.split("|")]
                if cells[-1] in {"employees", "contractors"}:
                    active_section, section_start = cells[-1], index
                if (
                    active_section == section
                    and label in cells
                    and raw_pattern.search(normalize_source_text(line))
                ):
                    candidates.append("\n".join(lines[section_start : index + 1]))
            if len(candidates) == 1:
                return candidates[0], "unique_labeled_row_in_source_section"
    anchor = re.split(r"\t|\.{2,}|…|\|", model_quote, maxsplit=1)[0].strip()
    normalized_anchor = normalize_source_text(anchor).replace(" | ", " ")
    if len(normalized_anchor) >= 8 and len(re.findall(r"[a-zA-Z]+", anchor)) >= 2:
        raw_pattern = re.compile(
            r"(?<![\d.,])" + re.escape(normalize_source_text(raw_value)) + r"(?![\d.,])"
        )
        candidates = []
        for line in lines:
            normalized_line = normalize_source_text(line).replace(" | ", " ")
            has_label = normalized_line.startswith(normalized_anchor)
            if has_label and raw_pattern.search(normalized_line):
                candidates.append(line)
        if len(candidates) == 1:
            return candidates[0], "unique_labeled_source_row"
    raw_pattern = re.compile(
        r"(?<![\d.])"
        + re.escape(normalize_source_text(raw_value).replace("%", "").strip())
        + r"(?![\d.])"
    )
    numeric_candidates = [
        line
        for line in lines
        if raw_pattern.search(
            normalize_source_text(line).replace("%", "").replace(" | ", " ")
        )
    ]
    if len(numeric_candidates) == 1:
        return numeric_candidates[0], "unique_numeric_source_row"
    return source_quote_around_number(
        block_text, raw_value
    ), "unique_raw_value_in_referenced_block"


def find_source_numeric_token(source_quote, model_raw_value):
    """Restore thousands separators from a grounded row without changing the number."""
    stripped = str(model_raw_value).strip()
    if not re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?\s*%?", stripped):
        return model_raw_value
    expected = normalize_source_text(stripped).replace("%", "").strip()
    numeric_tokens = re.findall(
        r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*%)?(?![\w.])",
        source_quote,
    )
    for token in numeric_tokens:
        if stripped.endswith("%") and not token.rstrip().endswith("%"):
            continue
        actual = normalize_source_text(token).replace("%", "").strip()
        if actual == expected:
            return token.strip()
    return model_raw_value
