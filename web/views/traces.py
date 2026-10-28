"""Traces & metrics (admins): why is the agent failing, and what exactly happened in a turn."""

import pandas as pd
import streamlit as st
from common import current_user, service

from retail_agent.observability.render import offset_ms, summarize

svc = service()
me = current_user()
if not me.is_admin:
    st.error("Admins only.")
    st.stop()

st.title("🔎 Traces & metrics")
days = st.slider("Window (days)", 1, 30, 7)
m = svc.metrics(me.user_id, days)

first = m["sql"]["first_try_success_rate"]
tiles = [
    ("Turns", m["turns"]),
    ("Success rate", "—" if m["success_rate"] is None else f"{m['success_rate']:.0%}"),
    ("Latency p50 / p95", f"{m['latency_ms_p50'] / 1000:.0f}s / {m['latency_ms_p95'] / 1000:.0f}s"),
    ("Est. cost", f"${m['est_cost_usd_total']:.2f}"),
    ("Avg tokens / turn", f"{m['avg_tokens_per_turn'] / 1000:.1f}k"),
    ("SQL first-try OK", "—" if first is None else f"{first:.0%}"),
    ("SQL self-corrected", m["sql"]["self_corrected"]),
    ("Guardrail blocks", sum(m["guardrail_blocks"].values())),
    ("PII redactions", m["pii_redactions"]),
    ("LLM fallbacks", m["llm_fallbacks"]),
    ("Retries", m["retries"]),
    ("Errors", sum(m["errors_by_type"].values())),
]
for row in range(0, len(tiles), 4):
    for col, (label, value) in zip(st.columns(4), tiles[row:row + 4], strict=False):
        col.metric(label, value)
with st.expander("All metrics (JSON)"):
    st.json(m)

turns = svc.services.tracer.turns(days=days)
if not turns:
    st.info("No turns yet.")
    st.stop()
df = pd.DataFrame(turns)[["ts", "trace_id", "user_id", "session_id", "status", "latency_ms", "tokens",
                          "cost_usd", "sql_queries", "flags", "answer_preview"]].iloc[::-1]
f1, f2 = st.columns(2)
users = f1.multiselect("User", sorted(df.user_id.unique()))
statuses = f2.multiselect("Status", sorted(df.status.unique()))
if users:
    df = df[df.user_id.isin(users)]
if statuses:
    df = df[df.status.isin(statuses)]
st.dataframe(df, hide_index=True, width="stretch", height=280)

trace_id = st.selectbox("Inspect a turn", df.trace_id.tolist())
events = svc.trace(me.user_id, trace_id) if trace_id else []
if events:
    st.subheader(f"Timeline of `{trace_id}`")
    st.dataframe(pd.DataFrame([{"+ms": offset_ms(events, e), "event": e["event"], "details": summarize(e)}
                               for e in events]), hide_index=True, width="stretch")
    with st.expander("Raw events (JSON)"):
        st.json(events)
