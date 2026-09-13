import copy
import json
import unittest
from pathlib import Path
from score_environment import score_environment

ROOT = Path(__file__).resolve().parents[1]

def record(directory):
    return json.loads((ROOT / 'data/report_examples' / directory / 'results.json').read_text())['environment']

class ScoringTests(unittest.TestCase):
    def test_missing_and_target_only(self):
        for env in [{}, {'status': 'not_extracted'}, record('agilent')]:
            self.assertIsNone(score_environment(env)['value'])

    def test_zero_is_performance_not_missing(self):
        score = score_environment(record('aflac'))
        self.assertEqual(score['value'], 0)
        self.assertEqual(score['coverage']['scored_topics'], 1)
        self.assertLess(score['components'][0]['annualized_change_percent'], 0)

    def test_reduction_and_neutral(self):
        env = record('adobe')
        before = score_environment(env)['value']
        env['greenhouse_gas']['scope_1_2_market_based_2023']['value'] = 7218 + 19602
        self.assertEqual(score_environment(env)['value'], 50)
        self.assertGreater(before, 50)

    def test_missing_baseline_not_zero_and_unit_mismatch(self):
        env = record('adobe')
        env['greenhouse_gas']['scope_1_2_market_based_2023']['value'] = 0
        self.assertIsNone(score_environment(env)['value'])
        env = record('adobe')
        env['greenhouse_gas']['scope_1_2_market_based_2023']['unit'] = 'kg'
        self.assertIsNone(score_environment(env)['value'])

    def test_boundary_change_excludes_ghg(self):
        score = score_environment(record('air_products'))
        self.assertEqual(score['value'], 5)
        self.assertIsNone(score['topics']['greenhouse_gas'])

    def test_missing_topics_not_penalized(self):
        env = record('air_products')
        env['water'] = {'value': None}
        self.assertEqual(score_environment(env)['value'], 5)

    def test_demo_and_materiality_cannot_change_score(self):
        env = record('airbnb')
        expected = score_environment(env)
        modified = copy.deepcopy(env)
        modified['income_statement'] = {'revenue': 1e100}
        modified['materiality'] = {'topics': [3] * 15}
        self.assertEqual(score_environment(modified), expected)

    def test_compound_multiyear_and_source_evidence(self):
        score = score_environment(record('amd'))
        expected = 50 + 500 * (1 - (42972 / 61754) ** (1/5))
        self.assertAlmostEqual(score['value'], round(expected, 1))
        self.assertTrue(all(i['evidence'] for c in score['components'] for i in c['inputs']))

    def test_dataset_ten_supported_scores(self):
        values = [score_environment(json.loads(p.read_text())['environment'])['value']
                  for p in (ROOT / 'data/report_examples').glob('*/results.json')]
        self.assertEqual(sum(v is not None for v in values), 10)
        self.assertTrue(all(0 <= v <= 100 for v in values if v is not None))

if __name__ == '__main__': unittest.main()
