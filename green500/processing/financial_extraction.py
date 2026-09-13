"""Prepare, extract and verify standardized source-linked financial metrics."""

from __future__ import annotations

import json
import re
from decimal import Decimal

from green500.processing.financial_input import render_financial_input
from green500.processing.financial_prompt import (
    METRIC_NAMES,
    REVIEW_PROMPT,
    SYSTEM_PROMPT,
)
from green500.processing.financial_schema import VERDEXFinancialData
from green500.processing.input import prepare_input
from green500.processing.validation import _hydrate_citation, _reported_numbers

SELECTION_VERSION = "verdex-financial-primary-statements-v19"
_FISCAL_YEAR = re.compile(
    r"\bfor the (?:fiscal )?year ended\b.{0,160}?\b(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE | re.DOTALL,
)
_FORBIDDEN_DEBT_COMPONENTS = (
    "accounts payable",
    "customer funds",
    "customer deposits",
    "deposits",
    "lease",
    "securities financing",
    "total liabilities",
    "trade liabilities",
    "trade payable",
)
_ALLOWED_DEBT_COMPONENTS = (
    "borrowings",
    "commercial paper",
    "credit facilities",
    "debt",
    "note payable",
    "notes and debentures",
    "notes payable",
    "senior unsecured notes",
)


