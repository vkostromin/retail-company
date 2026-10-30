# Technical design: Retail Insights data analysis agent

> **Audience:** OpsFleet reviewers (Team Lead, Harry).
>
> **How to read this:**
> - §1–3: the system as a whole.
> - §4: each requirement (#1–#8) in turn, giving the production design, what the prototype implements, and the trade-offs.
> - §5–9: errors, security, cost, operations and extensibility.
> - §10: limitations and next steps.
>
> **Diagrams** are in [architecture.md](architecture.md). Requirement #6 (Quality Assurance) has its own document, [evaluation.md](evaluation.md), and Requirement #7 (Observability) has [observability.md](observability.md).

---

## 1. Problem and principles

Non-technical executives want to talk to their sales data. In the same conversation they want to ask "why", compare, drill down, and get a report with action items. The data is raw transaction logs that contain PII, and each executive may only see *their* products.

Five principles drive every decision below:

1. **Never trust the LLM with enforcement.** The model is a planner and writer. Identity, data scope, PII protection, cost limits and destructive actions are all enforced by deterministic code and, in production, by the database itself. Prompts make the model *behave*; code makes it *safe*.
2. **Ground answers in expert knowledge.** Analysts have already decided what "churn", "net revenue" and "underspending" mean. The agent retrieves those definitions (the Golden Trios) instead of inventing them.
3. **Fail soft, never crash.** Every dependency has a timeout, a bounded retry policy and a fallback. Every turn ends with a useful message and a trace id.
4. **Everything is observable.** Every decision (guard verdict, retrieved examples, SQL attempt, retry, fallback, confirmation) is a structured event linked by `trace_id` / `session_id`.
5. **Change behaviour without deploys.** The persona, user preferences, golden trios and entitlements are data, not code.

## 2. Technology choices

| Building block | Production choice | Prototype | Why |
|---|---|---|---|
| Cloud | **Google Cloud** | Your GCP project | The data already lives in BigQuery, and Gemini, IAM, DLP and Model Armor are native. A single cloud keeps data residency, VPC Service Controls and IAM consistent. |
| Agent framework | **LangGraph** | LangGraph 1.2 | An explicit state machine rather than a free-running agent loop. It provides **`interrupt()` + checkpointer** for human-in-the-loop confirmation that survives restarts and can be resumed on another instance. Deterministic nodes (guards, confirmation, execution) sit next to LLM nodes, and the graph is easy to test with fakes. It is model-agnostic and has first-class Langfuse/LangSmith/OTel integration. |
| LLM: agent loop | **Gemini 3.8 Flash** (`gemini-3.8-flash`) | same | Strong tool calling and SQL ability, low latency and cost. Most turns are 10–25k tokens, about $0.005–0.015 each. |
| LLM: fallback | **Gemini 2.5 Flash** | same | A *different generation*, so it has independent capacity and failure modes. Verified live that a conversation started on Gemini 3.x continues on 2.5. |
| LLM: guard | **Gemini Flash-Lite** (`gemini-3.5-flash-lite`) | same | About 0.5–1 s and less than $0.0001 per classification. Multilingual. |
| LLM: judge | **Gemini 3.1 Pro** (`gemini-3.1-pro-preview`) | same | Stronger than the model under test. The judge checks factual agreement with a reference result set and returns a strict JSON rubric; 3.1 Pro has the best factual accuracy and the most stable structured output for that, and is cheaper and far faster than Gemini 4 Argon. Argon is the better choice only for judging long multi-step agent trajectories or very long contexts. |
| Embeddings | **Gemini embeddings** (`gemini-embedding-001`, 768-d) | same | Same vendor, same auth path, multilingual. |
| Vector index | **Vertex AI Vector Search** (or **pgvector** at < 100k trios) | in-memory numpy plus BM25 fallback | The trio count is small; pgvector avoids another service. Vector Search is used when the bucket grows or latency matters. |
| Warehouse | **BigQuery** via **authorized views + row-level access policies + policy tags / dynamic masking** | `sql_guard` scope rewrite (same contract, in code) | Enforcement happens in the database: even a bug in the agent cannot read PII or another user's products. |
| SQL safety | **sqlglot** AST validation | same | Real parsing in the BigQuery dialect. Regex would be bypassable. |
| Operational DB | **Cloud SQL for PostgreSQL** | SQLite (WAL) | Holds checkpoints, reports, the audit log, preferences and persona versions in one transactional store. |
| Cache / rate limits | **Memorystore (Redis)** | in-process TTL LRU | Query-result cache and per-user rate limiting. |
| Compute | **Cloud Run** (API + workers + jobs) | local process | Scales to zero, cheap, no cluster to run, and supports streaming. Agent Engine was considered: it is attractive for managed sessions, but Cloud Run keeps the HITL flow, networking and observability fully in our control. |
| Identity | **IAP / Identity Platform (OIDC)** with groups mapped to entitlements | `config/users.yaml` with `--user` | Identity has to come from the platform, never from the chat. |
| Prompt-injection scanning | **Model Armor** | regex pre-filter plus Flash-Lite classifier | A managed, multilingual scanner. The prototype uses the same contract with a cheaper implementation. |
| PII detection | **Cloud DLP (Sensitive Data Protection)** | regex redactor | DLP detects names and addresses in free text. Regex is enough as a last-line backstop. |
| Observability | **OpenTelemetry → Cloud Trace**, **Cloud Logging → BigQuery sink**, **Cloud Monitoring**, **Langfuse** | JSONL event log, `/trace`, `/stats`, Traces page; Langfuse when keys are set | See [observability.md](observability.md). |
| Persona registry | Cloud SQL `persona_versions` plus admin UI (or Langfuse prompt management) | SQLite plus Streamlit admin page | Versioned and immutable, with rollback and eval-gated publishing. |
| UI | Web chat, Slack bot, CLI | **CLI** (Typer + Rich + prompt_toolkit), **Streamlit** web | Thin clients over a single `AgentService`. |
| Secrets & IaC | Secret Manager, Terraform, Cloud Build | `.env` | — |

**Framework experience.** I learned LangGraph and Google ADK in labs, not in a shipped product. My production agent work is custom runtimes:
- **Verity:** a custom Python (FastAPI) runtime. The model plans a step and calls tools over MCP. Each step is persisted as state plus an RFC 6902 JSON Patch, so a run can be replayed or rolled back. Runs of about 100–200 steps and 50–100 tool calls stream over WebSockets, with provider fallback, context trimming, and a queue cap on concurrent runs.
- **aiChamp:** custom TypeScript LLM workflows with versioned prompts, structured JSON outputs, and a separate model config per task.

That experience shaped how LangGraph is used here. The framework supplies what it does well: the state graph, the checkpointer, and `interrupt()`/resume for human-in-the-loop. Everything a production runtime must control is explicit code around it, not framework defaults: retries and provider fallback (`LLMGateway`), per-turn budgets, guards, the error boundary with history repair, and tracing. Swapping LangGraph for a custom runtime like Verity's would therefore keep most of the code.

Alternatives considered:
- **Google ADK:** Gemini-native, with good tool-confirmation callbacks, but newer and with less control over the state machine.
- **CrewAI:** multi-agent role-play, which this problem does not need.
- **A hand-written loop on the Gemini SDK:** would mean rebuilding checkpointing, interrupts and tracing.

## 3. System overview and data flow

Request lifecycle (production; the prototype follows the same steps in-process):

1. **Client → Edge.** The CLI, web or Slack client calls `POST /chat`. Cloud Armor rate-limits and IAP authenticates. The API receives a verified identity (`user_id`, groups) and loads entitlements.
2. **API → Agent.** `AgentService.chat(user, session, text)` runs a new turn on the LangGraph thread `session_id`. Session ownership is checked, so a user cannot continue someone else's conversation. A new `trace_id` is created.
3. **guard_input.** The regex pre-filter runs first, then Model Armor (production), then the Flash-Lite classifier with the last 4 messages as context. Off-topic and malicious requests end the turn with a polite refusal in the user's language.
4. **load_context.** Loads the active persona version, the user's preferences and the top-3 golden trios above the similarity floor. Retrieval is skipped for smalltalk, report management and preference requests.
5. **agent ⇄ tools.** Gemini plans and calls tools:
   - `describe_dataset`;
   - `run_sql` (guard → dry-run → execute → sanitize → empty-result diagnosis);
   - `search_analyst_playbook`;
   - `save_report` / `list_reports`;
   - `request_report_deletion`;
   - `remember_preference`.

   The loop is bounded by per-turn budgets.
6. **Destructive branch.** A deletion request routes to `confirm` (`interrupt`) and then `execute_deletion`. Both are deterministic and run only after a human decision.
7. **guard_output.** PII redaction on the final text (DLP in production).
8. **Response.** Text, status (`ok | blocked | degraded | error | awaiting_confirmation`), `trace_id`, tokens and cost. A `turn_end` event closes the trace.

### Where data lives

| Data | Production store | Prototype | Contains PII? | Retention |
|---|---|---|---|---|
| Raw transactions | BigQuery `thelook_ecommerce` | same (public) | **yes** (users table) | source system |
| Agent-visible data | BigQuery authorized views (no PII columns, row policies) | scoped subqueries from `sql_guard` | no | — |
| Conversation state | Cloud SQL (LangGraph Postgres checkpointer) | `.data/checkpoints.db` | aggregates only | 90 days |
| Saved reports + audit log | Cloud SQL `reports`, `audit_log` | `.data/app.db` (+ `out/reports/*.md`) | no (redacted on save) | soft delete → purge after 30 days; audit 1 year |
| Preferences | Cloud SQL `user_preferences` | `.data/app.db` | no | until the user clears them |
| Persona versions | Cloud SQL `persona_versions` | `.data/app.db` | no | forever (immutable) |
| Golden trios | GCS bucket (versioned) + vector index | `golden/trios/*.yaml` + `.data/golden_index_*.json` | no (DLP-scrubbed on ingest) | versioned |
| Traces / events | Cloud Logging → BigQuery dataset; Langfuse | `.data/traces/YYYY-MM-DD.jsonl` | no (inputs truncated and redacted) | 30 days hot, 1 year cold |
| Query cache | Memorystore | in-process TTL LRU | no | 15 min |

---

## 4. Requirements

### Requirement 1: Hybrid Intelligence (Golden Knowledge)

**Goal.** Answers should follow how expert analysts interpret questions, not only what the raw data allows.

**What a trio contains** (`golden/trios/*.yaml`):
- `question`;
- `interpretation`: the business definitions and approach, e.g. *"churn = 90 days without a non-cancelled order, counted in the month the 90 days elapse"*;
- `sql`, written against the logical tables;
- `report`: **how** the analyst built the conclusion (method only, e.g. "compare the churned cohort's channel mix and return rate with last month's"). It contains no findings, so the model cannot copy a stale cause as a fact; the prompt also labels it as method;
- `eval_question`: a concrete phrasing used by the eval suite;
- `tags`.

**At query time** ([`load_context`](../src/retail_agent/agent/graph.py), [`GoldenStore`](../src/retail_agent/golden.py)):
1. The question is embedded with `RETRIEVAL_QUERY` and compared by cosine similarity with the trio questions embedded as `RETRIEVAL_DOCUMENT`.
2. Hits below a **similarity floor (0.60)** are dropped. An irrelevant example is worse than none, because it anchors the model on the wrong logic.
3. The **top-3** trios go into the system prompt under *"Analyst playbook"*, explicitly framed as *definitions and approach to adapt, SQL as a template*.
4. For multi-step analyses the agent can call `search_analyst_playbook` again for a sub-question.
5. If embeddings are unavailable, retrieval **degrades to BM25** keyword search, so a turn never fails because of retrieval. The trace records `golden_mode=semantic|keyword` and the hit ids with scores.

**Keeping the bucket fresh** (production, [diagram 6](architecture.md#6-golden-knowledge-ingestion-and-retrieval)):
- **Ingestion.** Analysts commit trios to a versioned GCS bucket, and Eventarc triggers a Cloud Run job. The job:
  1. validates the schema;
  2. **dry-runs the SQL** against current BigQuery, rejecting it if it is broken;
  3. scrubs PII from the report text with DLP;
  4. de-duplicates (cosine > 0.95 → merge or replace);
  5. embeds the trio and upserts it into the index with metadata (owner, version, tags, last_validated).
- **Growth from usage.** A 👍 answer, together with its executed SQL and final report, becomes a *candidate trio* in an analyst review queue. Only approved candidates enter the golden set, because humans curate the knowledge base and the agent never promotes itself.
- **Drift control.** A nightly job re-runs a dry-run for every trio. Trios that break, for example after a schema change, are quarantined and their owners notified.
- **Quality loop.** Each trio is also an eval case: its `eval_question` is asked with that trio hidden from retrieval (leave-one-out), and its SQL is the reference result (see [evaluation.md](evaluation.md)).
- **At scale:** hybrid retrieval (semantic + BM25 with reciprocal-rank fusion), a cross-encoder or Gemini reranker, and filtering by the user's domain (e.g. a womenswear user's trios first).

The prototype ships 11 trios covering revenue trends, top customers, state comparisons, churn, brand comparison, margins, returns, channels, quarterly reports, fulfilment and demographics. All of their SQL was validated live against BigQuery through the guard.

### Requirement 2: Safety & PII masking (prototype: fully implemented)

Defense in depth, with each layer independent:

| # | Layer | Where | What it stops |
|---|---|---|---|
| 1 | **Identity & entitlements** | IAP (production); [`entitlements.py`](../src/retail_agent/safety/entitlements.py) + `config/users.yaml` | "I'm actually the CEO." Identity never comes from the chat. Sessions are bound to their owner. |
| 2 | **Input guard** | [`input_guard.py`](../src/retail_agent/safety/input_guard.py) | Off-topic use and prompt injection or jailbreaks, in any language (regex pre-filter, then Flash-Lite classifier with conversation context; Model Armor in production). |
| 3 | **SQL guard** | [`sql_guard.py`](../src/retail_agent/safety/sql_guard.py) | DML/DDL, multiple statements, scripting, `EXPORT DATA`, `EXTERNAL_QUERY`, `INFORMATION_SCHEMA`, other datasets, CTE shadowing of source tables, direct references to PII columns (with an actionable hint: "use users.id"), unbounded results (`LIMIT` enforced). |
| 4 | **Scope rewrite (row/column security)** | `sql_guard.scoped_table_sql` | Each logical table is replaced by a **PII-free, entitlement-filtered subquery** with **parameterized** scope values (`@scope_brands` etc., never string-spliced). `SELECT *` from `users` only yields safe columns. Out-of-scope products are physically absent. |
| 5 | **Result sanitizer** | [`pii.py`](../src/retail_agent/safety/pii.py) | PII-looking columns are dropped and email, phone, street address and lat/long patterns are redacted *before* results reach the LLM. |
| 6 | **Report & output guard** | `save_report`, `guard_output` | Final redaction pass on saved reports and on every answer. |
| 6b | **Small-group protection** | [`privacy.py`](../src/retail_agent/safety/privacy.py), `sql_guard` | Demographic lookups for specific customer ids (`WHERE users.id = …` with age/gender/city) are rejected; demographics are stripped from customer-level rows; demographic groups smaller than 5 customers are suppressed when the result has a customer-count column. |
| 7 | **Audit** | trace events | Every block and redaction is logged with its layer and category, without logging the raw PII. |

Why it holds: layers 3 and 4 do not depend on the model at all. The tests show this in [`test_classifier_outage_fails_open_but_data_layer_still_enforces`](../tests/test_guardrails_and_persona.py): with the classifier down, a request for emails still never reaches BigQuery. Because of that, the input guard **fails open** when it is unavailable: availability is kept and safety is not lost.

**Behaviour choices.**
- "Top customers *with their emails*" is treated as an analysis request. The agent answers with customer ids and explains that personal data is unavailable, which serves the legitimate need.
- "Give me the email of user 46830" is blocked outright, because its primary purpose is PII.

**Production mapping.**
- The agent's service account sees only **authorized views**.
- **Row-level access policies** use `SESSION_USER()` or a per-request principal through a connection property.
- **Policy tags** on PII columns use the *nullify* or *hash* masking rules.
- **k-anonymity**: a minimum group size of 5, enforced in the database by **BigQuery aggregation threshold analysis rules** on the users view (`privacy_unit_column = user_id`, `threshold = 5`). The prototype approximates this in code (layer 6b); its group-size check relies on the result carrying a customer-count column, which is why the database rule is the production answer.

**Entitlement model.** Grants are additive (`brands ∪ categories ∪ departments`). Orders and users are scoped *through* in-scope order items: Alice sees orders and customers that bought Women's products. For scoped users, `orders.num_of_item` is recomputed from in-scope items only, so it never reveals out-of-scope purchases (Bob's average items per order is 1.02, versus 1.45 across all brands).

### Requirement 3: High-Stakes Oversight (prototype: fully implemented)

**Design goal.** The LLM can *request* but can never *execute* a destructive action. Execution needs an explicit, validated human decision, delivered without breaking the conversation.

1. **Resolve deterministically.** `request_report_deletion(text | this_conversation | report_ids | all_my_reports)` searches the report store with **`owner_id` injected from the authenticated identity**, never chosen by the model. With no criteria the tool refuses, so "delete everything" requires the explicit `all_my_reports` flag.
2. **Freeze.** The exact ids, the criteria, a one-time **token** and whether typed confirmation is required (for more than `bulk_delete_threshold` = 2 reports) are stored in graph state as `pending_action`.
3. **Interrupt.** The graph routes to the `confirm` node, which calls LangGraph `interrupt()`. The state is checkpointed, so the confirmation survives restarts and can be answered on another server instance.
4. **Show exactly what will happen.** The CLI shows a red table of the reports with `y/N`, or *type `delete 3`* for bulk deletions. The web app shows a confirmation card with Delete/Cancel, and Delete stays disabled until the phrase is typed.
5. **Validate on the server.** `execute_deletion` proceeds only if all of these hold:
   - `confirmed=True`;
   - the token matches;
   - the owner matches;
   - the typed phrase matches (when it is required).

   It then soft-deletes **frozen ids ∩ still owned ∩ not yet deleted**, which guards against races and replays, and writes the `audit_log`.
6. **Keep the UX intact.** Cancelling, or sending any new message, cancels the action with no deletion, and the conversation simply continues. After a deletion, **`/undo` within 30 minutes** restores the reports (soft delete). In production a scheduled job purges them after 30 days.

Tested in [`test_destructive_flow.py`](../tests/test_destructive_flow.py):
- owner isolation;
- the other-session report left untouched;
- cancel;
- a wrong typed phrase;
- a forged token;
- a new message cancelling the action;
- targeting another user's report by id;
- one-shot undo.

Also verified live in both the CLI and the web app. The same confirmation node is the extension point for any future outward-facing tool (e-mailing a report, posting to Slack).

### Requirement 4: Continuous improvement (design, plus a light prototype)

**User level.**
- **Explicit preferences** (prototype). When a user says "I prefer tables" or "keep it brief", the agent calls `remember_preference(key, value)`. Keys come from a whitelist: `format`, `depth`, `charts`, `language`, `focus`, `note`. The preferences are injected into every following prompt. Users manage them with `/prefs` in the CLI or see them in the web sidebar.
- **Implicit preferences** (production). After a session goes idle, a Pub/Sub-triggered job runs Flash-Lite over the transcript and feedback and proposes preferences with a confidence. Examples: *"the user asked twice for a table version"* → `format=tables (0.8)`; *"the user always asks follow-up why-questions"* → `depth=deep`. The job only writes preferences above a threshold, decays old inferred preferences, and never overrides explicit ones. Users can see and delete everything stored about them.

**System level** ([diagram 7](architecture.md#7-learning-loop)).
- **Signals:** 👍/👎 and comments, self-corrections, empty results, guardrail blocks, abandoned turns, and latency/cost outliers. All are already emitted as events.
- **Weekly loop:**
  1. Failures are clustered (e.g. "status values lowercase", "confusing quarter definitions").
  2. Each cluster yields new **eval cases** (the regression set grows), a **prompt or tool fix**, or a **golden trio** that encodes the right interpretation.
  3. Every change passes the **offline eval gate**: accuracy at or above the baseline and the red-team suite at 100%.
  4. Changes ship as a canary to a share of traffic, with online metrics compared before full rollout.
- **Explicitly not done:** autonomous self-modification of prompts or golden data without human review. With PII and executive decisions at stake, learning is *human-gated*.

### Requirement 5: Resilience & graceful error handling (prototype: fully implemented)

| Failure | Detection | Strategy | User sees | Event / metric |
|---|---|---|---|---|
| SQL syntax or semantic error | sqlglot parse, BigQuery **dry-run** (free) | Structured error plus hint returned to the agent, which fixes and retries. Positions are stripped and dataset names cleaned. | Nothing, if fixed | `sql_attempt{status=error}`, `self_corrected` |
| Forbidden SQL (PII, DML, foreign table) | `sql_guard` | Rejected with an actionable hint; BigQuery is never reached | An answer without PII | `guardrail_block{layer=sql_guard}` |
| Empty result | `row_count == 0` | **Automatic diagnosis**: string filters are extracted and the closest stored values probed (`'Brazil'` → `'Brasil'`); the agent retries once | The correct answer | `empty_result` |
| Retry storms / cost blow-up | per-turn counters | `max_sql_attempts=4`, `max_tool_calls=14`, `turn_token_budget=250k`; then a forced final answer without tools; `recursion_limit` as backstop | An honest partial answer | `sql_budget_exhausted`, `budget_exhausted` |
| Expensive query | dry-run byte estimate | Rejected above `max_bytes_billed` (1 GB) *before* running; BigQuery also enforces `maximum_bytes_billed` | The agent narrows the query | `sql_attempt{status=too_expensive}` |
| BigQuery 429 / 5xx / timeout | exception classification | Retry with exponential backoff and jitter (3 attempts), job timeout, **circuit breaker** | Transparent, or "data source temporarily unavailable" | `retry`, `dependency_down` |
| Gemini 429 / 5xx / timeout | same | Client **rate limiter** → retries → **fallback model** (2.5 Flash) → circuit breaker per model | Transparent, or a friendly "try again in a minute" | `retry`, `llm_failover`, `fallback_used`, `llm_unavailable` |
| Guard classifier down | exception | **Fail open** (data-layer enforcement still applies) | Normal service | `guard_degraded` |
| Embeddings down | exception | BM25 keyword retrieval | Normal service | `golden_mode=keyword` |
| Empty or malformed LLM response | no text and no tool calls | One nudge retry, then a polite fallback message | A request to rephrase | `empty_llm_response` |
| Unexpected exception anywhere | `AgentService._run` error boundary | Logged with a traceback; **conversation history repaired** (dangling tool calls closed) so the next turn works | "Something went wrong… (reference: `trace_id`)" | `error{error_type}` |
| UI-level errors | REPL / Streamlit try/except | The CLI never exits on errors; Ctrl-C cancels the current input | — | — |

**Cost control without losing quality.**
- Dry-runs are free, so syntax errors cost nothing in BigQuery.
- The LLM receives only a **preview** (40 rows) plus the row count.
- Identical queries hit the result cache.
- The guard runs on Flash-Lite.
- Retrieval is skipped for non-analytical turns.
- Budgets cap the worst case.

Measured: typical analytical turns use 10–25k tokens (about $0.005–0.015). A multi-query report uses about 70k tokens (about $0.04). Each query scans about 7–12 MB.

**Demonstrating it.** `RA_FAKE_FAIL=llm_primary,bigquery_once,embeddings` injects faults at the dependency boundaries. Verified live:
- the primary model fails → the trace shows retry ×2, `llm_failover`, `fallback_used`, and the answer completes on 2.5 Flash;
- a transient BigQuery failure is retried silently;
- `RA_FAKE_FAIL=bigquery` → a "degraded" answer telling the user the warehouse is unavailable.

### Requirement 6: Quality assurance

See [evaluation.md](evaluation.md). In summary:
- **Offline (pre-deployment):** an **accuracy suite** compares agent answers with *reference SQL result sets*, scored by a stronger LLM judge (Gemini 3.1 Pro). The **red-team suite** uses deterministic checks against *real* PII values, scope figures and destructive-action state, and is a 100% gate. Unit and flow tests use fakes.
- **Intent verification:** a judge rubric (answers intent, numbers match reference, **grounded in the query results the agent saw**, clarity, explains why), plus online sampling of production traces and 👍/👎.
- **Report checks (run in the eval):** `save_report` ran; every persona section is present; 3–5 action items, each with an owner and a numeric target; share of report numbers traceable to query results is recorded.
- **Golden trios as tests:** each trio is graded leave-one-out, which covers the brief's own examples (state underspending, churn spike, Q1 report, top customers).
- **UX:** task success, turns-to-answer, latency p95, confirmation friction (cancel rate), abandonment, and moderated sessions with executives.
- **Current results:** analysis 10/10, red team 14/14 (see [`evals/results/latest.md`](../evals/results/latest.md)).

### Requirement 7: Observability (prototype: fully implemented)

See [observability.md](observability.md). Every turn emits structured events tied together by `trace_id` / `session_id` / `user_id`:
- `turn_start` / `turn_end`;
- `guard_verdict`;
- `context_loaded`, including the persona version and golden hits;
- `llm_call`: model, tokens (incl. thinking), latency, finish reason, cost, last prompt message, reply and requested tool calls;
- `tool_call`;
- `sql_attempt`: the model's SQL, the scoped SQL BigQuery ran, status, rows, bytes, error, and the first result rows the model saw;
- `turn_messages`: the turn's full message exchange (user, model replies, tool calls and tool results), redacted;
- `retry`, `fallback_used`;
- `guardrail_block`, `pii_redaction`;
- `confirmation_*`, `deletion_executed`;
- `error`, with a traceback.

A trace alone therefore reconstructs a failing turn without Langfuse. They are viewable through `/trace`, `/transcript` and `/stats` in the CLI (and `retail-agent transcript <session>`), the web *Traces & metrics* page, and Langfuse when it is configured. In production: OpenTelemetry spans to Cloud Trace, JSON logs to a BigQuery sink for SQL deep dives, and Cloud Monitoring SLO alerts.

### Requirement 8: Agility (persona management)

- **The persona is data:** `Persona(name, tone, audience, detail_level, report_sections, formatting, avoid, extra_instructions)`. Fields are length-limited and stored as **immutable versions** with author, timestamp and change note.
- The active version is read **at the start of every turn**, so a change applies to the *next message* in every open CLI or web session, with no restart or deploy. Verified live: a version published in the web panel changed the CLI's answer style, and the trace showed `persona v2`.
- **Admin web page** (role `admin` only):
  - a form with tone presets;
  - a **diff vs the active version**;
  - **Preview**, which answers a sample question with the current *and* the draft persona side by side without publishing;
  - **Publish** with a change note;
  - **History** and one-click **Rollback**.

  The CLI has `/persona [history|rollback N]` as well.
- **Safety separation:** the persona can only change *style*. The non-negotiable rules are code-owned and always placed *above* the persona in the prompt, explicitly marked as not overridable (tested in `test_persona_change_applies_on_next_message_without_restart`).
- **Production:** the same model in Cloud SQL behind IAP. Publishing can require the eval gate (run the red-team and a judged sample against the draft) and a second approver. The agent caches the active version with a ~30 s TTL.

---

## 5. Error-handling principles

1. **Classify, then act.** Transient errors (429, 5xx, timeouts, network) are retried with backoff and jitter. Client errors (400, validation) are *not* retried and are fed back to the model as structured, actionable messages.
2. **Bound everything.** Retries, SQL attempts, tool calls, tokens, graph steps, query bytes, result rows, input length (4,000 characters) and LLM timeouts all have limits.
3. **Degrade along explicit paths:**
   - primary → fallback model;
   - semantic → keyword retrieval;
   - classifier → data-layer enforcement;
   - live → cached results.
4. **Contain failures at the turn boundary.** One request can never corrupt a conversation: the history is repaired after crashes, and interrupts are checkpointed.
5. **Every failure is debuggable.** The user sees a reference id, which is the `trace_id`, and engineers can read the full timeline for it.

## 6. Security and threat model

| Threat | Mitigation |
|---|---|
| Prompt injection (direct, multilingual, role-play) | Input guard; rules above the persona; tool output marked as data; most importantly, enforcement outside the LLM (SQL guard + scoped sources) |
| Indirect injection through data or trios | Tool results and examples are framed as data; trios are curated, scrubbed and reviewed |
| PII exfiltration | PII columns physically absent; result sanitizer; output redaction; red-team suite with real PII values |
| Privilege escalation ("I'm the CEO") | Identity from IAP only; scope bound as query parameters from the identity |
| Cross-user access | Sessions bound to owner; reports and traces owner-checked; admin-only pages |
| Destructive actions | HITL interrupt, one-time token, typed confirmation for bulk deletions, soft delete + undo, audit log |
| SQL injection / DML | AST validation, single read-only statement, read-only service account, no scripting |
| Cost abuse / DoS | Per-user rate limits (Cloud Armor, Redis), per-turn budgets, bytes cap, Flash-Lite guard |
| Secrets leakage | Secret Manager, nothing in prompts, logs redacted |

## 7. Performance and cost

- **Latency:** guard ~0.5–1 s; simple questions 8–15 s; multi-step "why" analyses 20–30 s; full reports 40–60 s.
  - Production adds **streaming** (SSE) of progress events, already modelled as `turn.progress(...)` and shown as live spinner text in the CLI and web.
  - Planned: parallel tool calls for independent sub-queries.
- **Cost per turn:** ~$0.005–0.015 (Flash), ~$0.04 for full reports. The guard costs < $0.0001.
- **BigQuery:** queries scan ~7–12 MB on the 25 MB dataset. Production adds partitioned and clustered authorized views (by `created_at`, `product_id`) and BI Engine for hot aggregates.

## 8. Deployment and operations (production)

- **Environments:** dev, staging and prod projects. Infrastructure is defined in Terraform. Cloud Build runs lint, unit tests, the red-team gate and the accuracy suite (against staging), then deploys to Cloud Run with **traffic splitting** for canaries.
- **Configuration:**
  - `settings.yaml` holds limits and models;
  - `users.yaml` is replaced by IAM groups;
  - personas, preferences and golden trios are runtime data.
- **Runbooks:** what to do when an alert fires:
  - `llm_unavailable` rate;
  - `sql_attempt` error rate;
  - p95 latency;
  - guardrail block spikes (possible attack);
  - PII redactions in final answers > 0 (always investigate).

## 9. Extensibility: adding a capability

Every capability is a `ToolSpec` (pydantic args + function) registered in [`tools/`](../src/retail_agent/tools/base.py). The graph, tracing, budgets and error handling apply automatically.

```python
class ChartArgs(BaseModel):
    sql: str
    kind: Literal["line", "bar"]
    title: str

def make_chart(ctx: ToolContext, args: ChartArgs) -> dict:
    df = run_guarded(ctx, args.sql)                  # same sql_guard + scope
    path = render_vega_lite(df, args.kind, args.title)  # → GCS signed URL in prod
    return {"status": "ok", "chart_url": path}

CHART_TOOLS = [ToolSpec("make_chart", "Render a chart from a query", ChartArgs, make_chart)]
```

- **Charts:** Vega-Lite specs rendered in the web UI or to PNG on GCS. The `charts=prefer` preference already exists.
- **E-mailing a report:** an outward-facing action, so `request_send_report` sets a `pending_action` and reuses the **same confirmation node**, showing recipients and content.
- **Web trends:** a search tool (e.g. Vertex AI grounding with Google Search). Its results are marked as untrusted external data and cited separately from internal numbers.
- **MCP:** tools can be exposed or consumed over MCP to share them with other agents.

## 10. Known limitations and next steps

- **Small-group protection is heuristic in the prototype:** suppression of small demographic groups needs a customer-count column in the result. A query that groups by city and returns only revenue is not checked. Production enforces k = 5 in BigQuery (aggregation threshold rules). Order-level fields such as `orders.status` describe the whole order, not only in-scope items.
- **Regex PII detection** misses free-text names. DLP is needed in production; the guard architecture makes this a drop-in change.
- **No response streaming** of tokens yet; progress events are streamed instead.
- **Preferences are explicit only**; the implicit extractor is designed but not built.
- **Single-process SQLite** is fine for the prototype. Production uses Cloud SQL (Postgres checkpointer) for multi-instance HITL.
- **OpenTelemetry export** is not wired in the prototype; the JSONL event schema maps 1:1 to spans.
- **Evaluation set size:** 10 accuracy and 14 red-team cases are a seed. The golden trios and real usage should grow it to hundreds before production.
