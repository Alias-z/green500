"""Apply reviewed, hash-bound extraction mappings to PDF or HTML evidence."""
import argparse
import json
import re
from pathlib import Path

from read_environment_report import read_report

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ["greenhouse_gas", "energy", "water", "waste_circularity", "biodiversity_land", "pollution"]


def extract(profile, evidence):
    if evidence["sha256"] != profile["source"]["sha256"]:
        raise ValueError("Source changed: extraction mappings require a fresh review")
    environment = {**profile["environment"], "status": "partially_extracted",
                   "source": profile["source"],
                   "extraction": {"method": "reviewed_mapping", "format": evidence["format"],
                                  "review": "PDF values visually checked; HTML statements checked in original content.",
                                  "limitations": "Only mapped observations are extracted; unmapped topics remain missing."}}
    for group in GROUPS:
        environment[group] = {}
    for rule in profile["observations"]:
        blocks = [b for b in evidence["blocks"]
                  if all(b["locator"].get(k) == v for k, v in rule["locator"].items())]
        if len(blocks) != 1:
            raise ValueError(f"Ambiguous evidence location: {rule['key']}")
        matches = list(re.finditer(rule["pattern"], blocks[0]["text"], re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"Expected one match for {profile['company']['ticker']}/{rule['key']}; got {len(matches)}")
        raw = matches[0].group("value")
        cleaned = raw.replace(",", "")
        if rule.get("space_decimal"):
            cleaned = cleaned.replace(" ", ".")
        value = float(cleaned) * rule.get("scale", 1)
        if value.is_integer():
            value = int(value)
        if value < 0 or (rule["unit"] == "percent" and value > 100):
            raise ValueError(f"Invalid numeric value for {rule['key']}")
        environment.setdefault(rule["group"], {})[rule["key"]] = {
            "value": value, "unit": rule["unit"],
            "status": rule.get("status", "extracted_and_reviewed"),
            "qualification": rule.get("qualification", ""),
            "evidence": {**blocks[0]["locator"], "source_id": profile["source"]["id"],
                         "raw_value": raw, "source_unit": rule.get("source_unit", rule["unit"]),
                         "scale_factor": rule.get("scale", 1),
                         "year_column": rule.get("year_column", str(environment["reporting_year"])),
                         "label": rule.get("label", rule["key"]),
                         "matched_text": matches[0].group(0)},
        }
    for group in GROUPS:
        if not environment[group]:
            environment[group] = {"value": None, "status": "not_extracted"}
    checks = []
    for check in profile.get("checks", []):
        group = environment[check["group"]]
        total = sum(group[key]["value"] for key in check["parts"])
        expected = group[check["total"]]["value"]
        delta = abs(total - expected)
        if delta > check.get("tolerance", 0):
            raise ValueError(f"Reconciliation failed: {check}")
        checks.append({**check, "difference": delta, "passed": True})
    environment["extraction"]["checks"] = checks
    return {"company": profile["company"], "environment": environment}


def run(profiles):
    results = []
    for profile in profiles:
        source = profile["source"]
        suffix = ".pdf" if source["content_type"] == "application/pdf" else ".html"
        path = ROOT / "data/objects" / (source["sha256"] + suffix)
        cache = ROOT / "data/exports" / (profile["company"]["ticker"] + "-evidence.json")
        # Validate the original even when using cached text.
        import hashlib
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != source["sha256"]:
                raise ValueError(f"Original hash mismatch: {path}")
        evidence = json.loads(cache.read_text()) if cache.exists() else read_report(path)
        results.append((profile, extract(profile, evidence)))
    # Complete validation of the entire batch before replacing reviewed results.
    for profile, result in results:
        output = ROOT / "data/report_examples" / profile["directory"] / "results.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        print(f"{profile['company']['ticker']}: {len(profile['observations'])} observations")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profiles", type=Path)
    args = parser.parse_args()
    run(json.loads(args.profiles.read_text()))
