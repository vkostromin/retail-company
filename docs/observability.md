# Observability (Requirement 7)

Goal: know **when** the agent fails, **why**, and be able to replay **exactly what happened**: messages, model calls, SQL, and the decisions of every guard.

## 1. Event model

Every user turn has a `trace_id`. Each event also carries `session_id` (the conversation), `user_id` and a timestamp.

| Event | Key fields | Answers the question |
|---|---|---|
| `turn_start` / `turn_end` | input preview (redacted) · status (`ok/blocked/degraded/error/awaiting_confirmation`), latency, tokens in/out, LLM calls, cost, SQL count, flags | Did the turn succeed? How long did it take and how much did it cost? |
| `guard_verdict` / `guardrail_block` / `guard_degraded` | source (prefilter/classifier/sql_guard), category, language, reason | Why was this refused? Are we under attack? |
| `context_loaded` | persona version, preferences keys, golden hits + scores, retrieval mode | Which instructions and examples shaped the answer? |
| `llm_call` / `llm_error` | model, tokens, latency, finish reason, cost | Which model answered? Was it slow or truncated? |
| `retry` / `llm_failover` / `fallback_used` / `llm_unavailable` / `dependency_down` | dependency, attempt, error type | Is a provider degrading? |
| `tool_call` / `tool_error` | tool, args (redacted), status, latency | What did the agent do? |
| `sql_attempt` | attempt #, purpose, SQL, status (`ok/error/rejected/too_expensive/unavailable`), rows, bytes, cache hit, error | Why did the data step fail? Did it self-correct? |
| `empty_result` | suggested valid values | Was the filter wrong? |
| `pii_redaction` | layer (query_result/report/final_answer), counts | Did anything sensitive get near the output? |
| `confirmation_requested/shown/accepted/cancelled`, `deletion_executed`, `deletion_undone` | ids, counts, criteria | Who deleted what, when, and with what confirmation? |
| `budget_exhausted` / `sql_budget_exhausted` | counters | Is the agent looping? |
| `report_saved`, `preference_set`, `persona_published`, `feedback` | — | Product analytics and auditing |
| `error` | error type, message, traceback | Root cause of crashes |

User text is **truncated and PII-redacted** before it is logged. Logs never contain raw PII.

## 2. Agent-level metrics

These are computed by [`metrics.py`](../src/retail_agent/observability/metrics.py) and shown in `/stats` and on the web *Traces & metrics* page:

- **Reliability:**
  - turn success rate (ok ÷ (ok + error + degraded); blocks and pending confirmations are intended outcomes);
  - errors by type;
  - fallback activations;
  - retries.
- **Quality proxies:**
  - SQL first-try success rate;
  - self-corrected turns;
  - turns that gave up;
  - empty-result rate;
  - 👍/👎.
- **Safety:**
  - guardrail blocks by category;
  - PII redactions by layer (a redaction in a final answer should trigger an investigation);
  - confirmation accept/cancel.
- **Performance and cost:** p50/p95 latency, average tokens per turn, estimated cost per turn and in total, LLM calls per turn, bytes scanned.

**Production alerts** (Cloud Monitoring, from log-based metrics):

| Condition | Severity |
|---|---|
| `llm_unavailable` > 2% of turns over 10 min | page |
| turn error rate > 5% over 15 min | page |
| p95 latency > 60 s over 30 min | ticket |
| `pii_redaction{layer=final_answer}` > 0 | ticket (review the trace) |
| guardrail blocks > 5× baseline per user | security ticket (possible probing) |
| 👎 rate > 30% in a day | product review |
| daily estimated cost > budget | ticket |

## 3. Deep-dive debugging workflow

1. The user reports a problem. Every error message includes `reference: <trace_id>`, and every CLI answer prints its trace id.
2. Run `retail-agent trace <id>`, use `/trace` in the chat, or open the web *Traces* page and pick the turn. This shows the full timeline:
   - the guard verdict;
   - persona version and golden examples used;
   - each LLM call (model, tokens, latency);
   - each SQL attempt with its exact SQL and BigQuery error;
   - retries and fallbacks;
   - the final status.
3. To see the conversation itself, the session's LangGraph checkpoint holds every message, including tool calls and tool results. `retail-agent chat --user X --session <id>` resumes it, and Langfuse shows the same thing as a nested trace when it is enabled.
4. Aggregate: `/stats` or the Traces page with filters by user and status. In production, SQL over the BigQuery log sink, e.g. *"top SQL error messages this week"* or *"sessions where self-correction failed"*.

Example: a real trace of a turn where the primary model was down (fault injection) shows `retry ×2 → llm_failover → llm_call gemini-2.5-flash → fallback_used`, then `sql_attempt #2 ok`, then `turn_end ok flags=[fallback]`.

## 4. Prototype vs production

| Prototype | Production |
|---|---|
| JSONL files `.data/traces/YYYY-MM-DD.jsonl` (append-only, thread-safe) | Cloud Logging (structured JSON) → **BigQuery log sink** for SQL analysis |
| `/trace`, `/stats`, web Traces page | Looker Studio / Grafana dashboards, Cloud Monitoring alerts |
| Langfuse `CallbackHandler` when `LANGFUSE_*` is set | Langfuse (self-hosted on Cloud Run + Cloud SQL) for LLM traces, prompt versions, eval scores |
| `turn.progress()` spinner messages | OpenTelemetry spans per node, tool and LLM call → **Cloud Trace**; SSE progress to clients |
| BigQuery job labels (`app`, `user`, `session`) | the same labels → per-user cost attribution in `INFORMATION_SCHEMA.JOBS` |
