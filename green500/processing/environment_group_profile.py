"""Small environment field groups for focused table extraction."""

from types import SimpleNamespace

from green500.processing.environment_features import FIELDS
from green500.processing.fixed_environment_profile import INSTRUCTIONS
from green500.processing.fixed_schema import build_model

GROUPS = {
    "emissions": (
        "env_total_ghg_tco2e",
        "env_scope_1_tco2e",
        "env_scope_2_location_based_tco2e",
        "env_scope_2_market_based_tco2e",
        "env_scope_3_total_tco2e",
    ),
    "resources": (
        "env_total_energy_mwh",
        "env_renewable_electricity_percent",
        "env_water_withdrawal_m3",
        "env_total_waste_tonnes",
        "env_waste_recycled_percent",
    ),
}

KEYWORDS = {
    "emissions": (
        "greenhouse gas",
        "ghg",
        "scope 1",
        "scope 2",
        "scope 3",
        "carbon emissions",
        "location-based",
        "market-based",
    ),
    "resources": (
        "energy consumption",
        "total energy",
        "renewable electricity",
        "water withdrawal",
        "total waste",
        "waste generated",
        "waste recycled",
        "recycling rate",
    ),
}


def environment_group_profile(group):
    """Return a closed schema and focused instructions for one field group."""
    if group not in GROUPS:
        raise ValueError("Unknown environment extraction group: " + group)
    fields = {field: FIELDS[field] for field in GROUPS[group]}
    instructions = (
        INSTRUCTIONS
        + "\n\nThis request covers only the "
        + group.replace("_", " ")
        + " field group. Extract every supported field in the supplied small schema. "
        "Read table headers, years, units and footnotes before copying a value."
    )
    return SimpleNamespace(
        FIELDS=fields,
        MODEL=build_model(
            "Environment" + group.title().replace("_", "") + "Extraction", fields
        ),
        INSTRUCTIONS=instructions,
        KEYWORDS=KEYWORDS[group],
    )
