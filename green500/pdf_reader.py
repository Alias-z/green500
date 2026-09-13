"""Resource-bounded PDF text extraction entry point."""

import json
import resource
import subprocess
import sys

import pymupdf


def main() -> None:
    """Read source bytes and emit page-scoped lines with explicit coverage gaps."""
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    body = sys.stdin.buffer.read(256_000_001)
    if len(body) > 256_000_000:
        raise ValueError("PDF exceeds byte limit.")
    lines, warnings = [], []
    with pymupdf.open(stream=body, filetype="pdf") as document:
        if document.needs_pass or document.page_count > 500:
            raise ValueError("Encrypted PDF or page limit exceeded.")
        page_texts = [page.get_text("text", sort=False) or "" for page in document]
        used_ocr = not any(text.strip() for text in page_texts)
        if used_ocr:
            page_texts = []
            for page in document:
                image = page.get_pixmap(
                    matrix=pymupdf.Matrix(1.5, 1.5),
                    colorspace=pymupdf.csGRAY,
                    alpha=False,
                ).tobytes("png")
                try:
                    result = subprocess.run(
                        [
                            "/usr/bin/tesseract",
                            "stdin",
                            "stdout",
                            "-l",
                            "eng",
                            "--psm",
                            "6",
                        ],
                        input=image,
                        capture_output=True,
                        timeout=60,
                        check=False,
                    )
                    page_texts.append(
                        result.stdout.decode("utf-8", errors="replace")
                    )
                except subprocess.TimeoutExpired:
                    page_texts.append("")
                    warnings.append(
                        f"Page {len(page_texts)} OCR exceeded its resource limit."
                    )
        for page_number, text in enumerate(page_texts, 1):
            # Plain extraction preserves complete numeric tokens. PyMuPDF handles
            # large sustainability reports that exceeded pypdf's memory or CPU
            # limits while retaining page-scoped source citations.
            if not text.strip():
                warnings.append(
                    f"Page {page_number} has no text layer; visual review may be needed."
                )
            for line_number, text_line in enumerate(text.splitlines(), 1):
                if text_line.strip():
                    lines.append(
                        {
                            "id": f"P{page_number}L{line_number}",
                            "page": page_number,
                            "text": text_line.strip(),
                        }
                    )
    print(
        json.dumps(
            {
                "parser_version": (
                    "source-lines-v4-pymupdf-ocr"
                    if used_ocr
                    else "source-lines-v4-pymupdf"
                ),
                "lines": lines,
                "is_complete": not warnings,
                "warnings": warnings,
            }
        )
    )


if __name__ == "__main__":
    main()
