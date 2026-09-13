"""Pydantic response format for environmental extraction; null means missing."""

from typing import Literal

from green500.processing.report_schema_types import (
    AdditionalObservation,
    CompanyIdentity,
    ExtractionModel,
    PhysicalMetric,
    ReportContext,
)


class GreenhouseGas(ExtractionModel):
    scope_1: PhysicalMetric | None
    scope_2_location_based: PhysicalMetric | None
    scope_2_market_based: PhysicalMetric | None
    scope_1_2_market_based: PhysicalMetric | None
    scope_3_total: PhysicalMetric | None


class Energy(ExtractionModel):
    total_energy: PhysicalMetric | None
    office_energy: PhysicalMetric | None
    renewable_energy: PhysicalMetric | None
    renewable_electricity: PhysicalMetric | None
    renewable_electricity_share: PhysicalMetric | None


class Water(ExtractionModel):
    withdrawal: PhysicalMetric | None
    consumption: PhysicalMetric | None
    discharge: PhysicalMetric | None


class WasteCircularity(ExtractionModel):
    total_waste: PhysicalMetric | None
    hazardous_waste: PhysicalMetric | None
    nonhazardous_waste: PhysicalMetric | None
    waste_recycled: PhysicalMetric | None


class BiodiversityLand(ExtractionModel):
    land_restored: PhysicalMetric | None
    land_affected: PhysicalMetric | None


class Pollution(ExtractionModel):
    nox: PhysicalMetric | None
    sox: PhysicalMetric | None
    mercury: PhysicalMetric | None


class Targets(ExtractionModel):
    """Named targets are supplied in additional_observations, separate from outcomes."""


ENVIRONMENTAL_GROUPS = {
    "greenhouse_gas": GreenhouseGas,
    "energy": Energy,
    "water": Water,
    "waste_circularity": WasteCircularity,
    "biodiversity_land": BiodiversityLand,
    "pollution": Pollution,
    "targets": Targets,
}


class EnvironmentalObservation(AdditionalObservation):
    group: Literal[
        "greenhouse_gas",
        "energy",
        "water",
        "waste_circularity",
        "biodiversity_land",
        "pollution",
        "targets",
    ]
    metric: PhysicalMetric


class VERDEXEnvironmentalData(ReportContext):
    greenhouse_gas: GreenhouseGas
    energy: Energy
    water: Water
    waste_circularity: WasteCircularity
    biodiversity_land: BiodiversityLand
    pollution: Pollution
    targets: Targets
    additional_observations: list[EnvironmentalObservation]


class EnvironmentalExtraction(ExtractionModel):
    company: CompanyIdentity
    environment: VERDEXEnvironmentalData
