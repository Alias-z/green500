"""Validate greenhouse-gas scope from the grounded source row label."""

import re

from green500.processing.source_quotes import normalize_source_text

_SCOPE_FIELDS = {
    "env_scope_1_tco2e": "scope_1",
    "climate_scope_1_tco2e": "scope_1",
    "env_scope_2_market_based_tco2e": "scope_2_market",
    "climate_scope_2_market_tco2e": "scope_2_market",
    "env_scope_2_location_based_tco2e": "scope_2_location",
    "climate_scope_2_location_tco2e": "scope_2_location",
    "env_scope_1_2_market_based_tco2e": "scope_1_2_market",
    "env_scope_3_total_tco2e": "scope_3_total",
    "climate_scope_3_tco2e": "scope_3_total",
}


def _has(text, scope):
    return re.search(rf"\bscope\s*{scope}\b", text) is not None


def _combined_scope_1_2(text):
    return re.search(
        r"\bscope\s*1\s*(?:&|\+|and)\s*(?:scope\s*)?2\b", text
    ) is not None


def validate_scope_field(field, source_quote, raw_value=None):
    """Reject totals and subsets whose grounded row names another GHG scope."""
    expected = _SCOPE_FIELDS.get(field)
    if expected is None:
        return
    text = normalize_source_text(source_quote)
    raw = normalize_source_text(raw_value).replace(",", "")
    if raw:
        matching_lines = [
            normalize_source_text(line)
            for line in str(source_quote).splitlines()
            if raw in normalize_source_text(line).replace(",", "")
        ]
        if len(matching_lines) == 1:
            text = matching_lines[0]
    combined = _combined_scope_1_2(text)
    if expected == "scope_1":
        if not _has(text, 1) or _has(text, 2) or combined:
            raise ValueError("The grounded row does not establish Scope 1 alone.")
        return
    if expected.startswith("scope_2"):
        if not _has(text, 2) or _has(text, 1) or combined:
            raise ValueError("The grounded row does not establish Scope 2 alone.")
        basis = expected.removeprefix("scope_2_")
        if basis not in text:
            raise ValueError(f"The grounded row does not establish {basis}-based Scope 2.")
        return
    if expected == "scope_1_2_market":
        if not combined or "market" not in text:
            raise ValueError(
                "The grounded row does not establish combined market-based Scope 1 and 2."
            )
        return
    if not _has(text, 3):
        raise ValueError("An overall emissions total cannot populate complete Scope 3.")
    if any(
        phrase in text
        for phrase in (
            "other scope 3",
            "selected scope 3",
            "partial scope 3",
            "scope 3 category",
            "scope 3 categories",
            "category 1",
            "category 2",
            "category 3",
            "category 4",
            "category 5",
            "category 6",
            "category 7",
            "category 8",
            "category 9",
            "category 10",
            "category 11",
            "category 12",
            "category 13",
            "category 14",
            "category 15",
        )
    ):
        raise ValueError(
            "A Scope 3 category or partial subtotal cannot populate complete Scope 3."
        )
