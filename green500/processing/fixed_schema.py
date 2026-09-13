"""Shared closed-schema output for comparable company-level model features."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Company(StrictRecord):
    name: str
    ticker: str


class FeatureEvidence(StrictRecord):
    block_id: int = Field(ge=0)
    quote: str = Field(min_length=1)
    raw_value: str | None
    source_unit: str | None
    scale_factor: float | None = Field(description="Positive canonical-unit conversion factor; negative is allowed only for explicitly disclosed signed percentage decreases.")


class FeatureMetadata(StrictRecord):
    status: Literal["reported", "company_estimate", "company_target", "not_disclosed", "not_extracted", "not_applicable", "conflicting"]
    reporting_year: int | None = Field(ge=1900, le=2100)
    reason: str | None
    qualification: str | None
    confidence: float | None = Field(ge=0, le=1)
    evidence: FeatureEvidence | None


class FixedExtraction(StrictRecord):
    company: Company
    reporting_year: int | None = Field(ge=1900, le=2100)
    boundary: str | None
    limitations: list[str]

    @model_validator(mode="after")
    def require_evidence_for_present_values(self):
        for name, value in self.values.model_dump().items():
            metadata = getattr(self.metadata, name)
            if value is None:
                continue
            if metadata is None or metadata.evidence is None:
                raise ValueError(f"{name}: a present value needs source evidence.")
            if metadata.status not in {"reported", "company_estimate", "company_target"}:
                raise ValueError(f"{name}: missing/conflicting status cannot carry a value.")
            if not isinstance(value, bool):
                evidence = metadata.evidence
                if evidence.raw_value is None or evidence.source_unit is None or evidence.scale_factor is None:
                    raise ValueError(f"{name}: numeric evidence needs raw_value, source_unit and scale_factor.")
                negative_scale_fields = (
                    "_change_pct",
                    "_reduction_pct",
                    "_progress_pct",
                )
                if evidence.scale_factor == 0 or (
                    evidence.scale_factor < 0
                    and not name.endswith(negative_scale_fields)
                ):
                    raise ValueError(
                        f"{name}: a negative conversion factor requires a signed percentage field."
                    )
        return self


def build_model(name, fields):
    """Create required, nullable columns with a fixed matching metadata record."""
    types = {"number": float, "integer": int, "boolean": bool}
    value_fields = {}
    for key, specification in fields.items():
        kind = types[specification["type"]]
        description = specification.get("description", key)
        if specification.get("unit") is not None:
            description += "; canonical unit: " + str(specification["unit"])
        value_fields[key] = (kind | None, Field(..., description=description))
    values = create_model(name + "Values", __base__=StrictRecord, **value_fields)
    metadata = create_model(name + "Metadata", __base__=StrictRecord,
                            **{key: (FeatureMetadata | None, ...) for key in fields})
    return create_model(name, __base__=FixedExtraction, values=(values, ...), metadata=(metadata, ...))
