"""Find compact source candidates for ten fixed environment fields."""

import json
import re
from pathlib import Path

from green500.processing.direct_report_extraction import load_direct_source
from green500.processing.fixed_environment_profile import KEYWORDS
from green500.processing.report_table_markdown import (
    VERSION as REPORT_MARKDOWN_VERSION,
)
from green500.processing.report_table_markdown import prepare_report_markdown

VERSION = "environment-candidate-input-v3"

FIELD_ALIASES = {
    "env_total_ghg_tco2e": (
        r"\btotal (?:ghg|greenhouse gas|carbon|co2e) emissions\b",
        r"\b(?:ghg|carbon) emissions total\b",
        r"\boverall\b",
        r"\babsolute carbon emissions\b",
        r"\bcarbon footprint\b",
        r"^carbon emissions\b",
    ),
    "env_scope_1_tco2e": (
        r"\bscope\s*1\b",
        r"\bdirect (?:ghg|carbon) emissions\b",
    ),
    "env_scope_2_location_based_tco2e": (
        r"\blocation[- ]based\b.{0,40}\bscope\s*2\b",
        r"\bscope\s*2\b.{0,40}\blocation[- ]based\b",
        r"(?:^|\|)\s*scope\s*2\b",
    ),
    "env_scope_2_market_based_tco2e": (
        r"\bmarket[- ]based\b.{0,40}\bscope\s*2\b",
        r"\bscope\s*2\b.{0,40}\bmarket[- ]based\b",
        r"(?:^|\|)\s*scope\s*2\b",
    ),
    "env_scope_3_total_tco2e": (
        r"\btotal scope\s*3\b",
        r"\bscope\s*3 (?:ghg |carbon )?emissions\b",
        r"(?:^|\|)\s*scope\s*3\b",
    ),
    "env_total_energy_mwh": (
        r"\btotal energy (?:consumed|consumption|use|used)?\b",
        r"\benergy (?:consumed|consumption|use) total\b",
    ),
    "env_renewable_electricity_percent": (
        r"\b(?:percent|percentage|%) (?:of )?(?:electricity )?(?:from )?renewable",
        r"\brenewable electricity\b.{0,80}(?:%|percent|percentage)",
        r"(?:%|percent|percentage).{0,80}\brenewable electricity\b",
        r"\belectricity from renewable sources\b",
    ),
    "env_water_withdrawal_m3": (
        r"\btotal water withdrawal\b",
        r"\bwater withdrawal\b",
        r"\bwater withdrawn\b",
        r"\btotal water intake\b",
    ),
    "env_total_waste_tonnes": (
        r"\btotal waste (?:generated|generation)?\b",
        r"\bwaste generated\b",
    ),
    "env_waste_recycled_percent": (
        r"\b(?:waste )?recycl(?:ed|ing|ing rate)\b",
        r"\brecovery and recycling rate\b",
    ),
}

_YEAR = re.compile(r"\b(?:fy\s*)?20\d{2}\b", re.IGNORECASE)
_NUMBER = re.compile(r"[-+]?(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:\.\d+)?%?")
_UNIT = re.compile(
    r"(?:t\s*co2e|co2e|carbon dioxide equivalent|metric ton(?:ne)?s?|"
    r"thousand metric ton(?:ne)?s?|million metric ton(?:ne)?s?|mwh|gwh|twh|"
    r"gigajoules?|million gallons?|kgal|megalit(?:er|re)s?|billion lit(?:er|re)s?|"
    r"m[³3]|cubic met(?:er|re)s?|percent|percentage|%)",
    re.IGNORECASE,
)
_FIELD_UNITS = {
    "emissions": re.compile(
        r"(?:t\s*co2e|co2e|carbon dioxide equivalent|metric ton(?:ne)?s?.{0,20}(?:co2|carbon))",
        re.IGNORECASE,
    ),
    "energy": re.compile(r"(?:mwh|gwh|twh|gigajoules?|\bgj\b)", re.IGNORECASE),
    "water": re.compile(
        r"(?:million gallons?|kgal|megalit(?:er|re)s?|billion lit(?:er|re)s?|m[³3]|cubic met(?:er|re)s?)",
        re.IGNORECASE,
    ),
    "waste": re.compile(
        r"(?:metric ton(?:ne)?s?|\bmt\b|percent|percentage|%)", re.IGNORECASE
    ),
}
_FOOTNOTE = re.compile(
    r"(?:\b(?:note|footnote)\b|^[*¹²³⁴⁵⁶⁷⁸⁹\d]+[.)]\s|"
    r"\b(?:includes?|excludes?|restated|recalculated|market-based|location-based|"
    r"reporting boundary|operational control|financial control)\b)",
    re.IGNORECASE,
)


