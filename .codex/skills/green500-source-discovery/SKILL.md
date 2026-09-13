---
name: green500-source-discovery
description: Find and verify official issuer reports, reporting hubs, and linked data files for Green500 source gaps or freshness reviews. Use for source discovery and evidence preparation; exclude model extraction, database writes, bulk crawling, and access-control bypass.
---

# Green500 source discovery

Find the strongest official source candidate and return evidence another agent can review or register. Keep discovery read-only. Never write Green500 production data or claim that discovery alone completed source registration.

## Admission

Any saved report assigned to a Green500 category is admissible input. Unknown, expired, or `newer_available` freshness does not block discovery or evidence preparation. Preserve freshness as metadata and use it only to sort or compare candidates.

## Inputs

Use the task and repository evidence to resolve:

- Company CIK, ticker, canonical name, and known official website.
- Requested categories: `financial_report`, `environment_report`, `social_employee`, `financial_targets`, or `climate_targets`.
- Check time.
- Existing saved report URL, hash, reporting period, and freshness metadata when available.
- Caller-provided agent artifact directory when downloaded evidence is needed.

Stop with `unknown` when company identity or the official issuer domain cannot be established safely.

## Source order

Prefer evidence in this order:

1. The issuer's current reporting, sustainability, responsibility, governance, or investor-relations hub.
2. A report or data file linked directly from that official hub.
3. A workbook or supporting file linked from an official report.
4. An official issuer CDN or filing repository whose relationship to the issuer is established by an official page.
5. Search results and secondary report directories as discovery leads only.

A filename year, report title, URL path, search snippet, download time, or HTTP `Last-Modified` value does not independently establish publication date, reporting period, or currentness.

## Verification

For each serious candidate:

- Preserve the requested URL and final URL.
- Confirm public HTTP(S), content type, file signature, title, and issuer identity.
- Record the official page or saved report that links to the candidate.
- Separate publication date, reporting period, acquisition time, target year, and freshness metadata.
- Inspect HTML text, PDF text, document links, metadata, and tables as needed.
- Record exact evidence text with its page, row, section, or link locator.
- Compare official navigation and adjacent editions before describing currentness.
- Classify only categories supported by the source body.
- For target categories, record `current`, `achieved`, `retired`, or `unknown`.
- When bytes are downloaded, save them only in the caller-provided agent artifact directory and record SHA-256 and byte count.

Use `unknown` when evidence is incomplete. Use `blocked` when the official source requires unavailable authorization or cannot be read through ordinary public HTTP.

## Output

Return one JSON-compatible record and a concise evidence summary:

```json
{
  "version": "green500-source-discovery-v1",
  "company": {
    "cik": "0000000000",
    "ticker": "EXAMPLE",
    "name": "Example Company"
  },
  "requested_categories": ["environment_report"],
  "checked_at": "ISO-8601 timestamp",
  "freshness_metadata": {
    "status": "latest_verified | newer_available | unknown",
    "checked_at": null,
    "valid_until": null
  },
  "currentness": "current_candidate | newer_candidate | unknown | blocked",
  "selected_candidate": {
    "url": "https://issuer.example/report.pdf",
    "final_url": "https://issuer.example/report.pdf",
    "title": "Example Report",
    "content_type": "application/pdf",
    "publication_date": null,
    "report_period": {
      "start": null,
      "end": null,
      "label": null
    },
    "categories": ["environment_report"],
    "target_status": "unknown",
    "sha256": null,
    "byte_count": null,
    "artifact_path": null
  },
  "evidence": [
    {
      "url": "https://issuer.example/reporting",
      "kind": "official_reporting_hub",
      "locator": "page section or document page",
      "text": "Bounded exact evidence text"
    }
  ],
  "rejected_candidates": [
    {
      "url": "https://issuer.example/older-report.pdf",
      "reason": "Official archive identifies a newer edition."
    }
  ],
  "blockers": []
}
```

Keep rejected candidates only when they explain currentness, identity, or category selection. Do not include search-result inventories or browsing history.

## Boundaries

- Do not insert or update `report_sources`, `documents`, reviews, tasks, or schedules.
- Do not call extraction models or reuse prior model output as source evidence.
- Do not download into `data/objects/` or another production path.
- Do not bypass authentication, challenges, robots controls, or publisher restrictions.
- Do not send messages, publish findings, deploy services, or restart runtime components.
- A proposed `latest_verified` review still requires the repository's normal hash-bound validation and registration workflow.

## Excluded infrastructure

Keep this skill independent of:

- Granny browser gateways, browser swarms, and challenge solvers.
- Proxy pools, fingerprint rotation, CAPTCHA handling, and anti-bot bypass.
- Ops queues, workers, schedulers, leases, heartbeats, and service control.
- Model-provider routing, prompts, API keys, and inference receipts.
- External-agent orchestration, terminal multiplexers, and checkout ownership.
- PostgreSQL writers, object-store backup, R2/B2 publishing, deployment, and observability systems.

Use ordinary web search, public HTTP clients, and local HTML/PDF inspection available in the current environment.