def prepare_financial_source(body, content_type, filename, source_url):
    """Select primary statements plus narrow workforce, debt and green-capex evidence."""
    full = prepare_input(body, content_type, filename, source_url)
    blocks = full["blocks"]
    roles = {}

    def primary_statement_candidates(block, index):
        if block.get("kind") != "table" or block.get("requires_table_review"):
            return []
        row_labels = [
            row.get("text", "").split("|", 1)[0].strip().casefold()
            for row in block.get("rows", [])
            if isinstance(row, dict)
        ]
        visible = _visible_block_text(block)
        if not re.search(r"\d", visible):
            return []
        context = block.get("context", "")[-800:].casefold()
        prior_titles = []
        for previous in blocks[max(0, index - 2) : index]:
            if len(previous.get("text", "")) <= 500:
                if previous.get("rows"):
                    prior_titles.extend(
                        row.get("text", "").strip(" |").casefold()
                        for row in previous["rows"]
                        if isinstance(row, dict)
                    )
                else:
                    prior_titles.extend(
                        _visible_block_text(previous).casefold().splitlines()
                    )
        def normalize_title(line):
            return (
                line.replace("consolidatedstatements", "consolidated statements")
                .replace("consolidatedbalance", "consolidated balance")
                .replace("statementsof", "statements of")
            )

        title_lines = [
            normalize_title(line.strip())
            for line in prior_titles + context.splitlines()[-8:] + row_labels[:5]
            if line.strip()
        ]
        nearest_title_lines = [
            normalize_title(line.strip())
            for line in prior_titles[-3:] + context.splitlines()[-5:] + row_labels[:3]
            if line.strip()
        ]
        context_heading = "\n".join(prior_titles + context.splitlines()[-8:])

        def has_title(pattern):
            return any(re.fullmatch(pattern, line) for line in title_lines)

        def has_nearest_title(pattern):
            return any(re.fullmatch(pattern, line) for line in nearest_title_lines)

        hard_bad_context = bool(
            re.search(
                r"\b(?:segment|pro forma|fair value|non-gaap|supplemental|"
                r"parent company|condensed|quarterly|quarter ended|schedule\s+i|"
                r"discontinued operations|financial statement schedules?|exhibits)\b",
                context_heading,
            )
        )
        soft_bad_context = bool(
            re.search(
                r"\b(?:summary|reconciliation|management.?s discussion|"
                r"working capital|unaudited)\b",
                context_heading,
            )
        )
        formal_context = bool(
            re.search(
                r"\bitem\s*8\b|accompanying notes .* integral part", context
            )
        )
        unconditional_bad_context = bool(
            re.search(r"\b(?:parent company|schedule\s+i)\b", context_heading)
        )
        context_is_bad = hard_bad_context or (soft_bad_context and not formal_context)
        unconditional_penalty = 100 * unconditional_bad_context
        results = []

        has_revenue = any(
            re.match(
                r"(?:total )?(?:net )?(?:revenues?|sales(?: and revenues?)?|"
                r"operating revenues?|interest income)(?:\W|$)",
                label,
            )
            for label in row_labels
        )
        has_income_result = any(
            re.match(
                r"(?:net (?:income|earnings|loss)|operating (?:income|profit|loss)|"
                r"income \(loss\) from continuing operations|"
                r"profit(?: of consolidated .*|\s*\d+)?)(?:\W|$)",
                label,
            )
            for label in row_labels
        )
        income_title = (
            r"(?:(?:consolidated(?: and combined)? )?statements? of "
            r"(?:operations|income|earnings|profit(?: or loss)?)"
            r"(?: and comprehensive income)?(?: for .*)?|consolidated results of "
            r"(?:operations|income|earnings)(?: for .*)?|"
            r"statements? of consolidated (?:income|operations|earnings)|"
            r"consolidated income statements?|"
            r"statement\s*1)"
        )
        income_heading = has_title(income_title)
        income_nearest = has_nearest_title(income_title)
        if income_heading or (has_revenue and has_income_result):
            score = (
                60 * income_nearest
                + 30 * (income_heading and not income_nearest)
                + 10 * (has_revenue and has_income_result)
                - unconditional_penalty
                - 40 * (context_is_bad and not income_nearest)
            )
            if score > 0:
                results.append(("income_statement", score))

        has_assets = any(
            re.match(r"total assets(?:\W|$)", label) for label in row_labels
        )
        has_liabilities = any(
            re.match(r"total liabilities(?:\W|$)", label) for label in row_labels
        )
        balance_title = (
            r"(?:(?:consolidated )?balance sheets?|"
            r"(?:consolidated(?: and combined)? )?statements? of "
            r"(?:financial )?(?:position|condition)|"
            r"statements? of consolidated financial (?:position|condition)|"
            r"statement\s*3)(?: (?:at|as of) .*)?"
        )
        balance_heading = has_title(balance_title)
        balance_nearest = has_nearest_title(balance_title)
        if balance_heading or (has_assets and has_liabilities):
            score = (
                60 * balance_nearest
                + 30 * (balance_heading and not balance_nearest)
                + 10 * (has_assets and has_liabilities)
                - unconditional_penalty
                - 40 * (context_is_bad and not balance_nearest)
            )
            if score > 0:
                results.append(("balance_sheet", score))

        has_cash_flow = any(
            re.search(
                r"(?:cash flows? from operating activities|"
                r"cash flows? from operations|"
                r"(?:net )?cash flows? (?:generated|provided|used).*?"
                r"(?:operating activities|operations)|"
                r"net (?:cash|change) (?:from|due to) operating activities|"
                r"cash (?:from|provided by) operating activities)",
                label,
            )
            for label in row_labels
        )
        cash_title = (
            r"(?:(?:consolidated(?: and combined)? )?statements? of cash flows?"
            r"(?:\([^)]*\))?|statements? of consolidated cash flows?|"
            r"cash flows? statements?|statement\s*5)(?: for .*)?"
        )
        cash_heading = has_title(cash_title)
        cash_nearest = has_nearest_title(cash_title)
        if cash_heading or has_cash_flow:
            score = (
                60 * cash_nearest
                + 30 * (cash_heading and not cash_nearest)
                + 10 * has_cash_flow
                - unconditional_penalty
                - 40 * (context_is_bad and not cash_nearest)
            )
            if score > 0:
                results.append(("cash_flow_statement", score))
        return results

    chosen = set()
    role_candidates = {}
    split_balance_companions = {}
    for index, block in enumerate(blocks):
        for role, score in primary_statement_candidates(block, index):
            role_candidates.setdefault(role, []).append(
                (score, -index, -len(block.get("text", "")), block["id"])
            )
        if block.get("kind") != "table" or block.get("requires_table_review"):
            continue
        labels = [
            row.get("text", "").split("|", 1)[0].strip().casefold()
            for row in block.get("rows", [])
            if isinstance(row, dict)
        ]
        if not any(re.fullmatch(r"assets:?", label) for label in labels) or not any(
            re.match(r"total assets(?:\W|$)", label) for label in labels
        ):
            continue
        if any(re.match(r"total liabilities(?:\W|$)", label) for label in labels):
            continue
        table_count = 0
        for companion in blocks[index + 1 : index + 10]:
            if companion.get("kind") != "table" or companion.get(
                "requires_table_review"
            ):
                continue
            table_count += 1
            if table_count > 3:
                break
            companion_labels = [
                row.get("text", "").split("|", 1)[0].strip().casefold()
                for row in companion.get("rows", [])
                if isinstance(row, dict)
            ]
            if any("liabilities" in label for label in companion_labels) and any(
                re.match(r"total liabilities(?:\W|$)", label)
                for label in companion_labels
            ):
                role_candidates.setdefault("balance_sheet", []).append(
                    (15, -index, -len(block.get("text", "")), block["id"])
                )
                split_balance_companions[block["id"]] = companion["id"]
                break
    chosen = set()
    for role, candidates in role_candidates.items():
        roles[role] = max(candidates)[-1]
        chosen.add(roles[role])
    if roles.get("balance_sheet") in split_balance_companions:
        chosen.add(split_balance_companions[roles["balance_sheet"]])

    employee_candidates = []
    for index, block in enumerate(blocks):
        if block.get("requires_table_review"):
            continue
        visible = _visible_block_text(block)
        folded = visible.casefold()
        direct_count = re.search(
            r"\b(?:had|have|has|employ(?:ed|s)?)\b\s*"
            r"(?:approximately|about|more than|a total of)?\s*"
            r"\d[\d,]{2,}\s+"
            r"(?:(?:full.time(?: equivalent)?|part.time|temporary|seasonal|"
            r"global|worldwide)(?:\s*,\s*|\s+and\s+|\s+))*"
            r"(?:employees?|people|colleagues|associates|team members?|"
            r"teammates?|staff|personnel|individuals|coworkers)\b|"
            r"(?:each of )?(?:our|the company.?s)\s+"
            r"(?:approximately|about|more than)?\s*\d[\d,]{2,}\s+"
            r"(?:(?:full.|part.time|global|worldwide|and)\s+)*"
            r"(?:employees?|people|colleagues|associates|team members?|"
            r"teammates?|staff|personnel)\b|"
            r"\b(?:workforce|employee count|coworkers|teammates?|staff|personnel)\b\s*"
            r"(?:was|of|totaled|:)?\s*"
            r"(?:approximately|about|more than)?\s*\d[\d,]{2,}",
            folded,
        )
        if (
            block.get("kind") != "table"
            and direct_count
            and "named executive" not in folded
        ):
            score = (
                2
                + int(
                    "as of" in folded
                    or "at the end of" in folded
                    or bool(re.search(r"\bat [a-z]+ \d{1,2}, 20\d{2}\b", folded))
                )
                + 3
                * int(
                    bool(
                        re.search(
                            r"\b(?:globally|worldwide|across the globe|"
                            r"countries|we (?:had|have|employed)|"
                            r"company and its subsidiaries|our workforce)\b",
                            folded,
                        )
                    )
                )
                - 2 * int("segment" in folded)
                - 3 * int("subsidiary" in folded or " in ukraine" in folded)
            )
            employee_candidates.append((score, len(visible), -index, block["id"]))
    if employee_candidates:
        # Preserve several explicit counts because a filing can state both a
        # segment count and its consolidated workforce. Source-aware review
        # needs both scopes to choose the consolidated value reliably.
        chosen.update(
            candidate[-1]
            for candidate in sorted(employee_candidates, reverse=True)[:6]
        )
    employee_table_candidates = []
    for index, block in enumerate(blocks):
        if block.get("kind") != "table" or block.get("requires_table_review"):
            continue
        rows = [
            row.get("text", "")
            for row in block.get("rows", [])
            if isinstance(row, dict)
        ]
        header = " ".join(rows[:4]).casefold().replace("ofemployees", "of employees")
        if not re.search(
            r"(?:employees?|associates|teammates?|staff|personnel) "
            r"(?:as of|count)|(?:employee|associate|teammate).?count|"
            r"number of (?:employees?|associates|teammates?|staff|personnel)|"
            r"(?:full.time|part.time) (?:employees?|associates|teammates?)",
            header,
        ):
            continue
        if not any(re.search(r"\d[\d,]{2,}", row) for row in rows[1:]):
            continue
        context = block.get("context", "").casefold()
        score = 2 + int("as of" in header or "as of" in context)
        for prior in blocks[max(0, index - 3) : index]:
            prior_text = _visible_block_text(prior).casefold()
            if re.search(
                r"number of (?:employees?|associates|teammates?|staff|personnel)"
                r".{0,80}(?:as follows|following)",
                prior_text,
            ):
                score += 6
                chosen.add(prior["id"])
        employee_table_candidates.append(
            (score, -len(block.get("text", "")), -index, block["id"])
        )
    if employee_table_candidates:
        chosen.update(
            candidate[-1]
            for candidate in sorted(employee_table_candidates, reverse=True)[:4]
        )

    balance_ids = {roles.get("balance_sheet")}
    companion_id = split_balance_companions.get(roles.get("balance_sheet"))
    if companion_id is not None:
        balance_ids.add(companion_id)
    balance_blocks = [block for block in blocks if block["id"] in balance_ids]
    balance_debt_labels = []
    for balance in balance_blocks:
        balance_debt_labels.extend(
            [
            row.get("text", "").split("|", 1)[0].strip().casefold()
            for row in balance.get("rows", [])
            if isinstance(row, dict)
            and re.search(
                r"debt|borrowings?|commercial paper|notes? payable",
                row.get("text", "").split("|", 1)[0],
                re.IGNORECASE,
            )
            ]
        )
    debt_is_sufficient = len(set(balance_debt_labels)) >= 2 or any(
        re.search(r"total (?:debt|borrowings?)", label)
        for label in balance_debt_labels
    )
    if not debt_is_sufficient:
        debt_candidates = []
        for index, block in enumerate(blocks):
            if block.get("requires_table_review"):
                continue
            context = block.get("context", "").casefold()
            visible = _visible_block_text(block)
            folded = visible.casefold()
            if not re.search(r"\d", visible) or not re.search(
                r"\bdebt\b|\bborrowings?\b|commercial paper|notes? payable",
                folded,
            ):
                continue
            labels = [
                row.get("text", "").split("|", 1)[0].casefold()
                for row in block.get("rows", [])
                if isinstance(row, dict)
            ]
            score = sum(
                bool(
                    re.search(
                        r"(?:total|short.term|long.term|non.current|current portion|"
                        r"term) (?:borrowings?|(?:term )?debt)|commercial paper|"
                        r"notes? payable",
                        label,
                    )
                    and "debt securit" not in label
                )
                for label in labels
            )
            if re.search(r"\b(?:note\s+\d+[^|]{0,60})?debt\b|commercial paper", context):
                score += 2
            if score:
                debt_candidates.append((score, -len(visible), -index, block["id"]))
        if debt_candidates:
            chosen.add(max(debt_candidates)[-1])

    cash_flow = next(
        (
            block
            for block in blocks
            if block["id"] == roles.get("cash_flow_statement")
        ),
        None,
    )
    capex_row = re.compile(
        r"(?:purchases?|payments?|additions?).{0,35}"
        r"(?:property(?:, plant)?(?: and| &) equipment|capital expenditures?)",
        re.IGNORECASE,
    )
    cash_flow_has_capex = cash_flow is not None and any(
        capex_row.search(row.get("text", "").split("|", 1)[0])
        for row in cash_flow.get("rows", [])
        if isinstance(row, dict)
    )
    if not cash_flow_has_capex:
        capex_candidates = []
        for index, block in enumerate(blocks):
            if block.get("kind") != "table" or block.get("requires_table_review"):
                continue
            if any(
                capex_row.search(row.get("text", "").split("|", 1)[0])
                and re.search(r"\d|—|–", row.get("text", ""))
                for row in block.get("rows", [])
                if isinstance(row, dict)
            ):
                capex_candidates.append(
                    (-len(_visible_block_text(block)), -index, block["id"])
                )
        if capex_candidates:
            chosen.add(max(capex_candidates)[-1])

    green_candidates = []
    for index, block in enumerate(blocks):
        if block.get("kind") == "table" or block.get("requires_table_review"):
            continue
        visible = _visible_block_text(block)
        folded = visible.casefold()
        if (
            re.search(r"green|climate|transition|decarbon|renewable|sustainab", folded)
            and re.search(r"capex|capital expenditures?", folded)
            and re.search(r"\d", visible)
            and re.search(r"invested|spent|incurred|expenditures? (?:were|of|total)", folded)
        ):
            green_candidates.append((-len(visible), -index, block["id"]))
    if green_candidates:
        chosen.add(max(green_candidates)[-1])

    selected = dict(full)
    selected["blocks"] = [block for block in blocks if block["id"] in chosen]
    selected["selection"] = {
        "version": SELECTION_VERSION,
        "total_blocks": len(full["blocks"]),
        "selected_blocks": len(selected["blocks"]),
        "is_exhaustive": False,
        "primary_statement_roles": sorted(roles),
    }
    return full, selected


