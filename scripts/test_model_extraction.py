import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis_schema import GROUP_FIELDS, CATEGORY_KEYS, extraction_schema
from analyze_report import analyze, validate_output, output_json, save_result, merge_results, chunks
from jsonschema import Draft202012Validator, ValidationError

COMPANY = {'name': 'Test Company', 'ticker': 'TEST'}

def empty(category='environmental'):
    return {'company': COMPANY.copy(), CATEGORY_KEYS[category]: {
        'reporting_year': 2025, 'boundary': {'description': 'Global operations'}, 'currency': 'USD',
        **{g: {k: None for k in keys} for g, keys in GROUP_FIELDS[category].items()},
        'additional_observations': [], 'limitations': []}}

def metric(value=42):
    return {'value': value, 'unit': 'tCO2e', 'reporting_year': 2025, 'status': 'reported',
            'qualification': '', 'confidence': .9,
            'evidence': {'block_id': 0, 'quote': f'Emissions: {value} tonnes.', 'raw_value': str(value),
                         'source_unit': 'tonnes', 'scale_factor': 1}}

def evidence(text='Emissions: 42 tonnes.'):
    return {'sha256': 'a' * 64, 'format': 'html', 'source_url': 'https://example.com/report',
            'document_name': 'report.html',
            'blocks': [{'block_id': 0, 'text': text, 'locator': {'html_block': 0, 'section': 'Emissions'}}]}

def completed(result):
    return {'id': 'test-response', 'status': 'completed', 'output': [
        {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(result)}]}]}

