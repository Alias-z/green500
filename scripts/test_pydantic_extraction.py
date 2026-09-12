import copy
import math
import unittest
from pydantic import ValidationError
from analysis_schema import EXTRACTION_MODELS, GROUP_FIELDS, extraction_schema, validate_extraction
from environmental import EnvironmentalExtraction, VERDEXEnvironmentalData
from social import SocialExtraction, VERDEXSocialData
from test_model_extraction import empty, metric


class PydanticExtractionTests(unittest.TestCase):
    def test_typed_environment_and_social_preserve_null_and_zero(self):
        for category, model, body_type, group, field in [
            ('environmental', EnvironmentalExtraction, VERDEXEnvironmentalData, 'greenhouse_gas', 'scope_1'),
            ('social', SocialExtraction, VERDEXSocialData, 'health_safety', 'employee_fatalities'),
        ]:
            result = empty(category)
            key = 'environment' if category == 'environmental' else category
            result[key][group][field] = metric(0)
            parsed = model.model_validate(result)
            self.assertIsInstance(getattr(parsed, key), body_type)
            data = parsed.model_dump(mode='json')
            self.assertEqual(data[key][group][field]['value'], 0)
            other = next(k for k in GROUP_FIELDS[category][group] if k != field)
            self.assertIsNone(data[key][group][other])

    def test_invalid_measurements_rejected(self):
        for value in [-1, '42', True, math.inf, math.nan]:
            result = empty('social')
            result['social']['health_safety']['employee_fatalities'] = {**metric(), 'value': value}
            with self.assertRaises(ValidationError): validate_extraction('social', result)

    def test_confidence_percentage_and_extra_fields_rejected(self):
        for changes in [{'confidence': 1.1}, {'value': 101, 'unit': 'percent'}, {'unexpected': True}]:
            result = empty()
            result['environment']['energy']['renewable_electricity_share'] = {**metric(), **changes}
            with self.assertRaises(ValidationError): validate_extraction('environmental', result)

    def test_additional_observations_are_typed(self):
        result = empty('social')
        result['social']['additional_observations'] = [{'group': 'water', 'key': 'test', 'metric': metric()}]
        with self.assertRaises(ValidationError): validate_extraction('social', result)
        result['social']['additional_observations'][0].update(group='health_safety', key='status')
        with self.assertRaises(ValidationError): validate_extraction('social', result)

    def test_schema_generated_from_models_and_closed(self):
        def check(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    self.assertIs(node['additionalProperties'], False)
                    self.assertEqual(set(node.get('required', [])), set(node['properties']))
                for value in node.values(): check(value)
            elif isinstance(node, list):
                for value in node: check(value)
        for category, model in EXTRACTION_MODELS.items():
            schema = extraction_schema(category)
            self.assertEqual(schema, model.model_json_schema())
            check(schema)

if __name__ == '__main__': unittest.main()