def _visible_block_text(block):
    """Return normalized visible text, including exact semantic table rows."""
    rows = block.get("rows")
    if isinstance(rows, list) and rows:
        text = "\n".join(
            row.get("text", "") for row in rows if isinstance(row, dict)
        )
    else:
        text = re.sub(r"<[^>]+>", " ", str(block.get("text", "")))
    return " ".join(text.split())


def _source_fiscal_year_evidence(full, requested_year=None):
    """Verify the annual cover year even when a table splits label and date rows."""
    blocks = full.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Full source has no blocks for fiscal-year verification.")
    retained_cover = full.get("annual_report_cover")
    if isinstance(retained_cover, dict):
        quote = retained_cover.get("quote")
        block_id = retained_cover.get("block_id")
        match = _FISCAL_YEAR.search(quote) if isinstance(quote, str) else None
        known_blocks = {block.get("id") for block in blocks}
        if match is not None and block_id in known_blocks:
            year = int(match.group("year"))
            if retained_cover.get("fiscal_year") != year:
                raise ValueError("Retained annual-report cover year is inconsistent.")
            if requested_year is not None and year != requested_year:
                raise ValueError("Requested fiscal year disagrees with the report cover.")
            return {
                "block_id": block_id,
                "block_ids": [block_id],
                "row_id": None,
                "quote": quote,
                "basis": "deterministic_metadata",
                "source_role": "annual_report_cover",
                "fiscal_year": year,
            }
    candidates = []
    cover_blocks = blocks[:120]
    for index, block in enumerate(cover_blocks):
        visible = _visible_block_text(block)
        match = _FISCAL_YEAR.search(visible)
        block_ids = [block.get("id")]
        if match is None:
            adjacent = cover_blocks[index : index + 3]
            visible = " ".join(_visible_block_text(item) for item in adjacent)
            match = _FISCAL_YEAR.search(visible)
            block_ids = [item.get("id") for item in adjacent]
        if match is None:
            continue
        candidates.append(
            {
                "year": int(match.group("year")),
                "block_ids": [item for item in block_ids if isinstance(item, str)],
                "quote": match.group(0),
            }
        )
    years = {item["year"] for item in candidates}
    if not candidates or len(years) != 1:
        raise ValueError("Full source has no unique annual fiscal-year cover statement.")
    year = next(iter(years))
    if requested_year is not None and year != requested_year:
        raise ValueError("Requested fiscal year disagrees with the report cover.")
    proof = min(candidates, key=lambda item: (len(item["block_ids"]), len(item["quote"])))
    return {
        "block_id": proof["block_ids"][0],
        "block_ids": proof["block_ids"],
        "row_id": None,
        "quote": proof["quote"],
        "basis": "deterministic_metadata",
        "source_role": "annual_report_cover",
        "fiscal_year": year,
    }


