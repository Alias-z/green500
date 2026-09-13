"""Compact financial tables with explicit columns and original row references."""

from lxml import html

VERSION = "financial-indexed-columns-v4-positioned-source-facts"


def compact_positioned_table(block):
    """Render exact visual coordinates and Inline XBRL provenance for div tables."""
    rendered = []
    for row in block.get("rows", []):
        cells = []
        for cell in row.get("layout_cells", []):
            fact_text = []
            for fact in cell.get("numeric_facts", []):
                attributes = [
                    item
                    for item in (
                        fact.get("name"),
                        fact.get("context_ref"),
                        fact.get("unit_ref"),
                        f"×10^{fact['scale']}",
                        f"sign={fact['sign']}" if fact.get("sign") else None,
                        f"fact={fact['fact_id']}" if fact.get("fact_id") else None,
                    )
                    if item
                ]
                fact_text.append("; ".join(attributes))
            suffix = f" [source fact: {' | '.join(fact_text)}]" if fact_text else ""
            cells.append(f"x{cell['left']:g}: {cell['text']}{suffix}")
        rendered.append(row["id"] + " | " + " | ".join(cells))
    return "\n".join(rendered)


def compact_table(block):
    """Preserve column positions and merged-cell spans while removing HTML markup."""
    table = html.fromstring(block["text"])
    occupied, rendered = {}, []
    source_rows = {row["id"]: row for row in block.get("rows", [])}
    for row_number, row in enumerate(table.xpath(".//tr")):
        column, cells = 0, []
        for cell in row.xpath("./th|./td"):
            while occupied.get(column, 0) > row_number:
                column += 1
            colspan = max(1, int(cell.get("colspan", "1")))
            rowspan = max(1, int(cell.get("rowspan", "1")))
            end = column + colspan - 1
            text = " ".join("".join(cell.itertext()).split())
            if text:
                label = f"c{column}" if colspan == 1 else f"c{column}-c{end}"
                cells.append(label + ": " + text)
            for index in range(column, end + 1):
                occupied[index] = row_number + rowspan
            column = end + 1
        row_id = row.get("data-row-id", f"row{row_number}")
        scales = sorted(
            {
                (str(f.get("unit_ref") or "unit unspecified"), int(f["scale"]))
                for f in source_rows.get(row_id, {}).get("numeric_facts", [])
            }
        )
        derived_scale = source_rows.get(row_id, {}).get("source_scale_evidence")
        if not scales and isinstance(derived_scale, dict):
            scales = [
                (
                    "/".join(derived_scale.get("unit_refs", []))
                    or "unit unspecified",
                    int(derived_scale["scale"]),
                )
            ]
        suffix = (
            " [source numeric scales: "
            + "; ".join(f"{unit} ×10^{scale}" for unit, scale in scales)
            + "]"
            if scales
            else ""
        )
        if scales and isinstance(derived_scale, dict):
            references = ", ".join(
                f"{item['block_id']}:{item['row_id']}"
                for item in derived_scale.get("source_rows", [])
            )
            suffix += f" [scale verified from duplicate source rows: {references}]"
        rendered.append(row_id + " | " + " | ".join(cells) + suffix)
    return "\n".join(rendered)


def render_financial_input(document):
    """Render source paragraphs and indexed table columns without changing evidence."""
    parts = [
        "Source: " + document["source_url"],
        "Tables use explicit zero-based columns c0, c1, etc. Empty cells retain their column positions but are omitted. c2-c4 denotes a merged cell. Match values to the fiscal-year column. Each line starts with the original row_id; its section heading is block_id.",
    ]
    for block in document["blocks"]:
        parts.append(f"\n## [{block['id']}] {block['kind']}")
        if block["context"]:
            parts.append("Context: " + block["context"])
        if block.get("layout") == "absolute_positioned_divs":
            parts.append(
                "Cells use exact source x pixel positions; Inline XBRL context identifies "
                "the period and source scale.\n" + compact_positioned_table(block)
            )
        else:
            parts.append(
                compact_table(block) if block["kind"] == "table" else block["text"]
            )
    return "\n\n".join(parts)
