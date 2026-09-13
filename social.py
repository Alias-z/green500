"""Pydantic response format for social extraction; null means missing."""
from typing import Literal
from extraction_models import (
    ExtractionModel, CompanyIdentity, PhysicalMetric, ReportContext, AdditionalObservation,
)


class WorkersLabor(ExtractionModel):
    employees: PhysicalMetric | None
    employee_turnover_percent: PhysicalMetric | None
    women_workforce_percent: PhysicalMetric | None


class HealthSafety(ExtractionModel):
    employee_fatalities: PhysicalMetric | None
    contractor_fatalities: PhysicalMetric | None
    recordable_injury_rate: PhysicalMetric | None


class HumanRightsSupplyChain(ExtractionModel):
    suppliers_audited: PhysicalMetric | None
    confirmed_violations: PhysicalMetric | None


class ProductCustomerResponsibility(ExtractionModel):
    product_recalls: PhysicalMetric | None


class DataPrivacyCybersecurity(ExtractionModel):
    confirmed_data_breaches: PhysicalMetric | None


class CommunityImpact(ExtractionModel):
    community_investment: PhysicalMetric | None


SOCIAL_GROUPS = {
    'workers_labor': WorkersLabor, 'health_safety': HealthSafety,
    'human_rights_supply_chain': HumanRightsSupplyChain,
    'product_customer_responsibility': ProductCustomerResponsibility,
    'data_privacy_cybersecurity': DataPrivacyCybersecurity,
    'community_impact': CommunityImpact,
}


class SocialObservation(AdditionalObservation):
    group: Literal['workers_labor', 'health_safety', 'human_rights_supply_chain',
                   'product_customer_responsibility', 'data_privacy_cybersecurity', 'community_impact']
    metric: PhysicalMetric


class VERDEXSocialData(ReportContext):
    workers_labor: WorkersLabor
    health_safety: HealthSafety
    human_rights_supply_chain: HumanRightsSupplyChain
    product_customer_responsibility: ProductCustomerResponsibility
    data_privacy_cybersecurity: DataPrivacyCybersecurity
    community_impact: CommunityImpact
    additional_observations: list[SocialObservation]


class SocialExtraction(ExtractionModel):
    company: CompanyIdentity
    social: VERDEXSocialData