def _bounded(text, maximum=650):
    """Keep a source row intact when possible and cap pathological PDF lines."""
    text = str(text or "").strip()
    return text if len(text) <= maximum else text[:maximum]


def _has_measurement_number(text):
    """Ignore questionnaire section numbers when detecting a value-bearing row."""
    without_section_numbers = re.sub(r"\(\d+(?:\.\d+)+\)", "", text)
    return _NUMBER.search(without_section_numbers) is not None


def _document_year(item):
    """Return the most recent source-metadata year as a ranking hint."""
    title_years = [
        int(value)
        for value in re.findall(r"\b20\d{2}\b", str(item.get("source_document") or ""))
    ]
    if title_years:
        return max(title_years)
    publication_years = [
        int(value)
        for value in re.findall(
            r"\b20\d{2}\b",
            str(item.get("source_review", {}).get("publication_date") or ""),
        )
    ]
    return max(publication_years) if publication_years else None


def _rows(source):
    """Flatten PDF lines and ordered HTML table rows without losing locations."""
    rows = []
    for block_index, block in enumerate(source["blocks"]):
        for line_index, line in enumerate(block["text"].splitlines()):
            if line.strip():
                rows.append(
                    {
                        "block_index": block_index,
                        "block_id": block["block_id"],
                        "location": block.get("location")
                        or block.get("locator", {}).get("location"),
                        "is_html": re.match(
                            r"^(?:HTML row )?L\d+$",
                            str(
                                block.get("location")
                                or block.get("locator", {}).get("location")
                            ),
                        )
                        is not None,
                        "line_index": line_index,
                        "text": line.strip(),
                    }
                )
    return rows


def _nearby(rows, index, pattern, *, backward=18, forward=4):
    """Find a nearby exact header, including adjacent HTML rows."""
    current = rows[index]
    candidates = []
    for candidate_index in range(
        max(0, index - backward), min(len(rows), index + forward + 1)
    ):
        row = rows[candidate_index]
        same_page = row["block_id"] == current["block_id"]
        adjacent_html = (
            current["is_html"]
            and row["is_html"]
            and abs(row["block_index"] - current["block_index"]) <= backward
        )
        if (same_page or adjacent_html) and pattern.search(row["text"]):
            candidates.append(
                (
                    abs(candidate_index - index),
                    candidate_index,
                    _bounded(row["text"], 360),
                )
            )
    return min(candidates)[2] if candidates else None


def _unit_header(rows, index, pattern):
    """Find the strongest dimension-compatible unit label in the table region."""
    current = rows[index]
    candidates = []
    for candidate_index, row in enumerate(rows):
        same_page = row["block_id"] == current["block_id"]
        adjacent_html = (
            current["is_html"]
            and row["is_html"]
            and abs(row["block_index"] - current["block_index"]) <= 30
        )
        if not (same_page or adjacent_html) or not pattern.search(row["text"]):
            continue
        lowered = row["text"].casefold()
        score = 10
        score += (
            8
            if any(
                word in lowered
                for word in ("amounts", "unit", "emissions", "energy", "water", "waste")
            )
            else 0
        )
        score += 5 if candidate_index <= index else 0
        score -= min(abs(candidate_index - index), 25)
        candidates.append((score, -candidate_index, _bounded(row["text"], 450)))
    return max(candidates)[2] if candidates else None


