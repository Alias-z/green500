"""Pure checks for typed SBTi parsing, conservative matching and persistence calls."""

from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

from openpyxl import Workbook
from scrapy.http import HtmlResponse

from green500 import data_store, target_sources

COMPANY_HEADERS = [
    "sbti_id",
    "company_name",
    "near_term_status",
    "long_term_status",
    "net_zero_status",
    "date_updated",
]
TARGET_HEADERS = [
    "row_entry_id",
    "sbti_id",
    "company_name",
    "status",
    "target_wording",
    "date_published",
]


def workbook_bytes(sheet_name, headers, rows):
    """Create an in-memory XLSX fixture with real typed cells."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def company_workbook(rows):
    """Build a current-company workbook with the required SBTi columns."""
    return workbook_bytes("Data", COMPANY_HEADERS, rows)


def target_workbook(rows):
    """Build a target-detail workbook with stable row identifiers."""
    return workbook_bytes("WebsiteData", TARGET_HEADERS, rows)


def test_typed_workbook_rows_keep_source_rows_dates_and_ids():
    """Excel dates and numeric SBTi IDs become JSON values without losing row identity."""
    rows = target_sources.parse_workbook_rows(
        company_workbook(
            [
                [
                    40005011.0,
                    "Microsoft Corporation",
                    "Targets set",
                    "",
                    "Commitment removed",
                    datetime(2026, 9, 12, 1, 0),  # noqa: DTZ001 - XLSX stores naive dates
                ]
            ]
        ),
        sheet_name="Data",
        required_columns=target_sources.COMPANY_REQUIRED_COLUMNS,
    )
    assert rows == [
        {
            "sbti_id": "40005011",
            "company_name": "Microsoft Corporation",
            "near_term_status": "Targets set",
            "long_term_status": None,
            "net_zero_status": "Commitment removed",
            "date_updated": "2026-09-12",
            "source_row": 2,
        }
    ]


def test_name_matching_is_unique_and_does_not_replace_legal_suffixes():
    """Case, punctuation and share classes match; broader legal-name changes do not."""
    assert (
        target_sources.normalize_company_name("ACME, Inc. (Class A)") == "acme inc"
    )
    source_rows = [
        {"sbti_id": "1", "company_name": "ACME INC", "source_row": 2},
        {"sbti_id": "2", "company_name": "Twin plc", "source_row": 3},
        {"sbti_id": "3", "company_name": "TWIN PLC", "source_row": 4},
        {"sbti_id": "4", "company_name": "Mapped source", "source_row": 5},
    ]
    result = target_sources.match_current_companies(
        [
            {"cik": "0000000001", "name": "Acme, Inc. (Class A)"},
            {"cik": "0000000002", "name": "Twin plc"},
            {"cik": "0000000003", "name": "No source"},
            {"cik": "0000000004", "name": "Mapped Holdings"},
            {"cik": "0000000005", "name": "Acme Corporation"},
        ],
        source_rows,
        sbti_id_overrides={"4": 4.0},
    )
    assert [(row["cik"], row["match_method"]) for row in result["matched"]] == [
        ("0000000001", "unique_name"),
        ("0000000004", "sbti_id_override"),
    ]
    assert [row["cik"] for row in result["ambiguous"]] == ["0000000002"]
    assert [row["cik"] for row in result["unmatched"]] == [
        "0000000003",
        "0000000005",
    ]


def test_observations_preserve_original_status_and_target_rows():
    """Current statuses and every target row retain workbook-specific evidence."""
    source_company = {
        "sbti_id": "42",
        "company_name": "Example Inc",
        "near_term_status": "Targets set",
        "long_term_status": "",
        "net_zero_status": "Committed",
        "date_updated": "2026-09-12",
        "source_row": 9,
    }
    targets = [
        {
            "row_entry_id": "first-42",
            "sbti_id": "42",
            "company_name": "Example Inc",
            "status": "NA",
            "target_wording": "Reduce emissions.",
            "date_published": "2025-01-02",
            "source_row": 15,
        }
    ]
    statuses = target_sources.status_observations(source_company)
    assert [row["metric_code"] for row in statuses] == [
        "near_term_target_status",
        "net_zero_target_status",
    ]
    assert statuses[0]["source_record"]["source_row"] == 9
    observation = target_sources.climate_targets_observation(
        source_company, targets
    )
    assert observation["value"][0]["source_row"] == 15
    assert observation["source_record"]["target_rows"] == [
        {
            "source_row": 15,
            "row_entry_id": "first-42",
            "sbti_id": "42",
            "status": "NA",
            "date_published": "2025-01-02",
        }
    ]
    assert target_sources.climate_targets_observation(source_company, []) is None


def test_dashboard_resolves_only_the_two_named_workbooks():
    """The spider follows observed SBTi workbook links and ignores unrelated files."""
    response = HtmlResponse(
        target_sources.TARGET_DASHBOARD_URL,
        body=b"""
        <a href="https://files.sciencebasedtargets.org/production/files/companies-excel.xlsx">Companies</a>
        <a href="/production/files/targets-excel.xlsx">Targets</a>
        <a href="/production/files/dictionary.xlsx">Dictionary</a>
        """,
        encoding="utf-8",
    )
    assert target_sources.resolve_workbook_links(response) == {
        "companies": target_sources.VERIFIED_WORKBOOK_URLS["companies"],
        "targets": "https://sciencebasedtargets.org/production/files/targets-excel.xlsx",
    }


def test_collect_saves_both_raw_files_before_observations(monkeypatch):
    """A task versions both XLSX bodies, then saves matched values and explicit outcomes."""
    companies_body = company_workbook(
        [
            [
                42.0,
                "Example Inc",
                "Targets set",
                "",
                "Committed",
                datetime(2026, 9, 12),  # noqa: DTZ001 - XLSX stores naive dates
            ]
        ]
    )
    targets_body = target_workbook(
        [
            [
                "target-42",
                42.0,
                "Example Inc",
                "NA",
                "Reduce emissions.",
                datetime(2025, 1, 2),  # noqa: DTZ001 - XLSX stores naive dates
            ]
        ]
    )
    downloads = {
        "companies": {
            "url": target_sources.VERIFIED_WORKBOOK_URLS["companies"],
            "body": companies_body,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        "targets": {
            "url": target_sources.VERIFIED_WORKBOOK_URLS["targets"],
            "body": targets_body,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    }
    calls = []
    selected_company_ciks = []

    def save_dataset(*args, **kwargs):
        calls.append(("dataset", kwargs["url"]))
        return {
            "id": len(calls),
            "sha256": str(len(calls)),
            "url": kwargs["url"],
            "last_checked_at": datetime(2026, 9, 12, 12, 0),  # noqa: DTZ001
        }

    def save_observations(*args, **kwargs):
        calls.append(
            (
                "observations",
                kwargs["company_cik"],
                kwargs["match_method"],
                [row["metric_code"] for row in kwargs["observations"]],
            )
        )
        return len(kwargs["observations"])

    def record_check(*args, **kwargs):
        calls.append(("check", kwargs["company_cik"], kwargs["status"]))

    monkeypatch.setattr(target_sources, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        target_sources,
        "_current_companies",
        lambda settings, company_cik: (
            selected_company_ciks.append(company_cik)
            or [{"cik": "0000000042", "name": "Example, Inc. (Class A)"}]
        ),
    )
    monkeypatch.setattr(
        target_sources,
        "_download_sbti_workbooks",
        lambda settings, dashboard_url: downloads,
    )
    monkeypatch.setattr(data_store, "save_dataset", save_dataset)
    monkeypatch.setattr(data_store, "save_observations", save_observations)
    monkeypatch.setattr(data_store, "record_check", record_check)

    result = target_sources.collect_targets_task(
        {"id": 7, "company_cik": "0000000042", "input": {}}
    )

    assert [call[0] for call in calls[:2]] == ["dataset", "dataset"]
    assert selected_company_ciks == ["0000000042"]
    assert result["companies_dataset"]["last_checked_at"] == "2026-09-12T12:00:00"
    assert result["matched"] == [
        {
            "cik": "0000000042",
            "company_name": "Example, Inc. (Class A)",
            "sbti_id": "42",
            "match_method": "unique_name",
        }
    ]
    assert result["unmatched"] == []
    assert ("check", "0000000042", "succeeded") in calls
    observation_calls = [call for call in calls if call[0] == "observations"]
    assert observation_calls == [
        (
            "observations",
            "0000000042",
            "unique_name",
            ["near_term_target_status", "net_zero_target_status"],
        ),
        ("observations", "0000000042", "unique_name", ["climate_targets"]),
    ]
