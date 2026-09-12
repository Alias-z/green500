from pathlib import Path
import tempfile
import unittest

from extract_environment_results import extract
from read_environment_report import read_report


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "company": {"ticker": "TEST"}, "source": {"id": 1, "sha256": "abc"},
            "environment": {"reporting_year": 2023, "boundary": {"description": "Equity share"}},
            "observations": [{"group": "greenhouse_gas", "key": "scope_2_market_based",
                              "locator": {"pdf_page": 2}, "pattern": r"FY2023: (?P<value>[\d,]+)",
                              "unit": "tCO2e", "scale": 1000}],
        }
        self.evidence = {"sha256": "abc", "format": "pdf", "blocks": [
            {"locator": {"pdf_page": 2}, "text": "FY2022: 999\nFY2023: 0"}]}

    def test_zero_is_observed_and_missing_is_null(self):
        env = extract(self.profile, self.evidence)["environment"]
        self.assertEqual(env["greenhouse_gas"]["scope_2_market_based"]["value"], 0)
        self.assertIsNone(env["water"]["value"])
        self.assertEqual(env["reporting_year"], 2023)
        self.assertEqual(env["boundary"]["description"], "Equity share")

    def test_units_scaled_once(self):
        self.evidence["blocks"][0]["text"] = "FY2023: 1,234"
        metric = extract(self.profile, self.evidence)["environment"]["greenhouse_gas"]["scope_2_market_based"]
        self.assertEqual(metric["value"], 1234000)
        self.assertEqual(metric["evidence"]["raw_value"], "1,234")

    def test_changed_source_requires_review(self):
        self.evidence["sha256"] = "changed"
        with self.assertRaises(ValueError): extract(self.profile, self.evidence)

    def test_missing_and_ambiguous_matches_fail(self):
        for text in ["FY2022: 123", "FY2023: 1\nFY2023: 2"]:
            self.evidence["blocks"][0]["text"] = text
            with self.assertRaises(ValueError): extract(self.profile, self.evidence)

    def test_html_div_narrative_and_table_spans_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.bin"
            path.write_text('<!doctype html><html><body><h2>Environment</h2>'
                            '<div>Scope 1: 42</div><script>FAKE 999</script>'
                            '<table><tr><th colspan="2">2025</th></tr>'
                            '<tr><td>Water</td><td>12</td></tr></table></body></html>')
            result = read_report(path)
            self.assertEqual(result["format"], "html")
            self.assertIn("Scope 1: 42", result["blocks"][-1]["text"])
            self.assertNotIn("FAKE", result["blocks"][-1]["text"])
            table = next(b for b in result["blocks"] if "rows" in b)
            self.assertEqual(table["rows"][0][0]["colspan"], "2")


if __name__ == "__main__":
    unittest.main()