def source_fiscal_year(full):
    """Read the fiscal year from exact annual-report cover evidence."""
    return _source_fiscal_year_evidence(full)["fiscal_year"]


def extraction_messages(selected, metadata):
    """Pass selected source blocks and context without expected test values."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "company": metadata,
                    "metric_definitions": list(METRIC_NAMES),
                    "source": render_financial_input(selected),
                },
                ensure_ascii=False,
            ),
        },
    ]


def _currency_scale_details(block, quote=None):
    """Read one unambiguous source scale and retain how it was established."""
    scale_by_word = {
        "billion": Decimal(1_000_000_000),
        "million": Decimal(1_000_000),
        "thousand": Decimal(1_000),
    }
    if isinstance(quote, str):
        matching_rows = [
            row
            for row in block.get("rows", [])
            if isinstance(row, dict) and row.get("text") == quote
        ]
        row_scales = {
            Decimal(10) ** fact["scale"]
            for row in matching_rows
            for fact in row.get("numeric_facts", [])
            if isinstance(fact, dict)
            and isinstance(fact.get("scale"), int)
            and -12 <= fact["scale"] <= 12
            and _is_currency_unit_reference(fact.get("unit_ref"))
        }
        if len(row_scales) == 1:
            return next(iter(row_scales)), {
                "basis": "inline_xbrl_cited_row",
                "source_rows": [],
            }
        matching_scale_evidence = [
            row.get("source_scale_evidence")
            for row in matching_rows
            if isinstance(row.get("source_scale_evidence"), dict)
        ]
        evidence_scales = {
            evidence.get("scale")
            for evidence in matching_scale_evidence
            if isinstance(evidence.get("scale"), int)
            and -12 <= evidence["scale"] <= 12
        }
        if len(evidence_scales) == 1:
            evidence = matching_scale_evidence[0]
            return Decimal(10) ** next(iter(evidence_scales)), {
                "basis": evidence.get("basis"),
                "source_rows": evidence.get("source_rows", []),
                "matching_values": evidence.get("matching_values", []),
            }
    for source in (block.get("text", ""), block.get("context", "")):
        declarations = re.findall(
            r"\bin\s+(?:[$€£¥]\s*)?(billions?|millions?|thousands?)\b",
            source,
            re.IGNORECASE,
        )
        declarations.extend(
            re.findall(
                r"\(\s*(?:dollars?\s+in\s+)?"
                r"(billions?|millions?|thousands?)\b",
                source,
                re.IGNORECASE,
            )
        )
        scales = {scale_by_word[word.casefold().removesuffix("s")] for word in declarations}
        if len(scales) > 1:
            if re.search(r"\bin\s+\w+s?\b.{0,100}\bexcept\b", source, re.IGNORECASE):
                return scale_by_word[declarations[0].casefold().removesuffix("s")], {
                    "basis": "visible_source_scale_declaration",
                    "source_rows": [],
                }
            raise ValueError("Cited source has multiple incompatible currency scales.")
        if scales:
            return next(iter(scales)), {
                "basis": "visible_source_scale_declaration",
                "source_rows": [],
            }
    return Decimal(1), {"basis": "unscaled_visible_number", "source_rows": []}


def _currency_scale(block, quote=None):
    """Return only the multiplier for callers that do not need provenance."""
    return _currency_scale_details(block, quote)[0]


def _is_currency_unit_reference(unit_reference):
    """Exclude explicit share, ratio and percent units from monetary fact checks."""
    if not isinstance(unit_reference, str) or not unit_reference.strip():
        return False
    normalized = unit_reference.casefold()
    return not any(
        term in normalized
        for term in ("share", "pure", "percent", "number", "employee")
    )


def _inline_fact_support(block, quote, value, *, allow_magnitude=False):
    """Match a base-unit value to one Inline XBRL fact in the cited source row."""
    for row in block.get("rows", []):
        if not isinstance(row, dict) or row.get("text") != quote:
            continue
        for fact in row.get("numeric_facts", []):
            if (
                not isinstance(fact, dict)
                or not isinstance(fact.get("scale"), int)
                or not -12 <= fact["scale"] <= 12
                or not _is_currency_unit_reference(fact.get("unit_ref"))
            ):
                continue
            scale = Decimal(10) ** fact["scale"]
            for reported in _reported_numbers(str(fact.get("text", ""))):
                if fact.get("sign") == "-" and reported > 0:
                    reported = -reported
                base_value = reported * scale
                if base_value == Decimal(value) or (
                    allow_magnitude and abs(base_value) == abs(Decimal(value))
                ):
                    return True, scale, {
                        "basis": "inline_xbrl_cited_row",
                        "source_rows": [],
                    }
    return False, None, None


def _integer(value, field, *, nonnegative=False):
    """Return a JSON integer without accepting booleans or rounded floats."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer in base units.")
    if nonnegative and value < 0:
        raise ValueError(f"{field} must be nonnegative.")
    return value


