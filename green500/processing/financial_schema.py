"""Strict schema for standardized company financial context data."""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FinancialMetricStatus = Literal[
    "reported",
    "calculated",
    "not_disclosed",
    "not_applicable",
    "conflicting",
]
FINANCIAL_METRIC_UNIT_METADATA = {
    "money": "The parent three-letter currency code; values use its base units.",
    "headcount": "employees; contractors are excluded.",
}

_CURRENCY = re.compile(r"^[A-Z]{3}$")


class FinancialMetric(BaseModel):
    """One financial or workforce metric from an authoritative disclosure."""

    model_config = ConfigDict(extra="forbid", strict=True)

    value: float | None = None
    unit: str | None = Field(
        default=None,
        description=(
            "Unit for the value. Money uses the parent currency code and workforce "
            "headcount uses employees."
        ),
        json_schema_extra={"allowed_units": FINANCIAL_METRIC_UNIT_METADATA},
    )
    status: FinancialMetricStatus
    fiscal_year: int = Field(ge=1900, le=2100)
    source_document: str = Field(min_length=1)
    source_url: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_section: str | None = None
    confidence: float = Field(
        ge=0,
        le=1,
        description="Extraction confidence from 0 to 1.",
    )
    notes: str | None = None

    @field_validator("value", "confidence")
    @classmethod
    def reject_nonfinite_numbers(cls, value: float | None) -> float | None:
        """Reject NaN and infinity before they can enter stored JSON."""
        if value is not None and not math.isfinite(value):
            raise ValueError("Numeric values must be finite.")
        return value

    @field_validator("source_document", "source_url", "source_section", "notes")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        """Keep optional text absent instead of accepting whitespace placeholders."""
        if value is not None and not value.strip():
            raise ValueError("Text values must not be blank.")
        return value

    @field_validator("source_url")
    @classmethod
    def require_public_url_shape(cls, value: str | None) -> str | None:
        """Require an explicit HTTP source URL when a URL is supplied."""
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("source_url must be an HTTP(S) URL.")
        return value

    @field_validator("unit")
    @classmethod
    def validate_unit_shape(cls, value: str | None) -> str | None:
        """Allow an uppercase currency code or the explicit employee-count unit."""
        if value is not None and value != "employees" and not _CURRENCY.fullmatch(value):
            raise ValueError("unit must be a three-letter currency code or employees.")
        return value

    @model_validator(mode="after")
    def validate_value_status(self) -> FinancialMetric:
        """Keep numeric and unavailable statuses unambiguous."""
        if self.status in {"reported", "calculated"}:
            if self.value is None or self.unit is None:
                raise ValueError(
                    "Reported/calculated metrics must contain a value and unit."
                )
        elif self.value is not None:
            raise ValueError(
                "Not-disclosed, not-applicable, and conflicting metrics must not "
                "contain a single value."
            )
        return self


class VERDEXFinancialData(BaseModel):
    """Nine standardized company metrics; missing values remain explicit."""

    model_config = ConfigDict(extra="forbid", strict=True)

    company_id: str = Field(
        min_length=1,
        description="Ticker or canonical VERDEX company identifier.",
    )
    company_name: str = Field(min_length=1)
    fiscal_year: int = Field(ge=1900, le=2100)
    currency: str = Field(
        default="USD",
        description=(
            "Three-letter uppercase currency code for every money metric; values "
            "use currency base units."
        ),
        json_schema_extra={"money_values": "currency_base_units"},
    )

    revenue: FinancialMetric = Field(
        description="Total annual company revenue in currency base units."
    )
    employees: FinancialMetric = Field(
        description="Total employee headcount, excluding contractors."
    )
    total_capex: FinancialMetric | None = Field(
        default=None,
        description=(
            "Total capital expenditures in currency base units. Context only; "
            "total capital expenditure is never green capital expenditure by default."
        ),
    )
    green_transition_capex: FinancialMetric | None = Field(
        default=None,
        description=(
            "Capital expenditure explicitly identified by the company as green, "
            "climate, transition, decarbonization, renewable, or sustainability-related."
        ),
    )
    operating_income: FinancialMetric | None = None
    total_assets: FinancialMetric | None = None
    cash_and_equivalents: FinancialMetric | None = None
    total_debt: FinancialMetric | None = None
    operating_cash_flow: FinancialMetric | None = None

    @field_validator("company_id", "company_name")
    @classmethod
    def reject_blank_identifiers(cls, value: str) -> str:
        """Reject identifiers that contain no visible characters."""
        if not value.strip():
            raise ValueError("Company identifiers must not be blank.")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        """Require the stable three-letter currency representation."""
        if not _CURRENCY.fullmatch(value):
            raise ValueError("currency must be a three-letter uppercase code.")
        return value

    @model_validator(mode="after")
    def validate_metric_years_and_units(self) -> VERDEXFinancialData:
        """Require one fiscal year and the field-specific base unit throughout."""
        for field_name in (
            "revenue",
            "employees",
            "total_capex",
            "green_transition_capex",
            "operating_income",
            "total_assets",
            "cash_and_equivalents",
            "total_debt",
            "operating_cash_flow",
        ):
            metric = getattr(self, field_name)
            if metric is None:
                continue
            if metric.fiscal_year != self.fiscal_year:
                raise ValueError(
                    f"{field_name}.fiscal_year must match the company fiscal_year."
                )
            expected_unit = "employees" if field_name == "employees" else self.currency
            if metric.unit is not None and metric.unit != expected_unit:
                raise ValueError(f"{field_name}.unit must be {expected_unit}.")
        return self
