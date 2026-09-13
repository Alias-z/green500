"""Resource-bounded table extraction for selected PDF pages."""

import argparse
import io
import json
import resource
import sys

import pdfplumber


def _clean_tables(tables):
    """Remove empty rows and columns without joining or rewriting source cells."""
    clean_tables = []
    for table in tables:
        rows = [[" ".join(str(cell or "").split()) for cell in row] for row in table]
        rows = [row for row in rows if any(row)]
        if len(rows) < 2:
            continue
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        columns = [index for index in range(width) if any(row[index] for row in rows)]
        rows = [[row[index] for index in columns] for row in rows]
        if len(columns) < 2 or sum(len(cell) for row in rows for cell in row) > 20_000:
            continue
        clean_tables.append(rows)
    return clean_tables


def main():
    """Read PDF bytes and return tables from explicitly selected pages."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", required=True)
    arguments = parser.parse_args()
    page_numbers = [int(value) for value in arguments.pages.split(",")]
    if (
        not page_numbers
        or len(page_numbers) > 20
        or any(page < 1 for page in page_numbers)
    ):
        raise ValueError("Pages must contain one to twenty positive page numbers.")
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    body = sys.stdin.buffer.read(128_000_001)
    if len(body) > 128_000_000:
        raise ValueError("PDF exceeds byte limit.")
    result = []
    with pdfplumber.open(io.BytesIO(body)) as document:
        if max(page_numbers) > len(document.pages):
            raise ValueError("Requested page exceeds the PDF page count.")
        for page_number in page_numbers:
            page = document.pages[page_number - 1]
            default_tables = _clean_tables(page.extract_tables())
            largest_default = max((len(table) for table in default_tables), default=0)
            default_cells = sum(
                bool(cell) for table in default_tables for row in table for cell in row
            )
            if largest_default >= 5 and default_cells >= 20:
                tables = default_tables
                strategy = "drawn_lines"
            else:
                tables = _clean_tables(
                    page.extract_tables(
                        {
                            "vertical_strategy": "text",
                            "horizontal_strategy": "text",
                            "intersection_tolerance": 5,
                            "snap_tolerance": 3,
                            "join_tolerance": 3,
                            "min_words_vertical": 2,
                            "min_words_horizontal": 1,
                        }
                    )
                )
                strategy = "aligned_text"
            result.append({"page": page_number, "strategy": strategy, "tables": tables})
    print(json.dumps({"version": "pdf-tables-v2", "pages": result}))


if __name__ == "__main__":
    main()