def _quote_supports_value(name, value, block, quote):
    """Check one reported base-unit value against its exact source quote."""
    if name != "employees":
        supported, scale, scale_evidence = _inline_fact_support(
            block, quote, value, allow_magnitude=name == "total_capex"
        )
        if supported:
            return True, scale, scale_evidence
    if name == "employees":
        scale = Decimal(1)
        scale_evidence = {"basis": "employee_count", "source_rows": []}
    else:
        scale, scale_evidence = _currency_scale_details(block, quote)
    expected = Decimal(value) / scale
    numbers = _reported_numbers(quote)
    supported = expected in numbers or (
        name == "total_capex" and any(abs(number) == abs(expected) for number in numbers)
    )
    if expected == 0 and not supported:
        supported = bool(
            {"—", "–", "-"} & {cell.strip() for cell in quote.split("|")}
        )
    return supported, scale, scale_evidence


def _debt_component_label(quote):
    """Return the row label used to assess debt-component meaning."""
    return quote.split("|", 1)[0].strip().casefold()


def _canonicalize_citation(citation, blocks):
    """Repair the observed model mistake of putting a unique row ID in block_id."""
    if not isinstance(citation, dict):
        return citation
    block_id = citation.get("block_id")
    row_id = citation.get("row_id")
    if block_id != row_id or not isinstance(row_id, str) or block_id in blocks:
        return citation
    containing = [
        candidate_id
        for candidate_id, block in blocks.items()
        if any(
            isinstance(row, dict) and row.get("id") == row_id
            for row in block.get("rows", [])
        )
    ]
    if len(containing) != 1:
        return citation
    return {**citation, "block_id": containing[0]}


