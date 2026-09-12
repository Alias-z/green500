from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator


# ============================================================
# VERDEX — FINANCIAL TARGETS / GUIDANCE EXTRACTION SCHEMA
# ============================================================


TargetStatus = Literal[
    "announced",
    "on_track",
    "ahead_of_target",
    "behind_target",
    "achieved",
    "exceeded",
    "withdrawn",
    "revised",
    "not_assessable",
]


TargetType = Literal[
    "revenue_growth",
    "revenue",
    "operating_margin",
    "operating_income",
    "eps_growth",
    "eps",
    "roe",
    "roic",
    "free_cash_flow",
    "operating_cash_flow",
    "capex",
    "cost_savings",
    "capital_return",
    "debt_reduction",
    "leverage",
    "liquidity",
    "industry_specific",
    "other",
]


class FinancialTarget(BaseModel):
    """
    One forward-looking financial target, objective or
    management guidance item.

    IMPORTANT:
    Targets are stored separately from actual financial results.
    """

    metric_id: str

    target_name: str

    target_type: TargetType

    # --------------------------------
    # TARGET VALUE
    # --------------------------------

    target_value: Optional[float] = None

    target_min: Optional[float] = None

    target_max: Optional[float] = None

    unit: Optional[str] = None

    # Examples:
    # USD
    # percent
    # USD_per_share
    # ratio


    # --------------------------------
    # TARGET CHARACTER
    # --------------------------------

    target_direction: Optional[
        Literal[
            "at_least",
            "at_most",
            "approximately",
            "range",
            "increase",
            "decrease",
            "greater_than",
            "less_than",
        ]
    ] = None


    # --------------------------------
    # TIME
    # --------------------------------

    baseline_year: Optional[int] = None

    target_year: Optional[int] = None

    target_period: Optional[str] = None

    # Examples:
    # FY2026
    # 2025-2027
    # Q4 2026
    # long_term


    # --------------------------------
    # ACTUAL PERFORMANCE
    # --------------------------------

    actual_value: Optional[float] = None

    actual_year: Optional[int] = None

    actual_unit: Optional[str] = None


    # --------------------------------
    # PROGRESS
    # --------------------------------

    progress_pct: Optional[float] = None

    status: Optional[TargetStatus] = None


    # --------------------------------
    # TARGET CHANGES
    # --------------------------------

    revised: bool = False

    previous_target_value: Optional[float] = None

    previous_target_min: Optional[float] = None

    previous_target_max: Optional[float] = None

    revision_reason: Optional[str] = None


    # --------------------------------
    # SCOPE
    # --------------------------------

    business_scope: Optional[str] = None

    geographic_scope: Optional[str] = None


    # --------------------------------
    # SOURCE / PROVENANCE
    # --------------------------------

    source_document: str

    source_document_type: Literal[
        "10_k",
        "10_q",
        "annual_report",
        "earnings_release",
        "earnings_transcript",
        "investor_day",
        "investor_presentation",
        "other",
    ]

    source_url: Optional[str] = None

    source_page: Optional[int] = None

    source_section: Optional[str] = None

    disclosure_date: Optional[str] = None


    # --------------------------------
    # EVIDENCE QUALITY
    # --------------------------------

    confidence: float = Field(
        ge=0,
        le=1
    )

    notes: Optional[str] = None


    @model_validator(mode="after")
    def validate_target(self):

        # Target should have either:
        # single value OR range

        if (
            self.target_value is None
            and self.target_min is None
            and self.target_max is None
        ):
            raise ValueError(
                "Financial target must contain a target value or range."
            )

        return self


# ============================================================
# INDUSTRY-SPECIFIC FINANCIAL TARGET
# ============================================================

class IndustryFinancialTarget(BaseModel):
    """
    Financial target specific to an industry.

    Examples:

    Insurance:
        combined ratio
        underwriting income
        premium growth

    Banks:
        CET1
        efficiency ratio

    REITs:
        occupancy
        FFO

    Airlines:
        capacity / unit cost

    Technology:
        cloud growth
    """

    metric_name: str

    value: Optional[float] = None

    target_min: Optional[float] = None

    target_max: Optional[float] = None

    unit: str

    target_year: Optional[int] = None

    actual_value: Optional[float] = None

    actual_year: Optional[int] = None

    status: Optional[TargetStatus] = None

    source_document: str

    source_url: Optional[str] = None

    source_page: Optional[int] = None

    confidence: float = Field(
        ge=0,
        le=1
    )

    notes: Optional[str] = None


