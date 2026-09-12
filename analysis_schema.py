"""Structured extraction contract. Keys describe data, never report-specific locations."""
from copy import deepcopy

GROUP_FIELDS = {
    'environmental': {
        'greenhouse_gas': ['scope_1', 'scope_2_location_based', 'scope_2_market_based', 'scope_1_2_market_based', 'scope_3_total'],
        'energy': ['total_energy', 'office_energy', 'renewable_energy', 'renewable_electricity', 'renewable_electricity_share'],
        'water': ['withdrawal', 'consumption', 'discharge'],
        'waste_circularity': ['total_waste', 'hazardous_waste', 'nonhazardous_waste', 'waste_recycled'],
        'biodiversity_land': ['land_restored', 'land_affected'],
        'pollution': ['nox', 'sox', 'mercury'],
        'targets': [],
    },
    'social': {
        'workers_labor': ['employees', 'employee_turnover_percent', 'women_workforce_percent'],
        'health_safety': ['employee_fatalities', 'contractor_fatalities', 'recordable_injury_rate'],
        'human_rights_supply_chain': ['suppliers_audited', 'confirmed_violations'],
        'product_customer_responsibility': ['product_recalls'],
        'data_privacy_cybersecurity': ['confirmed_data_breaches'],
        'community_impact': ['community_investment'],
    },
    'financial': {
        'financial': ['revenue', 'employees', 'total_capex', 'green_transition_capex', 'operating_income',
                      'total_assets', 'cash_and_equivalents', 'total_debt', 'operating_cash_flow'],
    },
}
CATEGORY_KEYS = {'environmental': 'environment', 'social': 'social', 'financial': 'financial'}


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


STRING = {'type': 'string'}
NUMBER = {'type': 'number'}
INTEGER = {'type': 'integer'}
EVIDENCE = obj({'block_id': INTEGER, 'quote': STRING, 'raw_value': STRING,
                'source_unit': STRING, 'scale_factor': NUMBER})
METRIC = obj({'value': NUMBER, 'unit': STRING, 'reporting_year': INTEGER,
              'status': {'type': 'string', 'enum': ['reported', 'company_estimate', 'company_target']},
              'qualification': STRING, 'confidence': NUMBER, 'evidence': EVIDENCE})


def extraction_schema(category):
    groups = GROUP_FIELDS[category]
    properties = {'reporting_year': nullable(INTEGER), 'boundary': obj({'description': STRING}),
                  'currency': nullable(STRING),
                  **{group: obj({key: nullable({'$ref': '#/$defs/metric'}) for key in keys}) for group, keys in groups.items()},
                  'additional_observations': {'type': 'array', 'items': obj({
                      'group': {'type': 'string', 'enum': list(groups)}, 'key': STRING,
                      'metric': {'$ref': '#/$defs/metric'}})},
                  'limitations': {'type': 'array', 'items': STRING}}
    result = obj({'company': obj({'name': STRING, 'ticker': STRING}), CATEGORY_KEYS[category]: obj(properties)})
    result['$defs'] = {'metric': deepcopy(METRIC)}
    return result