class ModelExtractionTests(unittest.TestCase):
    def test_schemas_all_categories(self):
        for category in GROUP_FIELDS:
            schema = extraction_schema(category)
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(empty(category))

    def test_missing_keys_extra_keys_and_types_rejected(self):
        for mutate in [lambda r: r.pop('company'), lambda r: r.update(fake=1),
                       lambda r: r['environment'].update(reporting_year='2025')]:
            result = empty(); mutate(result)
            with self.assertRaises(ValidationError): validate_output(result, 'environmental', COMPANY, evidence())

    def test_zero_preserved_with_provenance(self):
        result = empty(); result['environment']['greenhouse_gas']['scope_1'] = metric(0)
        validated = validate_output(result, 'environmental', COMPANY, evidence('Emissions: 0 tonnes.'))
        saved = merge_results([validated], 'environmental', COMPANY, evidence(), 'test-model', ['id'])
        m = saved['environment']['greenhouse_gas']['scope_1']
        self.assertEqual(m['value'], 0)
        self.assertEqual(m['evidence']['source_sha256'], 'a' * 64)
        self.assertEqual(m['status'], 'llm_extracted')
        self.assertIsNone(saved['environment']['water']['withdrawal']['value'])

    def test_quote_wrong_page_number_and_conversion_rejected(self):
        for mutate in [lambda m: m['evidence'].update(quote='Invented 42'),
                       lambda m: m['evidence'].update(block_id=99),
                       lambda m: m.update(value=420),
                       lambda m: m['evidence'].update(raw_value='43')]:
            result = empty(); m = metric(); mutate(m)
            result['environment']['greenhouse_gas']['scope_1'] = m
            with self.assertRaises(ValueError): validate_output(result, 'environmental', COMPANY, evidence())

    def test_conflicting_chunks_are_missing_with_candidates(self):
        one, two = empty(), empty()
        one['environment']['greenhouse_gas']['scope_1'] = metric(42)
        two['environment']['greenhouse_gas']['scope_1'] = metric(43)
        result = merge_results([one, two], 'environmental', COMPANY, evidence(), 'test', [])
        m = result['environment']['greenhouse_gas']['scope_1']
        self.assertIsNone(m['value']); self.assertEqual(m['status'], 'conflicting')
        self.assertEqual(len(m['candidates']), 2)

    def test_history_does_not_replace_current_year(self):
        one, two = empty(), empty()
        one['environment']['greenhouse_gas']['scope_1'] = metric(42)
        two['environment']['reporting_year'] = 2024
        two['environment']['greenhouse_gas']['scope_1'] = {**metric(43), 'reporting_year': 2024}
        result = merge_results([one, two], 'environmental', COMPANY, evidence(), 'test', [])
        self.assertEqual(result['environment']['greenhouse_gas']['scope_1']['value'], 42)
        self.assertEqual(result['environment']['greenhouse_gas']['scope_1_2024']['value'], 43)

    def test_refusal_and_truncation_rejected(self):
        for response in [{'status': 'incomplete'}, {'status': 'completed', 'output': [
            {'content': [{'type': 'refusal', 'refusal': 'No'}]}]}]:
            with self.assertRaises(ValueError): output_json(response)

    def test_chunks_cover_large_html_block(self):
        ev = evidence('A' * 1500 + 'UNIQUE_END')
        batches = list(chunks(ev, 1000))
        self.assertGreater(len(batches), 1)
        self.assertIn('UNIQUE_END', batches[-1]['blocks'][-1]['text'])
        ids = [b['block_id'] for c in batches for b in c['blocks']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_actual_reader_model_contract_and_saved_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.html'
            path.write_text('<html><p>Emissions: 42 tonnes.</p></html>')
            seen = []
            def fake(payload):
                seen.append(payload)
                request = json.loads(payload['input'][1]['content'])
                block = request['document_evidence']['blocks'][0]
                result = empty(); result['environment']['greenhouse_gas']['scope_1'] = metric()
                result['environment']['greenhouse_gas']['scope_1']['evidence']['block_id'] = block['block_id']
                return completed(result)
            result = analyze(path, 'environmental', COMPANY, 'test-model', transport=fake)
            self.assertTrue(seen[0]['text']['format']['strict'])
            self.assertEqual(seen[0]['text']['format']['schema'], extraction_schema('environmental'))
            self.assertIn('greenhouse_gas', result['environment'])
            self.assertEqual(result['environment']['greenhouse_gas']['scope_1']['value'], 42)
            dest = Path(directory) / 'results.json'
            dest.write_text(json.dumps({'company': COMPANY, 'social': {'preserve': True}}))
            save_result(dest, result)
            self.assertTrue(json.loads(dest.read_text())['social']['preserve'])

    def test_financial_output_validates_existing_model(self):
        r = empty('financial'); r['financial']['financial']['revenue'] = metric(42)
        r['financial']['financial']['revenue']['unit'] = 'USD'
        result = merge_results([r], 'financial', COMPANY, evidence(), 'test', [])
        from financial import VERDEXFinancialData
        validated = VERDEXFinancialData.model_validate(result['financial'])
        self.assertEqual(validated.revenue.value, 42)
        self.assertIsNone(validated.employees.value)
        self.assertIn('financial_evidence', result)

    def test_pdf_reader_and_model_schema_path(self):
        from unittest.mock import patch
        from read_environment_report import read_report
        report = next((Path(__file__).resolve().parents[1] / 'data/objects').glob('*.pdf'), None)
        if report is None:
            self.skipTest('No cached PDF fixture available')
        # Use the actual PDF reader, one page via a controlled file to keep this test small.
        import pdfplumber
        import pypdfium2 as pdfium
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'one-page.pdf'
            source = pdfium.PdfDocument(str(report))
            output = pdfium.PdfDocument.new()
            output.import_pages(source, [0])
            output.save(str(dest)); output.close(); source.close()
            seen = []
            def fake(payload):
                req = json.loads(payload['input'][1]['content'])
                seen.extend(req['document_evidence']['blocks'])
                return completed(empty())
            result = analyze(dest, 'environmental', COMPANY, 'test-model', transport=fake)
            self.assertEqual(result['environment']['extraction']['format'], 'pdf')
            self.assertTrue(all('pdf_page' in block['locator'] for block in seen))

    def test_failed_live_configuration_does_not_create_output(self):
        with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
            analyze(Path('unused'), 'environmental', COMPANY, 'test-model')

if __name__ == '__main__': unittest.main()