def _year_header(rows, index, document_year):
    """Prefer multi-year table headers before a row over nearby narrative years."""
    current = rows[index]
    candidates = []
    for candidate_index, row in enumerate(rows):
        same_page = row["block_id"] == current["block_id"]
        adjacent_html = (
            current["is_html"]
            and row["is_html"]
            and abs(row["block_index"] - current["block_index"]) <= 30
        )
        if not (same_page or adjacent_html):
            continue
        years = _YEAR.findall(row["text"])
        reporting_label = "reporting year" in row["text"].casefold()
        if not years and not reporting_label:
            continue
        score = len(years) * 12
        score += 8 if document_year and str(document_year) in row["text"] else 0
        score += 5 if candidate_index <= index else 0
        score -= min(abs(candidate_index - index), 25)
        candidates.append((score, -candidate_index, _bounded(row["text"], 450)))
    return max(candidates)[2] if candidates else None


def _context(rows, index):
    """Return a complete bounded table region around the matching row."""
    current = rows[index]
    values = []
    for candidate_index in range(max(0, index - 40), min(len(rows), index + 41)):
        row = rows[candidate_index]
        adjacent_html = (
            current["is_html"]
            and row["is_html"]
            and abs(row["block_index"] - current["block_index"]) <= 40
        )
        if row["block_index"] == current["block_index"] or adjacent_html:
            values.append(row["text"])
    joined = "\n".join(values)
    if len(joined) <= 6_000:
        return joined
    position = joined.find(current["text"])
    start = max(0, position - 2_200)
    return joined[start : start + 6_000]


def _paired_context(rows, label_index, value_index):
    """Keep exact rows between a field label and a nearby numeric table row."""
    label = rows[label_index]
    start = max(0, min(label_index, value_index) - 2)
    end = min(len(rows), max(label_index, value_index) + 3)
    values = []
    for row in rows[start:end]:
        same_source_block = row["block_index"] == label["block_index"]
        adjacent_html = (
            label["is_html"]
            and row["is_html"]
            and abs(row["block_index"] - label["block_index"]) <= 40
        )
        if same_source_block or adjacent_html:
            values.append(row["text"])
    joined = "\n".join(values)
    if len(joined) <= 2_400:
        return joined
    marker = rows[value_index]["text"]
    position = joined.find(marker)
    start = max(0, position - 900)
    return joined[start : start + 2_400]


def _semantic_score(field, row, context):
    """Prefer numeric actual rows whose label matches the requested meaning."""
    text = (row + "\n" + context).casefold()
    score = min(len(re.findall(r"[-+]?\d[\d,.%]*", row)) * 4, 24)
    score += min(len(re.findall(r"[-+]?\d[\d,.%]*", context)) * 2, 40)
    if row.lstrip().startswith("|") and row.count("|") > 10:
        score -= 30
    if any(word in text for word in ("target", "goal", "baseline")):
        score -= 14
    if "intensity" in text and field != "env_waste_recycled_percent":
        score -= 12
    if field == "env_scope_1_tco2e" and re.search(
        r"scope\s*1\s*(?:&|\+|and)\s*(?:scope\s*)?2", text
    ):
        score -= 22
    if field == "env_scope_3_total_tco2e" and "categor" in row.casefold():
        score -= 18
    if field == "env_total_ghg_tco2e":
        score += (
            16
            if any(
                word in row.casefold()
                for word in ("overall", "total", "footprint", "carbon emissions")
            )
            else -8
        )
        if "scope 3" in row.casefold() and "total" not in row.casefold():
            score -= 16
    if field == "env_waste_recycled_percent":
        score += 10 if "%" in text or "percent" in text else -12
    return score


