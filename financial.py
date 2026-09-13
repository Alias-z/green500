from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator


class FinancialMetric(BaseModel):
    """
    A single financial/context metric extracted from an authoritative
    company filing or public disclosure.
    """

    value: Optional[float] = None

    unit: Optional[str] = None

    status: Literal[
        "reported",
        "calculated",
        "not_disclosed",
        "not_applicable",
        "conflicting",
    ]

    fiscal_year: int

    source_document: str
    source_url: Optional[str] = None
    source_page: Optional[int] = None
    source_section: Optional[str] = None

    confidence: float = Field(
        ge=0,
        le=1,
        description="Extraction confidence from 0 to 1."
    )

    notes: Optional[str] = None

    @model_validator(mode="after")
    def validate_value_status(self):
        if self.status in {"reported", "calculated"} and self.value is None:
            raise ValueError(
                "Reported/calculated metrics must contain a value."
            )

        if self.status in {"not_disclosed", "not_applicable"}:
            if self.value is not None:
                raise ValueError(
                    "Missing/not-applicable metrics must not contain a value."
                )

        return self


class VERDEXFinancialData(BaseModel):
    """
    Financial/context data used by VERDEX.

    These are inputs to sustainability calculations and resilience
    analysis, NOT a standalone financial score.
    """

    company_id: str = Field(
        description="Ticker or canonical VERDEX company identifier."
    )

    company_name: str
    fiscal_year: int
    currency: str = "USD"

    # --------------------------------
    # CORE NORMALIZATION VARIABLES
    # --------------------------------

    revenue: FinancialMetric = Field(
        description=(
            "Total annual company revenue. "
            "Core denominator for environmental intensity calculations."
        )
    )

    employees: FinancialMetric = Field(
        description=(
            "Total employee/headcount figure. "
            "Used for per-employee social and workforce metrics."
        )
    )

    # --------------------------------
    # INVESTMENT / TRANSFORMATION
    # --------------------------------

    total_capex: Optional[FinancialMetric] = Field(
        default=None,
        description=(
            "Total capital expenditures. Context only. "
            "Must never be interpreted as green CapEx."
        )
    )

    green_transition_capex: Optional[FinancialMetric] = Field(
        default=None,
        description=(
            "Capital expenditure explicitly identified by the company "
            "as green, climate, transition, decarbonization, renewable, "
            "or otherwise sustainability-related."
        )
    )

    # --------------------------------
    # OPTIONAL RESILIENCE VARIABLES
    # --------------------------------

    operating_income: Optional[FinancialMetric] = None

    total_assets: Optional[FinancialMetric] = None

    cash_and_equivalents: Optional[FinancialMetric] = None

    total_debt: Optional[FinancialMetric] = None

    operating_cash_flow: Optional[FinancialMetric] = None
