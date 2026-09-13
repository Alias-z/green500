from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ============================================================
# VERDEX — CLIMATE / EMISSIONS / TARGETS EXTRACTION SCHEMA
# ============================================================


MetricStatus = Literal[
    "reported",
    "calculated",
    "inferred",
    "not_disclosed",
    "not_applicable",
    "conflicting",
]


# ============================================================
# GENERIC CLIMATE METRIC
# ============================================================


class ClimateMetric(BaseModel):
    """
    A single quantitative climate observation extracted from
    an authoritative company disclosure.
    """

    value: float | None = None
    unit: str | None = None
    year: int

    status: MetricStatus

    geographic_scope: str | None = None
    organizational_boundary: str | None = None

    source_document: str
    source_url: str | None = None
    source_page: int | None = None
    source_section: str | None = None

    externally_assured: bool | None = None

    confidence: float = Field(
        ge=0, le=1, description="Extraction confidence from 0 to 1."
    )

    notes: str | None = None

    @model_validator(mode="after")
    def check_value(self):
        if self.status in {"reported", "calculated", "inferred"} and self.value is None:
            raise ValueError("Reported/calculated/inferred metrics must have a value.")

        if (
            self.status in {"not_disclosed", "not_applicable"}
            and self.value is not None
        ):
            raise ValueError("Missing/non-applicable metrics must not contain a value.")

        return self


# ============================================================
# GHG EMISSIONS
# ============================================================


class GHGEmissions(BaseModel):
    """
    Core greenhouse-gas inventory.

    All emissions should preferably be normalized to metric
    tonnes of CO2 equivalent (tCO2e).
    """

    # ----------------------------
    # CORE
    # ----------------------------

    scope_1_tco2e: ClimateMetric | None = None

    scope_2_market_tco2e: ClimateMetric | None = None

    scope_2_location_tco2e: ClimateMetric | None = None

    scope_3_tco2e: ClimateMetric | None = None

    # ----------------------------
    # OPTIONAL SCOPE 3 BREAKDOWN
    # ----------------------------

    scope_3_purchased_goods_tco2e: ClimateMetric | None = None

    scope_3_capital_goods_tco2e: ClimateMetric | None = None

    scope_3_fuel_energy_tco2e: ClimateMetric | None = None

    scope_3_upstream_transport_tco2e: ClimateMetric | None = None

    scope_3_waste_tco2e: ClimateMetric | None = None

    scope_3_business_travel_tco2e: ClimateMetric | None = None

    scope_3_employee_commuting_tco2e: ClimateMetric | None = None

    scope_3_downstream_transport_tco2e: ClimateMetric | None = None

    scope_3_use_of_sold_products_tco2e: ClimateMetric | None = None

    scope_3_end_of_life_tco2e: ClimateMetric | None = None

    scope_3_leased_assets_tco2e: ClimateMetric | None = None

    scope_3_investments_tco2e: ClimateMetric | None = None


# ============================================================
# CLIMATE INTENSITY
# ============================================================


class ClimateIntensity(BaseModel):
    """
    Reported or calculated GHG intensity.

    Examples:
    - tCO2e / $1M revenue
    - tCO2e / employee
    - kgCO2e / ALBD
    - gCO2e / passenger-km
    - tCO2e / tonne of production
    """

    value: float

    numerator_unit: str

    denominator_type: Literal[
        "revenue",
        "employee",
        "production",
        "passenger_distance",
        "floor_area",
        "energy_output",
        "available_lower_berth_day",
        "other",
    ]

    denominator_unit: str

    year: int

    status: MetricStatus

    source_document: str
    source_url: str | None = None
    source_page: int | None = None
    source_section: str | None = None

    externally_assured: bool | None = None

    confidence: float = Field(ge=0, le=1)

    notes: str | None = None


# ============================================================
# CLIMATE TARGET
# ============================================================


