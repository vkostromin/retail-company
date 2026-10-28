# CLAUDE.md - project context for AI-assisted development

Retail data-analysis chat agent (OpsFleet assessment). LangGraph + Gemini + BigQuery `bigquery-public-data.thelook_ecommerce`.

## Commands
- `make test` - offline tests (fakes; no credentials). Run after every change.
- `make lint` - ruff.
- `make redteam` / `make eval` - live suites (need GCP + Gemini). Red team must stay 100%.
- `retail-agent chat --user alice` (users: ceo, alice, bob, carol) · `make web` · `retail-agent doctor`.
- Fault injection: `RA_FAKE_FAIL=llm_primary,bigquery_once,embeddings`.

## Architecture rules
- The LLM never enforces safety. Scope/PII/cost are enforced in `safety/sql_guard.py` (scoped subqueries, parameters), destructive actions only in the deterministic `confirm` → `execute_deletion` nodes.
- Every external call goes through `LLMGateway` or `BigQueryRunner` (retry, breaker, fallback). Never call models/BigQuery directly from tools.
- New capability = new `ToolSpec` in `tools/` (pydantic args + function returning a dict). Outward-facing tools must set `pending_action` and reuse the confirmation node.
- Emit trace events (`turn.emit`) for every decision; never log raw user text without `safe_preview`.
- Persona = style only; rules live in `agent/prompts.py` above the persona.
- `src/retail_agent/data/bq_client.py` is the provided starter - extend, keep its original API.
