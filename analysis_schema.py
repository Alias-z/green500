"""Generate every model response schema from Pydantic; no parallel handwritten schema."""
from typing import Literal
from extraction_models import (
    ExtractionModel, CompanyIdentity, ReportMetric, ReportContext, AdditionalObservation,
)
from environmental import EnvironmentalExtraction, ENVIRONMENTAL_GROUPS
from social import SocialExtraction, SOCIAL_GROUPS


class FinancialInputs(ExtractionModel):
    revenue: ReportMetric | None
    employees: ReportMetric | None
    total_capex: ReportMetric | None
    green_transition_capex: ReportMetric | None
    operating_income: ReportMetric | None
    total_assets: ReportMetric | None
    cash_and_equivalents: ReportMetric | None
    total_debt: ReportMetric | None
    operating_cash_flow: ReportMetric | None


class FinancialObservation(AdditionalObservation):
    group: Literal['financial']
    metric: ReportMetric


class FinancialReport(ReportContext):
    financial: FinancialInputs
    additional_observations: list[FinancialObservation]


class FinancialExtraction(ExtractionModel):
    company: CompanyIdentity
    financial: FinancialReport


EXTRACTION_MODELS = {
    'environmental': EnvironmentalExtraction,
    'social': SocialExtraction,
    'financial': FinancialExtraction,
}
GROUP_MODELS = {
    'environmental': ENVIRONMENTAL_GROUPS,
    'social': SOCIAL_GROUPS,
    'financial': {'financial': FinancialInputs},
}
GROUP_FIELDS = {
    category: {group: list(model.model_fields) for group, model in groups.items()}
    for category, groups in GROUP_MODELS.items()
}
CATEGORY_KEYS = {'environmental': 'environment', 'social': 'social', 'financial': 'financial'}


def extraction_schema(category):
    return EXTRACTION_MODELS[category].model_json_schema()


def validate_extraction(category, result):
    return EXTRACTION_MODELS[category].model_validate(result).model_dump(mode='json')
