"""Model contract and source-selection terms for financial target reports."""

from green500.processing.financial_targets_schema import VERDEXFinancialTargets

MODEL = VERDEXFinancialTargets

KEYWORDS = (
    "guidance",
    "outlook",
    "target",
    "expect",
    "forecast",
    "revenue growth",
    "operating margin",
    "operating income",
    "earnings per share",
    "eps",
    "return on equity",
    "return on invested capital",
    "free cash flow",
    "operating cash flow",
    "capital expenditures",
    "capex",
    "cost savings",
    "capital return",
    "debt reduction",
    "leverage",
    "liquidity",
    "investor day",
)

INSTRUCTIONS = """Extract only explicit forward-looking financial guidance, targets, or
strategic financial objectives from the supplied verified issuer evidence. Return one JSON object
that validates against VERDEXFinancialTargets. Targets are separate from historical financial
actuals and are not financial-performance ratings.

Copy company_id and company_name exactly from metadata. Set reporting_year to the year represented
by the source disclosure. Copy source_document and source_url exactly into every target, and retain
the supplied PDF page or HTML block identifier in source_page or source_section. Never invent a
source, page, reporting period, unit, target year, scope, status, or numeric value.

Preserve a point target as target_value and a stated range as target_min and target_max. Do not
replace a range with its midpoint. Keep target values separate from actual performance. Preserve
baseline year, target year, target period, direction, business and geographic scope, disclosure
date, status, and management's confidence limitations. Keep quarterly guidance, annual guidance,
and explicitly long-term objectives distinct. A qualitative phrase such as strong growth is not a
number and must not become a target.

Use USD only when the source explicitly states USD. Preserve percent, USD_per_share, ratio, and
other source units exactly enough for deterministic conversion. Never apply exchange rates. A
revised target must retain revised=true, its previous point or range when stated, and the revision
reason. A withdrawn target remains status=withdrawn; do not silently present it as active. Achieved
or exceeded historical targets remain distinct from current forward guidance.

Classify the stated metric using target_type. Put specialized bank, insurer, REIT, airline, or
other sector measures in industry_specific_targets rather than mapping them to a different common
metric. Do not calculate target achievement, gaps, historical growth, peer ranks, scores, or
investment recommendations. Use only the selected source evidence and return JSON without Markdown.
"""
