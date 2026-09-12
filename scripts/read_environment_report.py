"""Read PDF or HTML originals into evidence blocks without inventing observations."""
import argparse
import hashlib
import json
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup


def read_report(path):
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if raw.startswith(b"%PDF-"):
        blocks = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                blocks.append({"locator": {"pdf_page": page.page_number},
                               "text": page.extract_text() or ""})
                page.close()
        return {"format": "pdf", "sha256": digest, "blocks": blocks}
    if not any(marker in raw[:10000].lower() for marker in [b"<html", b"<!doctype html", b"<body"]):
        raise ValueError(f"Unsupported report format: {path}")
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.select("script, style, noscript, nav, header, footer, [hidden]"):
        tag.decompose()
    blocks = []
    heading = "Document"
    # Retain table cells as rows; preserve spans so callers cannot silently assume a grid.
    for index, tag in enumerate(soup.select("h1,h2,h3,h4,h5,h6,p,li,table")):
        if tag.find_parent(["table", "li"]):
            continue
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        if tag.name.startswith("h"):
            heading = text
        block = {"locator": {"html_block": index, "section": heading, "element": tag.name}, "text": text}
        if tag.get("id"):
            block["locator"]["element_id"] = tag["id"]
        if tag.name == "table":
            block["rows"] = [[{"text": cell.get_text(" ", strip=True),
                               "rowspan": cell.get("rowspan", "1"),
                               "colspan": cell.get("colspan", "1")}
                              for cell in row.find_all(["th", "td"], recursive=False)]
                             for row in tag.find_all("tr")]
        blocks.append(block)
    # Filings can keep narrative in divs or inline XBRL rather than paragraphs.
    blocks.append({"locator": {"section": "Complete document", "html_block": -1},
                   "text": soup.get_text(" ", strip=True)})
    return {"format": "html", "sha256": digest, "blocks": blocks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = read_report(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"format": result["format"], "blocks": len(result["blocks"]), "sha256": result["sha256"]}))