def _validate_debt_component_labels(labels):
    """Reject obvious non-debt and overlapping component combinations."""
    for label in labels:
        if any(term in label for term in _FORBIDDEN_DEBT_COMPONENTS):
            raise ValueError(f"total_debt: forbidden component label: {label}")
        if not any(term in label for term in _ALLOWED_DEBT_COMPONENTS):
            raise ValueError(f"total_debt: unsupported component label: {label}")
    if any(
        term in label
        for label in labels
        for term in ("total debt", "total borrowings", "total interest-bearing")
    ):
        raise ValueError("total_debt: a reported total cannot be added to components.")
    for total_label, family in (
        ("total term debt", "term debt"),
        ("total long-term debt", "long-term debt"),
    ):
        if any(total_label in label for label in labels) and sum(
            family in label for label in labels
        ) > 1:
            raise ValueError("total_debt: total and component debt rows overlap.")


def _calculated_total_debt(metric, blocks, year, currency):
    """Verify two or three cited debt operands and their exact Decimal sum."""
    value = _integer(metric["value"], "total_debt.value", nonnegative=True)
    citations = metric["citations"]
    if not isinstance(citations, list) or len(citations) not in {2, 3}:
        raise ValueError("total_debt: calculated value requires two or three operands.")
    operands = []
    identities = set()
    for index, citation in enumerate(citations):
        citation = _canonicalize_citation(citation, blocks)
        if not isinstance(citation, dict) or set(citation) != {
            "block_id",
            "row_id",
            "component_value",
        }:
            raise ValueError(
                "total_debt: each calculated citation requires block_id, row_id, "
                "and component_value."
            )
        identity = (citation["block_id"], citation["row_id"])
        if identity in identities:
            raise ValueError("total_debt: duplicate component citation.")
        identities.add(identity)
        component = _integer(
            citation["component_value"],
            f"total_debt.citations[{index}].component_value",
            nonnegative=True,
        )
        cited = _hydrate_citation("total_debt", citation, blocks)
        block = blocks[cited["block_id"]]
        if not re.search(rf"\b{year}\b", block.get("context", "") + block["text"]):
            raise ValueError("total_debt: component source does not show the fiscal year.")
        supported, scale, scale_evidence = _quote_supports_value(
            "total_debt", component, block, cited["quote"]
        )
        if not supported:
            raise ValueError(
                "total_debt: component quote does not support its base-unit value."
            )
        operands.append(
            {
                **cited,
                "component_value": component,
                "unit": currency,
                "source_scale": int(scale),
                "source_scale_evidence": scale_evidence,
                "label": _debt_component_label(cited["quote"]),
            }
        )
    _validate_debt_component_labels([item["label"] for item in operands])
    if sum(Decimal(item["component_value"]) for item in operands) != Decimal(value):
        raise ValueError("total_debt: value does not equal the exact component sum.")
    references = ", ".join(
        f"{item['block_id']}:{item['row_id'] or 'block'}" for item in operands
    )
    values = " + ".join(str(item["component_value"]) for item in operands)
    formula_note = f"Verified total_debt calculation in {currency}: {values} = {value}."
    return (
        value,
        operands,
        f"Calculated from {references}",
        " ".join(item for item in [metric["notes"], formula_note] if item),
    )


