"""Build bounded Qwen input for fixed social fields from report page images."""

import base64
import json

from green500.processing.report_page_images import image_payload, validate_company_pages
from green500.processing.social_features import FIELDS

VERSION = "social-page-images-v1"


def extraction_messages(company):
    """Return a complete ten-field social extraction request using one or two images."""
    validate_company_pages(company)
    field_lines = "\n".join(
        f"- {name}: {specification['description']} Canonical unit: {specification['unit']}."
        for name, specification in FIELDS.items()
    )
    skeleton = {
        "company": {"name": company["company_name"], "ticker": company["company_id"]},
        "reporting_year": company["reporting_year"],
        "boundary": None,
        "limitations": [],
        "values": dict.fromkeys(FIELDS),
        "metadata": dict.fromkeys(FIELDS),
    }
    system = """You extract employee and social facts from original report page images.
Return exactly one JSON object and no Markdown. Read each table or infographic visually:
bind a value to its label, year, unit, company population and employee/contractor boundary.
Use only the required reporting year. Every values and metadata key must be present. Keep a
missing or ambiguous field null and never turn missing disclosure into zero.

For each non-null value, metadata must contain status, reporting_year, reason,
qualification, numeric confidence from 0 to 1, and evidence. Evidence uses exactly the
keys block_id, quote, raw_value, source_unit, scale_factor. block_id is the supplied PDF
page number. quote transcribes the visible header, boundary label, metric row and value.
Copy raw_value exactly and give the deterministic multiplier into the canonical unit.

Employee count must be total company employees. Turnover must be employee turnover.
Women workforce excludes managers, board members and regional subsets. Employee and
contractor fatalities remain separate. Use the employee recordable injury rate and only
the field whose per-200,000 or per-1,000,000-hours denominator is visibly stated; do not
use the contractor rate or convert between denominators. Supplier audits require a count
of suppliers actually audited. Confirmed violations require confirmed human-rights or
supply-chain violations. Community investment requires an explicitly reported USD amount.
Targets, policies, participants, training counts and qualitative claims stay null."""
    user_content = [
        {
            "type": "text",
            "text": (
                f"Company: {company['company_name']} ({company['company_id']})\n"
                f"Required actual reporting year: {company['reporting_year']}\n\n"
                f"Fields:\n{field_lines}\n\nReturn this complete object shape:\n"
                + json.dumps(skeleton, ensure_ascii=False, separators=(",", ":"))
            ),
        }
    ]
    for page in company["pages"]:
        body, media_type, _ = image_payload(page)
        user_content.extend(
            [
                {
                    "type": "text",
                    "text": f"Original report PDF page {page['page_number']}:",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,"
                        + base64.b64encode(body).decode(),
                        "detail": "high",
                    },
                },
            ]
        )
    return [
        {"role": "system", "content": [{"type": "text", "text": system}]},
        {"role": "user", "content": user_content},
    ]
