# Reused mechanisms

The following granny_data files informed the standalone implementation. Their product-specific schemas, proxy account machinery, service configuration and credentials are not copied into Green500 source code.

| Original component | Reused behavior |
|---|---|
| `cli/online_research/access_plan.py` | HTTP-first acquisition, bounded bytes and time, original response and attempt evidence |
| `cli/online_research/pdf_text.py` | Resource-limited PDF subprocess, page/line references and incomplete-text reporting |
| `scrapers/goods/subspecs/_GOODS_PIPELINE_SPEC.md` | Await exact durable writes; keep blocking writes outside the Scrapy event loop |
| `scrapers/goods/postgres_raw_writer.py` | Parameterized transactions and idempotent records |
| `cli/online_research/run_history.py` | Preserve evidence and verify hashes; Green500 stores operational records in PostgreSQL |
| `cli/online_research/ai.py` | Typed model results, source citations and replay inputs |

The source inspection inventory and SHA-256 values are retained in the green500 agent's external implementation evidence. Dependencies are managed by Green500's own lockfiles. This folder is a standalone project; it does not depend on a neighboring granny_data checkout at runtime.

Green500 calls models only through an OpenAI-compatible HTTP endpoint. An operator may point that
endpoint at a public provider, a local model server or a separately deployed gateway. Account
rotation, plan admission, shared observability, proxy rotation, browser swarms and challenge
handling remain outside this repository. Source discovery is documented as a read-only repository
skill; deterministic intake starts from the candidate URL and downloaded bytes.