class ClimateTarget(BaseModel):
    """
    A disclosed climate / emissions / renewable-energy target.

    Targets are stored separately from actual performance.
    """

    target_name: str | None = None

    target_scope: Literal[
        "scope_1",
        "scope_2",
        "scope_1_2",
        "scope_3",
        "scope_1_2_3",
        "renewable_electricity",
        "carbon_neutrality",
        "net_zero",
        "supplier_engagement",
        "other",
    ]

    target_type: Literal[
        "absolute_reduction",
        "intensity_reduction",
        "renewable_energy",
        "carbon_neutrality",
        "net_zero",
        "supplier_engagement",
        "other",
    ]

    reduction_pct: float | None = Field(default=None, ge=0, le=100)

    baseline_year: int | None = None

    target_year: int

    # If intensity-based, preserve denominator
    intensity_denominator: str | None = None

    # Progress against target
    progress_pct: float | None = None

    achieved: bool | None = None

    achieved_year: int | None = None

    # Target credibility
    sbti_validated: bool | None = None

    source_document: str
    source_url: str | None = None
    source_page: int | None = None
    source_section: str | None = None

    confidence: float = Field(ge=0, le=1)

    notes: str | None = None


# ============================================================
# SECTOR-SPECIFIC CLIMATE METRIC
# ============================================================


class SectorClimateMetric(BaseModel):
    """
    Climate metric specific to an industry.

    Examples:
    - Cruise emissions / ALBD
    - Airline emissions / passenger-km
    - REIT emissions / square meter
    - Steel emissions / tonne steel
    - Cement emissions / tonne cement
    """

    metric_name: str

    value: float | None = None

    unit: str | None = None

    year: int

    status: MetricStatus

    source_document: str
    source_url: str | None = None
    source_page: int | None = None
    source_section: str | None = None

    confidence: float = Field(ge=0, le=1)

    notes: str | None = None


# ============================================================
# COMPLETE VERDEX CLIMATE DATA
# ============================================================


class VERDEXClimateData(BaseModel):
    """
    Complete climate extraction object for one company/year.

    IMPORTANT:
    This schema stores evidence.

    It does NOT calculate the final VERDEX score.

    Scoring, normalization, peer benchmarking, trends,
    target gaps and other calculations should be performed
    later using deterministic code.
    """

    # ========================================================
    # COMPANY
    # ========================================================

    company_id: str

    company_name: str

    reporting_year: int

    # ========================================================
    # 1. CURRENT CLIMATE IMPACT
    # ========================================================

    emissions: GHGEmissions

    # ========================================================
    # 2. CLIMATE / CARBON EFFICIENCY
    # ========================================================

    reported_intensities: list[ClimateIntensity] = Field(default_factory=list)

    renewable_electricity_pct: ClimateMetric | None = None

    # ========================================================
    # 3. HISTORICAL DIRECTION
    # ========================================================

    scope_1_2_change_pct: ClimateMetric | None = None

    scope_3_change_pct: ClimateMetric | None = None

    ghg_intensity_change_pct: ClimateMetric | None = None

    # ========================================================
    # 4. CLIMATE TARGETS
    # ========================================================

    climate_targets: list[ClimateTarget] = Field(default_factory=list)

    net_zero_target_year: int | None = None

    # ========================================================
    # 5. TARGET CREDIBILITY / EVIDENCE QUALITY
    # ========================================================

    sbti_validated: bool | None = None

    ghg_inventory_assured: bool | None = None

    assurance_provider: str | None = None

    assurance_level: Literal["limited", "reasonable", "other"] | None = None

    # ========================================================
    # 6. OFFSETS / CARBON REMOVALS
    # ========================================================

    carbon_offsets_tco2e: ClimateMetric | None = None

    carbon_removals_tco2e: ClimateMetric | None = None

    # ========================================================
    # 7. SCOPE 3 / SUPPLY CHAIN TRANSITION
    # ========================================================

    suppliers_with_science_based_targets_pct: ClimateMetric | None = None

    suppliers_engaged_pct: ClimateMetric | None = None

    # ========================================================
    # 8. SECTOR-SPECIFIC TRANSITION METRICS
    # ========================================================

    sector_specific_metrics: list[SectorClimateMetric] = Field(default_factory=list)


