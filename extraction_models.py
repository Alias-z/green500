"""Shared Pydantic types for structured report extraction."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class CompanyIdentity(ExtractionModel):
    name: str
    ticker: str


class SourceEvidence(ExtractionModel):
    block_id: int = Field(ge=0)
    quote: str = Field(min_length=1)
    raw_value: str = Field(min_length=1)
    source_unit: str
    scale_factor: float = Field(gt=0)


class ReportMetric(ExtractionModel):
    value: float
    unit: str
    reporting_year: int = Field(ge=1900, le=2100)
    status: Literal['reported', 'company_estimate', 'company_target']
    qualification: str
    confidence: float = Field(ge=0, le=1)
    evidence: SourceEvidence


class PhysicalMetric(ReportMetric):
    value: float = Field(ge=0)

    @model_validator(mode='after')
    def validate_percentage(self):
        if self.unit == 'percent' and self.value > 100:
            raise ValueError('Percentage must be between 0 and 100')
        return self


class Boundary(ExtractionModel):
    description: str


class ReportContext(ExtractionModel):
    reporting_year: int | None = Field(ge=1900, le=2100)
    boundary: Boundary
    currency: str | None
    limitations: list[str]


class AdditionalObservation(ExtractionModel):
    key: str = Field(pattern=r'^[a-z][a-z0-9_]*$')

    @model_validator(mode='after')
    def validate_key(self):
        if self.key in {'value', 'status', 'evidence', 'source'}:
            raise ValueError('Reserved metric key')
        return self
