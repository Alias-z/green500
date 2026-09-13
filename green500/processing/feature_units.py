"""Explicit unit conversions for fixed features; unknown units stay missing."""

import math
import re


def _key(unit):
    value = str(unit or "").strip().replace("₂", "2").replace("³", "3")
    value = value.casefold()
    value = re.sub(r"^in\s+", "", value)
    value = re.sub(r"\bcarbon[\s-]+dioxide[\s-]+equivalents?\b", "co2e", value)
    value = re.sub(r"\bco2[\s-]*(?:equivalents?|eq)\b", "co2e", value)
    value = re.sub(r"\bco2[\s-]+e\b", "co2e", value)
    value = re.sub(r"\bof\s+(?=co2e\b)", "", value)
    value = re.sub(r"\b(million|thousand|billion)s?\s+of\s+", r"\1 ", value)
    return re.sub(r"[\s_·]+", "", value)


_FACTORS = {}


def _add(target, factor, *names):
    for name in names:
        _FACTORS[(target, _key(name))] = factor


for target in ("t", "tCO2e"):
    suffixes = ("", " CO2e") if target == "tCO2e" else ("",)
    for suffix in suffixes:
        _add(
            target,
            1,
            *[
                word + suffix
                for word in (
                    "t",
                    "mt",
                    "metric ton",
                    "metric tons",
                    "metric tonne",
                    "metric tonnes",
                    "tonne",
                    "tonnes",
                )
            ],
        )
        if target == "tCO2e":
            _add(
                target,
                1,
                "metric ton of CO2e",
                "metric tons of CO2e",
                "metric tonne of CO2e",
                "metric tonnes of CO2e",
            )
        _add(
            target,
            1_000,
            *[
                word + suffix
                for word in (
                    "kt",
                    "thousand t",
                    "thousand metric tons",
                    "thousand metric tonnes",
                    "thousand tonnes",
                )
            ],
        )
        _add(
            target,
            1_000_000,
            *[
                word + suffix
                for word in (
                    "MMT",
                    "million t",
                    "million metric tons",
                    "million metric tonnes",
                    "million tonnes",
                )
            ],
        )
        _add(target, 0.001, "kg" + suffix, "kilograms" + suffix)
        _add(target, 0.000001, "g" + suffix, "grams" + suffix)
        _add(target, 0.90718474, "short tons" + suffix, "US tons" + suffix)
        _add(target, 0.00045359237, "pounds" + suffix, "lb" + suffix, "lbs" + suffix)
_add("MWh", 1, "MWh", "megawatt hours", "megawatt-hours", "megawatt hours (MWh)")
_add("MWh", 0.001, "kWh", "kilowatt hours")
_add("MWh", 1_000, "GWh", "gigawatt hours")
_add("MWh", 1_000_000, "TWh")
_add("MWh", 1 / 3.6, "GJ", "gigajoules")
_add("MWh", 1_000_000 / 3.6, "million GJ", "million gigajoules")
_add("MWh", 1_000 / 3.6, "TJ", "terajoules")
_add("MWh", 1_000_000 / 3.6, "PJ", "petajoules")
_add("m3", 1, "m3", "cubic meters", "cubic metres", "kiloliters", "kilolitres", "kL")
_add("m3", 0.001, "L", "liters", "litres")
_add("m3", 1_000, "megaliters", "megalitres", "million liters", "million litres")
_add("m3", 1_000_000, "million m3", "million cubic meters", "million cubic metres")
_add("m3", 1_000_000, "billion liters", "billion litres")
_add("m3", 3.785411784, "kGal", "thousand US gallons", "thousand US gallon")
_add(
    "m3",
    3_785.411784,
    "million US gallons",
    "million US gallon",
)
_add("m3", 0.003785411784, "US gallons", "US gallon")
_add("m3", 0.00454609, "imperial gallons")
_add("ha", 1, "ha", "hectares", "hectare")
_add("ha", 0.40468564224, "acres", "acre")
_add("ha", 0.0001, "m2", "square meters", "square metres")
_add("ha", 100, "km2", "square kilometers")
_add("percent", 1, "%", "percent", "percentage", "% reduction", "percent reduction")
_add("percent", 100, "fraction", "ratio", "share")
_add(
    "count",
    1,
    "count",
    "number",
    "#",
    "people",
    "persons",
    "individual",
    "individuals",
    "employees",
    "global employees",
    "employees worldwide",
    "employees globally",
    "employees (approximate)",
    "full-time employees",
    "full time employees",
    "full-time equivalent employees",
    "full time equivalent employees",
    "headcount",
    "workers",
    "colleague",
    "colleagues",
    "team members",
    "associates",
    "suppliers",
    "cases",
    "incidents",
    "fatalities",
    "employee fatalities at work",
    "recalls",
    "breaches",
    "sites",
)
_add("count", 1_000, "thousand employees", "thousand people")
_add("year", 1, "year", "years", "calendar year", "fiscal year")
_add("ratio", 1, "ratio", "times", "x", "multiple")
_add("USD", 1, "USD", "US dollars", "U.S. dollars", "US$", "$US", "US $")
_add(
    "USD", 1_000, "thousand USD", "USD thousand", "USD thousands", "thousand US dollars"
)
_add(
    "USD", 1_000_000, "million USD", "USD million", "USD millions", "million US dollars"
)
_add(
    "USD",
    1_000_000_000,
    "billion USD",
    "USD billion",
    "USD billions",
    "billion US dollars",
)
_add(
    "USD_per_share",
    1,
    "USD_per_share",
    "USD/share",
    "USD per share",
    "US dollars per share",
)
_add("percentage_points", 1, "percentage points", "percentage point", "pp")
_add("percentage_points", 0.01, "basis points", "bps")


def convert(value, source_unit, target_unit):
    """Convert a finite number only when its unit has an explicit known meaning."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    source = str(source_unit or "").strip()
    if target_unit == "m3" and source == "ML":
        return value * 1_000
    if target_unit == "m3" and source == "ml":
        return value / 1_000_000
    # Mt can mean megatonnes or an informal metric-ton abbreviation in these sources.
    if re.match(r"^Mt(?:\b|CO)", source):
        return None
    if target_unit == "percent" and _key(source) == "share" and not 0 <= value <= 1:
        return None
    factor = _FACTORS.get((target_unit, _key(source)))
    if factor is None:
        return None
    result = value * factor
    return result if math.isfinite(result) else None
