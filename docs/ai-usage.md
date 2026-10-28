# How AI was used to build this

The assignment was done with an AI coding agent, **Claude Code**, as the brief expects. The split of work:

**Human decisions.** Scope and priorities, plus:
- the framework (LangGraph);
- local JSONL tracing with optional Langfuse;
- adding a persona admin panel and a web chat on top of the required CLI;
- the GCP project to use;
- final review of design trade-offs and wording.

**AI-assisted work:**
1. **Analysis & planning.** Read the brief and the provided `bq_client.py`, inspected the live dataset (schemas, sizes, value spellings such as `Brasil`), checked the enabled GCP APIs and available Gemini models, and produced a plan that was reviewed and iterated with the human before any code was written.
2. **Implementation in small, committed steps** (see `git log`): safety core first (SQL guard + PII, test-driven), then the graph, tools, CLI, stores, resilience, observability, web app, evals and docs.
3. **Verification against the real system**, not just unit tests:
   - drove the interactive CLI through a pseudo-terminal;
   - drove the web app in a browser;
   - ran fault injection (`RA_FAKE_FAIL`) to exercise retries and fallbacks;
   - ran the live red-team and accuracy suites.

   Several bugs were found and fixed this way, for example:
   - Rich swallowing the `[y/N]` prompt as markup;
   - an injected BigQuery fault escaping the retry wrapper in `dry_run`;
   - a budget-exhausted loop;
   - a misleading success-rate metric;
   - a form/preset UX issue in the persona editor.
4. **Documentation**, including the Mermaid diagrams, which were render-checked with Mermaid 11.

**Guardrails for the AI-assisted process itself:**
- every change went through tests (`make test`) and lint;
- every safety claim in the docs is backed by a test or a live eval run;
- generated SQL in the golden trios was executed against BigQuery before being committed.

`CLAUDE.md` holds the project context used during development.
