import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis_prompts import CATEGORY_PROMPTS, SHARED_INSTRUCTIONS, build_messages
from prepare_analysis import prepare


class PromptTests(unittest.TestCase):
    def test_categories_share_rules_and_keep_evidence_out_of_system(self):
        for category, prompt in CATEGORY_PROMPTS.items():
            messages = build_messages(category, {'ticker': 'TEST'}, {'text': 'UNTRUSTED_DOCUMENT_SENTINEL'})
            self.assertIn(SHARED_INSTRUCTIONS, messages[0]['content'])
            self.assertIn(prompt, messages[0]['content'])
            self.assertNotIn('UNTRUSTED_DOCUMENT_SENTINEL', messages[0]['content'])
            self.assertEqual(json.loads(messages[1]['content'])['document_evidence']['text'], 'UNTRUSTED_DOCUMENT_SENTINEL')

    def test_unknown_category_rejected(self):
        with self.assertRaises(ValueError): build_messages('unknown', {}, {})

    def test_html_request_retains_evidence_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.html'
            path.write_text('<html><h1>Water</h1><p>2025 withdrawals: 42 m3.</p></html>')
            request = prepare(path, 'environmental', {'ticker': 'TEST'}, 'https://example.com/report')
            self.assertEqual(request['status'], 'prepared_not_submitted')
            self.assertEqual(len(request['source_sha256']), 64)
            evidence = json.loads(request['messages'][1]['content'])['document_evidence']
            self.assertEqual(evidence['format'], 'html')
            self.assertEqual(evidence['source_url'], 'https://example.com/report')
            self.assertIn('42 m3', str(evidence['blocks']))

if __name__ == '__main__': unittest.main()