# ============================================================
# CORE VERDEX CLIMATE METRICS
# ============================================================

"""
PRIMARY METRICS TO EXTRACT:

CURRENT IMPACT
--------------
1. scope_1_tco2e
2. scope_2_market_tco2e
3. scope_2_location_tco2e
4. scope_3_tco2e

EFFICIENCY
----------
5. ghg_intensity
6. renewable_electricity_pct

DIRECTION
---------
7. scope_1_2_change_pct
8. scope_3_change_pct
9. ghg_intensity_change_pct

TARGET
------
10. target_reduction_pct
11. baseline_year
12. target_year
13. target_scope
14. target_type
15. target_progress_pct

CREDIBILITY
-----------
16. sbti_validated
17. net_zero_target_year
18. ghg_inventory_assured

TRANSITION
----------
19. carbon_offsets_tco2e
20. carbon_removals_tco2e
21. suppliers_with_science_based_targets_pct
22. suppliers_engaged_pct

SECTOR-SPECIFIC
---------------
23. sector-specific physical GHG intensity
24. sector-specific transition metrics
"""


# ============================================================
# IMPORTANT EXTRACTION RULES
# ============================================================

"""
1. NEVER convert missing disclosure into zero.

   Correct:
       value = None
       status = "not_disclosed"

   Incorrect:
       value = 0


2. NEVER combine Scope 2 market-based and location-based.

   Keep them as separate observations.


3. NEVER subtract carbon offsets/removals from gross emissions
   during extraction.

   Store separately:

       gross_emissions
       offsets
       removals


4. NEVER assume total Scope 3 if only some categories are
   disclosed.

   Only calculate Scope 3 total when the disclosed categories
   clearly represent the complete relevant inventory.


5. PRESERVE whether a target is:

       absolute
       intensity-based

   These are not equivalent.


6. PRESERVE the target baseline year.

   Example:

       reduction_pct = 80
       baseline_year = 2019
       target_year = 2030


7. PRESERVE the target scope.

   Example:

       Scope 1 + 2 target
       Scope 3 target

   must remain separate.


8. REPORTED intensity and VERDEX-calculated intensity must
   remain distinguishable.

   Example:

       Carnival reported:
           kgCO2e / ALBD

       VERDEX calculated:
           tCO2e / $1M revenue


9. SECTOR-SPECIFIC physical intensity should be retained when
   available.

   Examples:

       airlines:
           CO2e / passenger-km

       cruise:
           CO2e / ALBD

       real estate:
           CO2e / m2

       steel:
           CO2e / tonne steel


10. AI EXTRACTS EVIDENCE.

    AI should NOT generate the final climate score.

    Deterministic code should calculate:

        Scope 1 + 2 totals
        revenue intensity
        historical changes
        CAGR
        target progress
        target gaps
        peer percentiles
        VERDEX criterion scores
"""


# ============================================================
# EXAMPLES OF LATER DETERMINISTIC CALCULATIONS
# ============================================================

"""
scope_1_2_tco2e = (
    scope_1_tco2e
    + scope_2_market_tco2e
)


carbon_intensity_revenue = (
    scope_1_2_tco2e
    / (revenue_usd / 1_000_000)
)


emissions_change_pct = (
    (current_emissions - baseline_emissions)
    / baseline_emissions
) * 100


reduction_pct = (
    (baseline_emissions - current_emissions)
    / baseline_emissions
) * 100


target_gap_percentage_points = (
    actual_reduction_pct
    - target_reduction_pct
)


scope_3_share_pct = (
    scope_3_tco2e
    / (
        scope_1_tco2e
        + scope_2_market_tco2e
        + scope_3_tco2e
    )
) * 100
"""
