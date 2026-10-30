# Observability (Requirement 7)

Goal: know **when** the agent fails, **why**, and be able to replay **exactly what happened**: messages, model calls, SQL, and the decisions of every guard.

## 1. Event model

Every user turn has a `trace_id`. Each event also carries `session_id` (the conversation), `user_id` and a timestamp.

| Event | Key fields | Answers the question |
|---|---|---|
| `turn_start` / `turn_end` | input preview (redacted) · status (`ok/blocked/degraded/error/awaiting_confirmation`), latency, tokens in/out/thinking, LLM calls, cost, SQL count, flags, answer preview | Did the turn succeed? How long did it take and how much did it cost? |
| `guard_verdict` / `guardrail_block` / `guard_degraded` | source (prefilter/classifier/sql_guard), category, language, reason | Why was this refused? Are we under attack? |
| `context_loaded` | persona version, preferences keys, golden hits + scores, retrieval mode | Which instructions and examples shaped the answer? |
| `llm_call` / `llm_error` | model, tokens (incl. thinking), latency, finish reason, cost, number of prompt messages, the last prompt message, the reply text and the tool calls it requested (all redacted, truncated) | Which model answered, with what input, and what did it decide? |
| `retry` / `llm_failover` / `fallback_used` / `llm_unavailable` / `dependency_down` | dependency, attempt, error type | Is a provider degrading? |
| `tool_call` / `tool_error` | tool, args (redacted), status, latency | What did the agent do? |
| `sql_attempt` | attempt #, purpose, the model's SQL, **the scoped SQL BigQuery actually ran**, status (`ok/error/rejected/too_expensive/unavailable`), rows, bytes, cache hit, error, **the first 5 result rows the model saw** (after PII/privacy filtering) | Why did the data step fail? Did it self-correct? Was the answer grounded in these rows? |
| `empty_result` | suggested valid values | Was the filter wrong? |
| `pii_redaction` | layer (query_result/report/final_answer), counts | Did anything sensitive get near the output? |
| `privacy_suppression` | demographic columns dropped from customer-level rows, groups < 5 customers suppressed | Did small-group protection change a result? |
| `turn_messages` | every message of the turn: user text, assistant replies, tool calls with arguments, tool results (redacted, truncated) | What exactly was said between the user, the model and the tools? |
| `confirmation_requested/shown/accepted/cancelled`, `deletion_executed`, `deletion_undone` | ids, counts, criteria | Who deleted what, when, and with what confirmation? |
| `budget_exhausted` / `sql_budget_exhausted` | counters | Is the agent looping? |
| `report_saved`, `preference_set`, `persona_published`, `feedback` | — | Product analytics and auditing |
| `error` | error type, message, traceback | Root cause of crashes |

User text is **truncated and PII-redacted** before it is logged. Logs never contain raw PII.

## 2. Agent-level metrics

These are computed by [`compute_metrics`](../src/retail_agent/observability/metrics.py) and shown in `/stats` and on the web *Traces & metrics* page. The keys below are exactly what it returns:

| Group | Key | Meaning |
|---|---|---|
| Volume | `turns`, `status` | turns in the window, count per status |
| Reliability | `success_rate` | ok ÷ (ok + error + degraded); blocks and pending confirmations are intended outcomes |
| | `block_rate` | blocked ÷ all turns |
| | `errors_by_type`, `llm_fallbacks`, `retries` | failures by exception type, fallback-model activations, retry attempts |
| Performance | `latency_ms_p50`, `latency_ms_p95` | turn latency |
| Cost | `avg_tokens_per_turn`, `avg_reasoning_tokens_per_turn`, `avg_llm_calls_per_turn` | model usage per turn |
| | `est_cost_usd_total`, `est_cost_usd_per_turn` | estimated LLM cost |
| SQL quality | `sql.turns_with_sql`, `sql.first_try_success_rate`, `sql.self_corrected`, `sql.gave_up`, `sql.empty_results` | how often the first query works, how often the agent recovers |
| SQL cost | `sql.bytes_scanned_total`, `sql.avg_bytes_per_sql_turn` | BigQuery bytes (cache hits excluded) |
| Safety | `guardrail_blocks` (by category), `pii_redactions`, `pii_redactions_by_layer`, `privacy_suppressions`, `confirmations` | blocks, redactions per layer (a `final_answer` redaction should trigger an investigation), small-group protection, confirmation shown/accepted/cancelled |
| Satisfaction | `feedback` | 👍 / 👎 counts |

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
   - each LLM call (model, tokens, latency, the reply and the tools it asked for);
   - each SQL attempt: the model's SQL, the scoped SQL BigQuery ran, the error, and the first result rows;
   - retries and fallbacks;
   - the final status.
3. The conversation itself is in the trace: the `turn_messages` event holds every message of the turn (user text, model replies, tool calls with arguments, tool results), redacted. The web Traces page shows it under *Conversation in this turn*. For the whole session, run `retail-agent transcript <session_id> --user <id>` or `/transcript` in the chat; it reads the LangGraph checkpoint (owner or admin only). Langfuse adds the same view as a nested trace when it is enabled, but nothing depends on it.
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
