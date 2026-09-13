"""Validated records shared by Green500 machine-learning data adapters."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FeatureCategory = Literal[
    "financial",
    "social",
    "environmental",
    "climate_target",
    "financial_target",
    "fixed_context",
]
FeatureDataType = Literal["number", "integer", "boolean", "string"]
LabelTarget = Literal["esg", "csa"]

APPROVED_FEATURE_CATEGORIES = (
    "financial",
    "social",
    "environmental",
    "climate_target",
    "financial_target",
    "fixed_context",
)
LABEL_TARGETS = ("esg", "csa")


class StrictRecord(BaseModel):
    """Reject undeclared fields and non-finite numbers in persisted contracts."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class FeatureDefinition(StrictRecord):
    """Describe one model input independently of its observed availability."""

    feature_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_JSON_path: str = Field(min_length=1)
    data_type: FeatureDataType
    canonical_unit: str | None
    category_or_context: FeatureCategory
    validation_rules: list[str]
    derived_feature_dependencies: list[str]
    categorical: bool
    predictive: bool

    @field_validator("validation_rules")
    @classmethod
    def require_validation_rules(cls, value: list[str]) -> list[str]:
        """Require concrete validation rules for every feature."""
        if not value or any(not item.strip() for item in value):
            raise ValueError("Every feature needs at least one validation rule.")
        return value


class CompanyRecord(StrictRecord):
    """Keep company join identifiers separate from predictive columns."""

    company_cik: str = Field(pattern=r"^[0-9]{10}$")
    ticker: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    industry: str = Field(min_length=1)


class FeatureObservation(StrictRecord):
    """Represent one source-backed feature value before an assessment cutoff."""

    company_cik: str = Field(pattern=r"^[0-9]{10}$")
    feature_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: float | int | bool | str | None
    unit: str | None
    reporting_year: int | None = Field(default=None, ge=1900, le=2100)
    publication_date: date | None = None
    processed_at: datetime | None = None
    boundary: str | None = None
    status: str = Field(min_length=1)
    missing_reason: str | None = None
    qualification: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_document_id: int | None = Field(default=None, ge=1)
    source_url: str | None = None
    evidence: dict | None = None

    @field_validator("value")
    @classmethod
    def reject_nonfinite_observation(cls, value):
        """Keep NaN and infinity out of external JSON records."""
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Feature observations must contain finite numbers or null.")
        return value

    @field_validator("source_url")
    @classmethod
    def validate_optional_source_url(cls, value: str | None) -> str | None:
        """Accept only explicit public HTTP source references."""
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Feature source_url must be an HTTP(S) URL.")
        return value

    @model_validator(mode="after")
    def require_missing_reason_for_null(self):
        """Explain every absent observation without converting it to zero."""
        if self.value is None and not (self.missing_reason or "").strip():
            raise ValueError("A null feature observation needs a missing_reason.")
        return self


class LabelRecord(StrictRecord):
    """Describe one authorized official score for one assessment cycle."""

    company_cik: str = Field(pattern=r"^[0-9]{10}$")
    target_name: LabelTarget
    assessment_cycle: str = Field(min_length=1, max_length=100)
    prediction_as_of: date
    score: float = Field(ge=0, le=100)
    label_published_at: date
    source_name: str = Field(min_length=1, max_length=200)
    source_url: str
    source_date: date
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_reference: str = Field(min_length=1, max_length=500)

    @field_validator("assessment_cycle", "source_name", "authorization_reference")
    @classmethod
    def reject_blank_label_text(cls, value: str) -> str:
        """Reject whitespace placeholders in label provenance."""
        if not value.strip():
            raise ValueError("Label text fields must not be blank.")
        return value.strip()

    @field_validator("source_url")
    @classmethod
    def validate_label_source_url(cls, value: str) -> str:
        """Require a traceable HTTP source for every imported score."""
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Label source_url must be an HTTP(S) URL.")
        return value


def training_row_id(
    company_cik: str, assessment_cycle: str, prediction_as_of: date | str
) -> str:
    """Return the deterministic identifier for one company assessment row."""
    if re.fullmatch(r"[0-9]{10}", company_cik) is None:
        raise ValueError("company_cik must contain ten digits.")
    cycle = assessment_cycle.strip()
    if not cycle:
        raise ValueError("assessment_cycle must not be blank.")
    prediction_date = (
        prediction_as_of
        if isinstance(prediction_as_of, date)
        else date.fromisoformat(prediction_as_of)
    )
    source = f"{company_cik}|{cycle}|{prediction_date.isoformat()}"
    return hashlib.sha256(source.encode()).hexdigest()
