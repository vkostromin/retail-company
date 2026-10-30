# Architecture

This document holds all diagrams. The reasoning behind each choice is in
[technical-design.md](technical-design.md).

1. [Production high-level design](#1-production-high-level-design-gcp)
2. [Prototype components](#2-prototype-components-this-repository)
3. [Agent state graph](#3-agent-state-graph-langgraph)
4. [Analytical question with self-correction](#4-sequence-analytical-question-with-self-correction)
5. [Destructive action with human confirmation](#5-sequence-delete-reports-with-human-confirmation)
6. [Golden Knowledge pipeline](#6-golden-knowledge-ingestion-and-retrieval)
7. [Learning loop](#7-learning-loop)

---

## 1. Production high-level design (GCP)

```mermaid
flowchart LR
    subgraph Clients
        CLI["CLI"]
        WEB["Web chat"]
        SLK["Slack / Teams bot"]
        ADM["Admin console<br/>(persona, golden review)"]
    end

    subgraph Edge["Edge & identity"]
        LB["Cloud Load Balancer<br/>+ Cloud Armor (WAF, rate limits)"]
        IAP["IAP / Identity Platform<br/>(OIDC, groups → entitlements)"]
    end

    subgraph Serving["Serving (Cloud Run, autoscaled)"]
        API["Chat API (FastAPI)<br/>SSE streaming · sessions · authz"]
        AGT["Agent workers<br/>LangGraph state machine"]
        ADMAPI["Admin API<br/>persona registry · review queue"]
    end

    subgraph AI["Vertex AI"]
        ARM["Model Armor<br/>(injection / jailbreak / PII scan)"]
        GEM["Gemini 3.8 Flash (agent) · 2.5 Flash (fallback)<br/>Flash-Lite (guard) · Gemini 3.1 Pro (judge)"]
        EMB["Gemini embeddings"]
        VS["Vector Search<br/>(golden trios index)"]
    end

    subgraph State["Operational state"]
        SQL[("Cloud SQL Postgres<br/>checkpoints · reports · audit<br/>preferences · persona versions")]
        RED[("Memorystore Redis<br/>result cache · rate limits")]
    end

    subgraph Data["Analytics data (read-only)"]
        BQV["BigQuery authorized views<br/>row-level policies per principal<br/>policy tags + dynamic masking (PII)"]
        BQR[("thelook_ecommerce<br/>raw transaction logs")]
    end

    subgraph Golden["Golden Knowledge"]
        GCS[("GCS bucket<br/>Question → SQL → Report trios")]
        ING["Ingestion job (Cloud Run Job)<br/>validate SQL · DLP scrub · dedupe · embed"]
    end

    subgraph Obs["Observability"]
        OTEL["OpenTelemetry"]
        TRC["Cloud Trace"]
        LOG["Cloud Logging → BigQuery sink"]
        MON["Cloud Monitoring<br/>SLOs & alerts"]
        LF["Langfuse (LLM traces,<br/>prompt mgmt, evals)"]
    end

    subgraph Async["Async & learning"]
        PS["Pub/Sub: turn & feedback events"]
        PREF["Preference extractor job"]
        REV["Analyst review queue<br/>(candidate trios)"]
        SCH["Cloud Scheduler<br/>purge soft-deleted · re-validate golden"]
    end

    CLI & WEB & SLK --> LB --> IAP --> API
    ADM --> LB
    IAP --> ADMAPI
    API <--> AGT
    AGT --> ARM --> GEM
    AGT --> EMB --> VS
    AGT -- "guarded, scoped SQL<br/>(dry-run + bytes cap)" --> BQV --> BQR
    AGT <--> SQL
    AGT <--> RED
    ADMAPI <--> SQL
    GCS -- "Eventarc: object finalized" --> ING --> EMB
    ING --> VS
    AGT -- "spans / events" --> OTEL --> TRC
    AGT --> LOG --> MON
    AGT --> LF
    API --> PS --> PREF --> SQL
    PS --> REV --> GCS
    SCH --> SQL
    SCH --> ING
```

Notes:
- **Two hard trust boundaries** sit outside the LLM:
  - IAP/OIDC establishes *who* the user is.
  - BigQuery authorized views and row-level policies establish *what* data that identity can reach.

  The LLM can be fooled, but these two boundaries do not depend on it.
- **Agent workers are stateless.** All conversation state lives in Postgres (LangGraph checkpointer), so Cloud Run can scale to zero and a human-in-the-loop interrupt can be resumed on any instance.
- **Secrets** (Langfuse keys, Slack tokens) live in Secret Manager. The service accounts are:
  - *agent*: BigQuery Job User plus Data Viewer on the authorized views only;
  - *ingestion*: Storage Object Viewer, DLP User.

## 2. Prototype components (this repository)

```mermaid
flowchart TB
    subgraph UI["Thin clients"]
        CLI["CLI (Typer + Rich + prompt_toolkit)"]
        WEB["Streamlit app<br/>Chat · Persona (admin) · Traces (admin)"]
    end
    SVC["AgentService<br/>error boundary · sessions · confirmation resume"]
    subgraph Graph["LangGraph"]
        G1["guard_input"] --> G2["load_context"] --> G3["agent (Gemini)"]
        G3 <--> G4["tools"]
        G4 --> G5["confirm (interrupt)"] --> G6["execute_deletion"]
        G3 --> G7["guard_output"]
    end
    subgraph Safety
        IG["input_guard<br/>regex + Flash-Lite classifier"]
        SG["sql_guard<br/>sqlglot validate + scope rewrite"]
        PII["pii redaction<br/>results · reports · answer"]
    end
    subgraph Res["Resilience"]
        LLMGW["LLMGateway<br/>retry · breaker · fallback model · rate limit"]
        BQ["BigQueryRunner (provided, extended)<br/>dry-run · bytes cap · retry · breaker"]
    end
    subgraph Stores["SQLite (WAL)"]
        CK[("checkpoints.db<br/>LangGraph state")]
        APP[("app.db: reports · audit_log<br/>user_preferences · persona_versions · feedback")]
    end
    GOLD["GoldenStore<br/>YAML trios · Gemini embeddings · BM25 fallback"]
    TR[("JSONL traces<br/>.data/traces/YYYY-MM-DD.jsonl")]
    LFU["Langfuse (optional)"]

    CLI & WEB --> SVC --> Graph
    G1 --> IG
    G4 --> SG --> BQ --> BQD[("BigQuery<br/>thelook_ecommerce")]
    G4 --> PII
    G7 --> PII
    G2 --> GOLD
    G3 --> LLMGW --> GEM["Gemini (AI Studio or Vertex AI)"]
    Graph --> CK
    G4 & G6 & G2 --> APP
    SVC & Graph --> TR
    SVC -.-> LFU
```

## 3. Agent state graph (LangGraph)

```mermaid
stateDiagram-v2
    [*] --> guard_input
    guard_input --> [*]: off_topic / malicious<br/>(polite refusal)
    guard_input --> load_context: allowed
    load_context --> agent: persona + preferences<br/>+ top-k golden trios
    agent --> tools: tool calls
    tools --> agent: results / structured errors
    tools --> confirm: pending destructive action
    confirm --> execute_deletion: human decision<br/>(Command(resume=...))
    execute_deletion --> [*]
    agent --> guard_output: final answer
    guard_output --> [*]: PII-redacted answer
```

Budgets that bound every turn:
- `max_sql_queries` (8 queries) and `max_sql_errors` (3 failed or empty queries), counted separately;
- `max_tool_calls` (14);
- `turn_token_budget` (250k tokens);
- LangGraph `recursion_limit`.

When the budget is exhausted, the agent is forced to answer with what it has, without tools.

## 4. Sequence: analytical question with self-correction

```mermaid
sequenceDiagram
    actor U as Executive
    participant S as AgentService
    participant G as Guard (Flash-Lite)
    participant A as Agent (Gemini Flash)
    participant Q as sql_guard
    participant B as BigQuery
    participant T as Tracer

    U->>S: "How many customers do we have in Brazil?"
    S->>T: turn_start (trace_id)
    S->>G: classify (multilingual, context-aware)
    G-->>S: analysis
    S->>A: system prompt (rules + persona + prefs + golden examples) + history
    A->>Q: run_sql("... WHERE country = 'Brazil'")
    Q->>Q: parse · read-only · allowed tables · no PII columns · rewrite to scoped subqueries · LIMIT
    Q->>B: dry-run (free: syntax + bytes estimate)
    B-->>Q: OK, 7 MB
    Q->>B: execute (maximum_bytes_billed, labels, timeout)
    B-->>Q: 0 rows
    Q->>B: probe: closest values for users.country LIKE '%bra%'
    B-->>Q: ["Brasil", ...]
    Q-->>A: {row_count: 0, valid_values: [{"users.country = 'Brazil'": ["Brasil", ...]}]}
    A->>Q: run_sql("... WHERE country = 'Brasil'")
    Q->>B: dry-run + execute
    B-->>Q: 14,484
    Q-->>A: rows (PII-sanitized preview)
    A-->>S: answer
    S->>S: guard_output (PII regex redaction)
    S->>T: turn_end (status, latency, tokens, cost)
    S-->>U: "We have 14,484 customers in Brasil ..."
```

## 5. Sequence: delete reports with human confirmation

```mermaid
sequenceDiagram
    actor U as User (alice)
    participant UI as CLI / Web
    participant S as AgentService
    participant A as Agent
    participant R as ReportStore
    participant C as confirm node

    U->>UI: "Delete all the reports we made in this conversation"
    UI->>S: chat()
    S->>A: run graph
    A->>R: request_report_deletion(this_conversation=true)
    Note over R: search(owner_id = alice ← from identity, never from the model)
    R-->>A: 2 matches → pending_action {ids:[7,8], token, typed_required:false}
    A->>C: route (deterministic)
    C-->>S: interrupt({reports, count, token})
    S-->>UI: status = awaiting_confirmation
    UI->>U: table of exactly these reports + "Delete? [y/N]" (bulk: type "delete N")
    U->>UI: y
    UI->>S: resume({confirmed: true, token})
    S->>C: Command(resume=decision)
    C->>R: soft_delete(alice, frozen ids ∩ still-owned ∩ not-deleted) + audit_log
    R-->>S: deleted [7,8]
    S-->>UI: "Deleted 2 reports ... /undo within 30 minutes"
    Note over U,UI: Cancel or any new message → nothing deleted, conversation continues
```

## 6. Golden Knowledge ingestion and retrieval

```mermaid
flowchart LR
    subgraph Authoring
        AN["Analysts"] --> GCS[("GCS: golden/trios/*.yaml<br/>versioned bucket")]
        CAND["Candidate trios<br/>(from 👍 answers)"] --> RQ["Review queue"] --> AN
    end
    GCS -- "Eventarc: finalize" --> V1["Validate schema + dry-run SQL<br/>against current BigQuery"]
    V1 --> V2["DLP: scrub PII in report text"]
    V2 --> V3["Dedupe (embedding similarity > 0.95)"]
    V3 --> V4["Embed question + interpretation<br/>(Gemini embeddings)"]
    V4 --> IDX[("Vector Search index<br/>+ metadata: tags, owner, version")]
    NIGHT["Nightly: re-run dry-run for all trios"] --> Q["Quarantine stale trios<br/>(schema drift)"]
    IDX --> RET

    subgraph QT["Query time"]
        QQ["User question"] --> RET["Hybrid retrieval<br/>semantic + keyword (BM25)"]
        RET --> RR["Similarity floor + top-k = 3<br/>(optional rerank)"]
        RR --> PROMPT["Injected as 'Analyst playbook'<br/>(definitions, approach, SQL template)"]
    end
```

## 7. Learning loop

```mermaid
flowchart TB
    T["Every turn → events<br/>(trace, SQL attempts, verdicts, feedback)"] --> L[("Log sink → BigQuery")]
    subgraph UL["User level"]
        EXP["Explicit: 'I prefer tables'<br/>→ remember_preference"] --> P[("user_preferences")]
        IMP["Implicit (async job): LLM reads the session,<br/>proposes prefs with confidence"] --> P
        P --> CTX["Injected into the next prompt"]
    end
    subgraph SL["System level"]
        L --> CL["Failure clustering<br/>(SQL errors, empty results, 👎, blocks)"]
        CL --> EV["New eval cases<br/>(regression set grows)"]
        CL --> GT["Candidate golden trios<br/>(👍 + verified SQL)"]
        CL --> PR["Prompt / persona / tool fixes"]
        EV & PR --> GATE{"Offline eval gate<br/>accuracy ≥ baseline, red-team 100%"}
        GATE -- pass --> CAN["Canary 10% → full rollout"]
        GATE -- fail --> PR
        GT --> REV["Analyst review"] --> GOLD[("Golden bucket")]
    end
    T --> IMP
```