# ============================================================
# COMPLETE COMPANY FINANCIAL TARGET DATA
# ============================================================

class VERDEXFinancialTargets(BaseModel):
    """
    Forward-looking financial objectives for one company.

    These targets primarily support VERDEX Resilience,
    Efficiency and Transform Tomorrow analysis.

    They are NOT a standalone financial rating.
    """

    company_id: str

    company_name: str

    reporting_year: int


    # ========================================================
    # 1. GROWTH
    # ========================================================

    revenue_growth_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 2. PROFITABILITY
    # ========================================================

    operating_margin_targets: list[FinancialTarget] = Field(
        default_factory=list
    )

    operating_income_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 3. EARNINGS
    # ========================================================

    eps_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 4. CAPITAL RETURNS
    # ========================================================

    roe_targets: list[FinancialTarget] = Field(
        default_factory=list
    )

    roic_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 5. CASH GENERATION
    # ========================================================

    free_cash_flow_targets: list[FinancialTarget] = Field(
        default_factory=list
    )

    operating_cash_flow_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 6. INVESTMENT
    # ========================================================

    capex_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 7. COST / PRODUCTIVITY
    # ========================================================

    cost_savings_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 8. BALANCE SHEET / FINANCIAL RESILIENCE
    # ========================================================

    debt_reduction_targets: list[FinancialTarget] = Field(
        default_factory=list
    )

    leverage_targets: list[FinancialTarget] = Field(
        default_factory=list
    )

    liquidity_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 9. CAPITAL RETURN
    # ========================================================

    capital_return_targets: list[FinancialTarget] = Field(
        default_factory=list
    )


    # ========================================================
    # 10. INDUSTRY-SPECIFIC TARGETS
    # ========================================================

    industry_specific_targets: list[
        IndustryFinancialTarget
    ] = Field(
        default_factory=list
    )


# ============================================================
# CORE VERDEX FINANCIAL TARGET METRICS
# ============================================================

"""
PRIMARY TARGETS TO SEARCH FOR:

1. revenue_growth_target_pct

2. operating_margin_target_pct

3. eps_growth_target_pct

4. roe_target_pct

5. roic_target_pct

6. free_cash_flow_target_usd

7. operating_cash_flow_target_usd

8. capex_target_usd

9. cost_savings_target_usd

10. debt_reduction_target_usd

11. leverage_target

12. capital_return_target_usd


ALWAYS EXTRACT WITH:

13. target_year

14. baseline_year (when applicable)

15. target range

16. actual performance

17. target status

18. whether target was revised

19. previous target

20. source + date
"""


# ============================================================
# EXTRACTION RULES
# ============================================================

"""
1. TARGETS AND ACTUALS ARE DIFFERENT.

Example:

    target:
        ROE >= 10%

    actual:
        ROE = 11.1%

Never overwrite the target with the actual result.


2. PRESERVE RANGES.

Alphabet example:

    2026 CapEx guidance:
        $175B - $185B

Store:

    target_min = 175_000_000_000
    target_max = 185_000_000_000
    unit = "USD"

Do NOT convert this into:

    target = $180B

unless VERDEX later calculates the midpoint.


3. PRESERVE REVISIONS.

Example:

    previous CapEx guidance:
        $85B

    revised:
        $91B - $93B

Store BOTH.


4. NEVER INTERPRET MANAGEMENT LANGUAGE AS A NUMBER.

Example:

    "We expect strong growth"

is NOT:

    revenue_growth_target = 10%


5. DISTINGUISH GUIDANCE FROM LONG-TERM TARGETS.

Example:

    FY2026 revenue guidance

is different from:

    2025-2027 strategic ROE objective.


6. PRESERVE INDUSTRY-SPECIFIC TARGETS.

Insurance:
    combined ratio
    underwriting income
    premium growth

Banks:
    CET1 ratio
    efficiency ratio

REIT:
    FFO
    occupancy

Technology:
    Cloud growth
    AI CapEx


7. AI EXTRACTS TARGETS.

AI does NOT decide whether the company
is financially resilient.


8. DETERMINISTIC CODE CALCULATES:

    target achievement
    target gap
    historical growth
    margin improvement
    FCF conversion
    leverage
    financial capacity
    peer percentiles
    VERDEX resilience score
"""