def _response_preflight_errors(result, blocks):
    """Collect independent semantic contract errors before field validation stops."""
    errors = []
    operating_income = result["metrics"]["operating_income"]
    if isinstance(operating_income, dict) and operating_income.get("value") is not None:
        cited_text = ""
        citations = operating_income.get("citations")
        if isinstance(citations, list) and len(citations) == 1:
            try:
                citation = _canonicalize_citation(citations[0], blocks)
                cited_text = _hydrate_citation(
                    "operating_income", citation, blocks
                )["quote"]
            except (TypeError, ValueError):
                pass
        source_label = cited_text.split("|", 1)[0]
        invalid_measure = (
            r"pre[ -]?provision|pre[ -]?tax|"
            r"(?:income|earnings|profit) before (?:income )?tax|"
            r"net (?:income|earnings|profit)"
        )
        source_is_surrogate = bool(
            re.search(invalid_measure, source_label, re.IGNORECASE)
        )
        source_is_operating_income = bool(
            re.search(
                r"operating (?:income|profit|loss)|(?:income|loss) from operations",
                source_label,
                re.IGNORECASE,
            )
        )
        notes = operating_income.get("notes")
        notes_describe_surrogate = not source_is_operating_income and isinstance(
            notes, str
        ) and bool(
            re.search(
                rf"(?:{invalid_measure}).{{0,100}}(?:used|treated|serves|closest|proxy)"
                rf"|(?:used|treated|serves|closest|proxy).{{0,100}}(?:{invalid_measure})",
                notes,
                re.IGNORECASE,
            )
        )
        if source_is_surrogate or notes_describe_surrogate:
            errors.append(
                "operating_income cannot use pre-provision profit, pretax income, "
                "net income, or another surrogate"
            )

    cash = result["metrics"]["cash_and_equivalents"]
    if isinstance(cash, dict) and cash.get("value") is not None:
        citations = cash.get("citations")
        if cash.get("status") != "reported" or not isinstance(citations, list) or len(citations) != 1:
            errors.append(
                "cash_and_equivalents requires one explicitly reported aggregate row; "
                "non-debt calculations are forbidden"
            )
    return errors