def build_environment_candidate_input(
    item, *, maximum_characters=80_000, maximum_candidates_per_field=5
):
    """Return ranked source rows, table headers and footnotes per field."""
    direct_source = load_direct_source(item)
    source = direct_source
    if Path(item["path"]).read_bytes().lstrip().startswith(b"%PDF-"):
        prepared = prepare_report_markdown(item, KEYWORDS)
        source = {
            "source_sha256": prepared["source_sha256"],
            "source_url": prepared["source_url"],
            "blocks": [*prepared["blocks"], *direct_source["blocks"]],
            "warnings": [*prepared["warnings"], *direct_source["warnings"]],
        }
    rows = _rows(source)
    document_year = _document_year(item)
    candidates_by_field = {}
    for field, aliases in FIELD_ALIASES.items():
        patterns = [re.compile(alias, re.IGNORECASE) for alias in aliases]
        candidates = []
        seen = set()
        for index, row in enumerate(rows):
            match = next(
                (
                    pattern.search(row["text"])
                    for pattern in patterns
                    if pattern.search(row["text"])
                ),
                None,
            )
            if match is None:
                continue
            numeric_rows = [index] if _has_measurement_number(row["text"]) else []
            if not numeric_rows:
                for value_index in range(
                    max(0, index - 40), min(len(rows), index + 41)
                ):
                    value_row = rows[value_index]
                    same_source_block = value_row["block_index"] == row["block_index"]
                    adjacent_html = (
                        row["is_html"]
                        and value_row["is_html"]
                        and abs(value_row["block_index"] - row["block_index"]) <= 40
                    )
                    if (same_source_block or adjacent_html) and _has_measurement_number(
                        value_row["text"]
                    ):
                        numeric_rows.append(value_index)
            if not numeric_rows:
                continue
            if field.startswith("env_scope") or field == "env_total_ghg_tco2e":
                unit_pattern = _FIELD_UNITS["emissions"]
            elif field == "env_total_energy_mwh":
                unit_pattern = _FIELD_UNITS["energy"]
            elif field == "env_water_withdrawal_m3":
                unit_pattern = _FIELD_UNITS["water"]
            else:
                unit_pattern = _FIELD_UNITS["waste"]
            for value_index in numeric_rows:
                value_row = rows[value_index]
                identity = (row["location"], row["text"], value_row["text"])
                if identity in seen:
                    continue
                seen.add(identity)
                context = _paired_context(rows, index, value_index)
                table_region = _context(rows, index)
                year_header = _year_header(rows, value_index, document_year)
                unit_header = _unit_header(rows, index, unit_pattern)
                footnotes = []
                for nearby_index, nearby in enumerate(rows):
                    if nearby["block_id"] != row["block_id"] or not _FOOTNOTE.search(
                        nearby["text"]
                    ):
                        continue
                    value = _bounded(nearby["text"], 300)
                    if (
                        value not in {row["text"], value_row["text"]}
                        and value not in footnotes
                    ):
                        footnotes.append(value)
                    if len(footnotes) == 2:
                        break
                score = 20 + _semantic_score(
                    field, row["text"], row["text"] + "\n" + value_row["text"]
                )
                score += max(0, 30 - abs(value_index - index) * 2)
                score += 7 if year_header else 0
                score += 7 if unit_header else 0
                if document_year and year_header and str(document_year) in year_header:
                    score += 8
                candidates.append(
                    {
                        "field": field,
                        "block_id": row["block_id"],
                        "location": row["location"],
                        "label_row": _bounded(row["text"], 900),
                        "value_row": _bounded(value_row["text"], 900),
                        "context": context,
                        "table_region": table_region,
                        "year_header": year_header,
                        "unit_header": unit_header,
                        "footnotes": footnotes,
                        "retrieval_score": score,
                    }
                )
        candidates_by_field[field] = sorted(
            candidates,
            key=lambda value: (-value["retrieval_score"], value["block_id"]),
        )[:maximum_candidates_per_field]

    result = {
        "version": VERSION,
        "source_sha256": source["source_sha256"],
        "source_url": source["source_url"],
        "document_reporting_year_hint": document_year,
        "report_markdown_version": REPORT_MARKDOWN_VERSION,
        "fields": {field: [] for field in FIELD_ALIASES},
        "warnings": source["warnings"],
    }
    budget = maximum_characters - 400
    for rank in range(maximum_candidates_per_field):
        for field in FIELD_ALIASES:
            available = candidates_by_field[field]
            if rank >= len(available):
                continue
            result["fields"][field].append(available[rank])
            if len(json.dumps(result, ensure_ascii=False)) > budget:
                result["fields"][field].pop()
    result["character_count"] = len(json.dumps(result, ensure_ascii=False))
    if result["character_count"] > maximum_characters:
        raise ValueError("Environment candidate input exceeds its character limit.")
    return result
