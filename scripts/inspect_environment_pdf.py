"""Find environmental evidence in a local PDF; outputs candidates, not validated facts."""

import argparse
import hashlib
import importlib.metadata
import json
import re
from pathlib import Path

import pdfplumber

TOPICS = {
    "climate_ghg": r"\bscope\s*[123]\b|greenhouse|\bco2e\b|carbon emissions",
    "energy": r"\benergy\b|electricity|renewable|\bmwh\b|\bgwh\b",
    "water": r"\bwater\b|withdrawal|water.stress",
    "waste_circularity": r"\bwaste\b|recycl|landfill|circular",
    "biodiversity_land": r"biodiversity|deforestation|ecosystem|habitat",
    "pollution": r"pollut|toxic|\bnox\b|\bsox\b|\bspills?\b",
}


def inspect(path, source_url=None):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    pages = []
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            topics = [key for key, pattern in TOPICS.items() if re.search(pattern, text, re.I)]
            # Low native text is a routing hint, not proof of a scanned page.
            sparse = len(text.strip()) < 80
            if topics or sparse:
                tables = [
                    {"bbox": list(table.bbox), "rows": table.extract()}
                    for table in page.find_tables()
                ] if topics else []
                pages.append({
                    "pdf_page": page.page_number,
                    "width": page.width,
                    "height": page.height,
                    "topics": topics,
                    "native_text_characters": len(text),
                    "needs_visual_or_ocr_review": sparse,
                    "text": text,
                    "tables": tables,
                    "status": "candidate_evidence_requires_review",
                })
            page.close()
    return {
        "source_url": source_url,
        "sha256": digest,
        "extractor": {"name": "pdfplumber", "version": importlib.metadata.version("pdfplumber")},
        "page_count": page_count,
        "page_numbering": "1-based PDF pages, not printed page labels",
        "limitations": [
            "Keyword routing can miss relevant pages and match unrelated prose.",
            "Native text can scramble columns; review tables, footnotes and rendered pages.",
            "No OCR or automatic semantic validation is performed.",
        ],
        "pages": pages,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--source-url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inspect(args.pdf, args.source_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"pages": result["page_count"], "candidate_pages": len(result["pages"]),
                      "output": str(args.output), "sha256": result["sha256"]}))