def validate_financial_response(content, full, selected, metadata):
    """Validate metric values, one debt calculation, and source references."""
    result = json.loads(content)
    if not isinstance(result, dict) or set(result) != {"currency", "metrics"}:
        raise ValueError("Response must contain exactly currency and metrics.")
    if not isinstance(result["metrics"], dict) or set(result["metrics"]) != set(
        METRIC_NAMES
    ):
        raise ValueError("Response must contain all nine metric names exactly once.")
    year = metadata["fiscal_year"]
    year_evidence = _source_fiscal_year_evidence(full, year)
    blocks = {block["id"]: block for block in selected["blocks"]}
    preflight_errors = _response_preflight_errors(result, blocks)
    if preflight_errors:
        raise ValueError(
            "Financial response contract errors: " + "; ".join(preflight_errors)
        )
    data = {
        key: metadata[key] for key in ("company_id", "company_name", "fiscal_year")
    }
    data["currency"] = result["currency"]
    evidence = {}
    for name, metric in result["metrics"].items():
        if not isinstance(metric, dict) or set(metric) != {
            "value",
            "status",
            "confidence",
            "notes",
            "citations",
        }:
            raise ValueError(f"{name}: metric keys do not match the extraction contract.")
        status = metric["status"]
        citations = metric["citations"]
        if not isinstance(citations, list):
            raise TypeError(f"{name}: citations must be a list.")
        section = None
        page = None
        notes = metric["notes"]
        value = metric["value"]
        if status == "calculated":
            if name != "total_debt":
                raise ValueError("Calculated status is allowed only for total_debt.")
            value, operands, section, notes = _calculated_total_debt(
                metric, blocks, year, result["currency"]
            )
            evidence[name] = {
                "basis": "verified_component_sum",
                "formula": "sum_non_overlapping_interest_bearing_borrowings",
                "value": value,
                "unit": result["currency"],
                "operands": operands,
            }
        elif value is not None:
            value = _integer(value, f"{name}.value")
            if status != "reported" or len(citations) != 1:
                raise ValueError(
                    f"{name}: reported values require exactly one source citation."
                )
            if not isinstance(citations[0], dict) or set(citations[0]) != {
                "block_id",
                "row_id",
            }:
                raise ValueError(
                    f"{name}: reported citation requires only block_id and row_id."
                )
            cited = _hydrate_citation(
                name, _canonicalize_citation(citations[0], blocks), blocks
            )
            block = blocks[cited["block_id"]]
            supported, scale, scale_evidence = _quote_supports_value(
                name, value, block, cited["quote"]
            )
            if not supported:
                raise ValueError(
                    f"{name}: citation {cited['block_id']}:{cited['row_id'] or 'block'} "
                    f"does not support base-unit value {value}; exact source quote: "
                    f"{json.dumps(cited['quote'][:300], ensure_ascii=False)}"
                )
            evidence[name] = {
                **cited,
                "source_scale": int(scale),
                "source_scale_evidence": scale_evidence,
            }
            section = f"Source block {cited['block_id']}" + (
                f", row {cited['row_id']}" if cited["row_id"] else ""
            )
            match = re.fullmatch(r"page (\d+)", block.get("location", ""))
            page = int(match[1]) if match else None
        elif citations:
            raise ValueError(f"{name}: unavailable values cannot claim citations.")
        data[name] = {
            "value": value,
            "unit": (
                "employees" if name == "employees" else result["currency"]
            )
            if value is not None
            else None,
            "status": status,
            "fiscal_year": year,
            "source_document": metadata["source_document"],
            "source_url": metadata["source_url"],
            "source_page": page,
            "source_section": section,
            "confidence": metric["confidence"],
            "notes": notes,
        }
    parsed = VERDEXFinancialData.model_validate(data)
    return parsed.model_dump(mode="json"), {
        "metrics": evidence,
        "fiscal_year": year_evidence,
    }


def review_messages(selected, metadata, candidates, evidence):
    """Review only hydrated citations and their exact containing source blocks."""
    if not isinstance(evidence, dict) or set(evidence) != {"metrics", "fiscal_year"}:
        raise ValueError("Verified financial evidence is required for semantic review.")
    metric_evidence = evidence["metrics"]
    if not isinstance(metric_evidence, dict):
        raise TypeError("Verified metric evidence must be an object.")
    cited_ids = set()
    for item in metric_evidence.values():
        if not isinstance(item, dict):
            continue
        references = item.get("operands", [item])
        if not isinstance(references, list):
            raise TypeError("Verified debt operands must be a list.")
        for reference in references:
            if isinstance(reference, dict) and isinstance(
                reference.get("block_id"), str
            ):
                cited_ids.add(reference["block_id"])
    cited = {**selected}
    cited["blocks"] = [
        block for block in selected["blocks"] if block["id"] in cited_ids
    ]
    if len(cited["blocks"]) != len(cited_ids):
        raise ValueError("Verified evidence refers to a missing selected source block.")
    return [
        {"role": "system", "content": REVIEW_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "company": metadata,
                    "candidates": candidates,
                    "fiscal_year_cover_evidence": evidence["fiscal_year"],
                    "verified_metric_evidence": metric_evidence,
                    "cited_source_blocks": render_financial_input(cited),
                },
                ensure_ascii=False,
            ),
        },
    ]


def validate_review(content):
    """Reject missing, duplicate or negative semantic judgments."""
    result = json.loads(content)
    if (
        not isinstance(result, dict)
        or set(result) != {"checks"}
        or not isinstance(result["checks"], list)
    ):
        raise ValueError("Semantic review must contain a checks list.")
    checks = result["checks"]
    if sorted(check.get("metric", "") for check in checks) != sorted(METRIC_NAMES):
        raise ValueError("Semantic review must cover each metric exactly once.")
    rejected = []
    for check in checks:
        if (
            set(check) != {"metric", "accepted", "reason"}
            or type(check["accepted"]) is not bool
            or not isinstance(check["reason"], str)
        ):
            raise ValueError("Semantic review check has invalid fields.")
        if not check["accepted"]:
            rejected.append(check)
    if rejected:
        raise ValueError(
            "Semantic review rejected metrics: "
            + json.dumps(rejected, ensure_ascii=False)
        )
    return checks
