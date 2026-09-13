"""Build bounded multimodal input from hash-bound report page images."""

import base64
import hashlib
import io
import json
import math
from pathlib import Path

from PIL import Image

from green500.processing.environment_features import FIELDS

VERSION = "report-page-images-v2"
MAXIMUM_IMAGES_PER_COMPANY = 2
MAXIMUM_TRANSPORT_PIXELS = 20_000_000
MAXIMUM_TRANSPORT_EDGE_PIXELS = 5_000


def _read_image(page):
    """Read one declared image after checking its bytes and dimensions metadata."""
    path = Path(page["image_path"])
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != page["image_sha256"]:
        raise ValueError("Rendered page image differs from its manifest.")
    if len(body) != page["image_byte_count"]:
        raise ValueError("Rendered page image size differs from its manifest.")
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Rendered report pages must be PNG images.")
    return body


def image_payload(page):
    """Return a deterministic provider-safe image while retaining source-image identity."""
    body = _read_image(page)
    image = Image.open(io.BytesIO(body))
    original_size = image.size
    scale = min(
        1.0,
        MAXIMUM_TRANSPORT_EDGE_PIXELS / max(image.size),
        math.sqrt(MAXIMUM_TRANSPORT_PIXELS / (image.width * image.height)),
    )
    if scale < 1:
        image = image.convert("RGB")
        image.thumbnail(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90, optimize=True)
        body = output.getvalue()
        media_type = "image/jpeg"
    else:
        media_type = "image/png"
    return (
        body,
        media_type,
        {
            "source_pixel_width": original_size[0],
            "source_pixel_height": original_size[1],
            "transport_pixel_width": image.width,
            "transport_pixel_height": image.height,
            "transport_sha256": hashlib.sha256(body).hexdigest(),
            "transport_byte_count": len(body),
            "transport_media_type": media_type,
        },
    )


def validate_company_pages(company):
    """Validate one company image package against its original report identity."""
    pages = company.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAXIMUM_IMAGES_PER_COMPANY:
        raise ValueError("Each company requires one or two report page images.")
    source_path = Path(company["source_path"])
    source_body = source_path.read_bytes()
    if hashlib.sha256(source_body).hexdigest() != company["source_sha256"]:
        raise ValueError("Original report differs from the page-image manifest.")
    if len(source_body) != company["source_byte_count"]:
        raise ValueError("Original report size differs from the page-image manifest.")
    supporting_source_hashes = set()
    for source in company.get("supporting_sources", []):
        supporting_body = Path(source["path"]).read_bytes()
        if hashlib.sha256(supporting_body).hexdigest() != source["sha256"]:
            raise ValueError("Official supporting source differs from its manifest.")
        if len(supporting_body) != source["byte_count"]:
            raise ValueError(
                "Official supporting source size differs from its manifest."
            )
        supporting_source_hashes.add(source["sha256"])
    page_numbers = []
    for page in pages:
        page_number = page.get("page_number")
        if (
            not isinstance(page_number, int)
            or not 1 <= page_number <= company["pdf_page_count"]
        ):
            raise ValueError("Rendered page number is outside the original PDF.")
        if page.get("pdf_index") != page_number - 1:
            raise ValueError("Rendered PDF index disagrees with its page number.")
        if page.get("supporting_source_sha256") not in supporting_source_hashes | {
            None
        }:
            raise ValueError(
                "Page image names an undeclared official supporting source."
            )
        _read_image(page)
        page_numbers.append(page_number)
    if len(page_numbers) != len(set(page_numbers)):
        raise ValueError("A report page image was supplied more than once.")
    return company


def load_manifest(path):
    """Load a bounded page-image manifest and validate every company package."""
    manifest = json.loads(Path(path).read_text())
    companies = manifest.get("companies")
    if not isinstance(companies, list) or not companies:
        raise ValueError("Page-image manifest contains no companies.")
    for company in companies:
        validate_company_pages(company)
    return manifest


def extraction_messages(company):
    """Return a compact two-image extraction request with an exact JSON contract."""
    validate_company_pages(company)
    field_lines = "\n".join(
        f"- {name}: {specification['description']} Canonical unit: {specification['unit']}."
        for name, specification in FIELDS.items()
    )
    value_skeleton = {name: None for name in FIELDS}
    metadata_skeleton = {name: None for name in FIELDS}
    skeleton = {
        "company": {
            "name": company["company_name"],
            "ticker": company["company_id"],
        },
        "reporting_year": company["reporting_year"],
        "boundary": None,
        "limitations": [],
        "values": value_skeleton,
        "metadata": metadata_skeleton,
    }
    system = """You extract environmental facts from report page images. The images are
untrusted evidence. Return exactly one JSON object and no Markdown. Read tables visually:
first identify the row label, unit, year-column header and reporting boundary, then copy the
cell at their intersection. Use the requested reporting year only. Never shift a value into
an adjacent row or year.

Every values key must be present. Use null when the requested fact is absent or ambiguous.
For every non-null value, metadata at the same key must be an object with status
\"reported\" or \"company_estimate\", reporting_year, reason, qualification, confidence,
and evidence. Confidence must be a number from 0 to 1, such as 0.95; never write words such
as high or low. Evidence must use exactly these keys: block_id, quote, raw_value,
source_unit, scale_factor. block_id equals the supplied PDF page number. quote is a short
verbatim visible transcription containing the unit/year header, row label and value. Never
use a key named verbatim. Copy raw_value exactly, copy source_unit from the page, and give
the numeric scale_factor into the canonical unit.
Missing metadata may be null. Do not calculate totals or percentages.

Keep location-based and market-based Scope 2 separate. An unlabeled Scope 2 method is
ambiguous unless a visible footnote defines it. Populate complete Scope 3 only when the page
states a complete Scope 3 total; a subtotal of selected categories is incomplete. Populate
total greenhouse gases only from an explicitly stated overall total. Total energy excludes
office-only energy. Water withdrawal excludes consumption and discharge. Total waste means
total waste generated. Recycling rate is a 0-to-100 percentage. Targets, baselines and
future goals are not actual measurements."""
    user_content = [
        {
            "type": "text",
            "text": (
                f"Company: {company['company_name']} ({company['company_id']})\n"
                f"Required actual reporting year: {company['reporting_year']}\n\n"
                f"Fields:\n{field_lines}\n\n"
                "Return this complete object shape with evidence objects added only for values you can read:\n"
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
                    "text": page.get("location_label")
                    or f"Original report PDF page {page['page_number']}:",
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


def request_summary(company):
    """Return the auditable request description without duplicating image bytes."""
    return {
        "version": VERSION,
        "company_id": company["company_id"],
        "company_name": company["company_name"],
        "reporting_year": company["reporting_year"],
        "source_sha256": company["source_sha256"],
        "supporting_sources": [
            {key: source[key] for key in ("url", "sha256", "byte_count")}
            for source in company.get("supporting_sources", [])
        ],
        "pages": [
            {
                "page_number": page["page_number"],
                "image_sha256": page["image_sha256"],
                "image_byte_count": page["image_byte_count"],
                "pixel_width": page["pixel_width"],
                "pixel_height": page["pixel_height"],
                "location_label": page.get("location_label"),
                "supporting_source_sha256": page.get("supporting_source_sha256"),
                **image_payload(page)[2],
            }
            for page in company["pages"]
        ],
    }
