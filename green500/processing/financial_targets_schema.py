"""Strict schema for forward-looking company financial targets and guidance."""

from typing import Literal

from pydantic import Field, model_validator

from green500.processing.report_schema_types import ExtractionModel

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

TargetDirection = Literal[
    "at_least",
    "at_most",
    "approximately",
    "range",
    "increase",
    "decrease",
    "greater_than",
    "less_than",
]

SourceDocumentType = Literal[
    "10_k",
    "10_q",
    "annual_report",
    "earnings_release",
    "earnings_transcript",
    "investor_day",
    "investor_presentation",
    "other",
]


class FinancialTarget(ExtractionModel):
    """One forward-looking financial target or management-guidance item."""

    metric_id: str
    target_name: str
    target_type: TargetType
    target_value: float | None = None
    target_min: float | None = None
    target_max: float | None = None
    unit: str | None = None
    target_direction: TargetDirection | None = None
    baseline_year: int | None = Field(default=None, ge=1900, le=2100)
    target_year: int | None = Field(default=None, ge=1900, le=2100)
    target_period: str | None = None
    actual_value: float | None = None
    actual_year: int | None = Field(default=None, ge=1900, le=2100)
    actual_unit: str | None = None
    progress_pct: float | None = None
    status: TargetStatus | None = None
    revised: bool = False
    previous_target_value: float | None = None
    previous_target_min: float | None = None
    previous_target_max: float | None = None
    revision_reason: str | None = None
    business_scope: str | None = None
    geographic_scope: str | None = None
    source_document: str
    source_document_type: SourceDocumentType
    source_url: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_section: str | None = None
    disclosure_date: str | None = None
    confidence: float = Field(ge=0, le=1)
    notes: str | None = None

    @model_validator(mode="after")
    def validate_target_value(self):
        """Require a point or range and reject an inverted complete range."""
        if (
            self.target_value is None
            and self.target_min is None
            and self.target_max is None
        ):
            raise ValueError("Financial target must contain a target value or range.")
        if (
            self.target_min is not None
            and self.target_max is not None
            and self.target_min > self.target_max
        ):
            raise ValueError("Financial target minimum exceeds its maximum.")
        return self


class IndustryFinancialTarget(ExtractionModel):
    """A target whose metric definition is specific to one industry."""

    metric_name: str
    value: float | None = None
    target_min: float | None = None
    target_max: float | None = None
    unit: str
    target_year: int | None = Field(default=None, ge=1900, le=2100)
    actual_value: float | None = None
    actual_year: int | None = Field(default=None, ge=1900, le=2100)
    status: TargetStatus | None = None
    source_document: str
    source_url: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    confidence: float = Field(ge=0, le=1)
    notes: str | None = None


class VERDEXFinancialTargets(ExtractionModel):
    """Forward-looking financial objectives for one company."""

    company_id: str
    company_name: str
    reporting_year: int = Field(ge=1900, le=2100)
    revenue_growth_targets: list[FinancialTarget] = Field(default_factory=list)
    operating_margin_targets: list[FinancialTarget] = Field(default_factory=list)
    operating_income_targets: list[FinancialTarget] = Field(default_factory=list)
    eps_targets: list[FinancialTarget] = Field(default_factory=list)
    roe_targets: list[FinancialTarget] = Field(default_factory=list)
    roic_targets: list[FinancialTarget] = Field(default_factory=list)
    free_cash_flow_targets: list[FinancialTarget] = Field(default_factory=list)
    operating_cash_flow_targets: list[FinancialTarget] = Field(default_factory=list)
    capex_targets: list[FinancialTarget] = Field(default_factory=list)
    cost_savings_targets: list[FinancialTarget] = Field(default_factory=list)
    debt_reduction_targets: list[FinancialTarget] = Field(default_factory=list)
    leverage_targets: list[FinancialTarget] = Field(default_factory=list)
    liquidity_targets: list[FinancialTarget] = Field(default_factory=list)
    capital_return_targets: list[FinancialTarget] = Field(default_factory=list)
    industry_specific_targets: list[IndustryFinancialTarget] = Field(
        default_factory=list
    )
