"""A trace alone must reconstruct a turn: messages, tool calls, executed SQL and the rows the model saw."""

import pandas as pd
import pytest
from langchain_core.messages import AIMessage

from retail_agent.service import AccessDenied
from tests.fakes import FakeBQ, FakeLLM, make_service, tool_call


def test_trace_contains_transcript_executed_sql_and_result_preview(tmp_path):
    llm = FakeLLM([tool_call("run_sql", sql="SELECT status, COUNT(*) n FROM orders GROUP BY 1", purpose="by status"),
                   AIMessage("Most orders are Shipped.")])
    bq = FakeBQ(lambda sql: pd.DataFrame({"status": ["Shipped", "Complete"], "n": [54, 45]}))
    svc = make_service(tmp_path, llm, bq)
    r = svc.chat("alice", svc.new_session(), "orders by status")
    events = svc.services.tracer.read_trace(r.trace_id)

    attempt = next(e for e in events if e["event"] == "sql_attempt")
    assert "@scope_departments" in attempt["executed_sql"]  # the scoped query BigQuery ran
    assert "Shipped" in attempt["result_preview"]

    convo = next(e for e in events if e["event"] == "turn_messages")["messages"]
    assert [m["role"] for m in convo] == ["user", "assistant", "tool", "assistant"]
    assert convo[1]["tool_calls"][0]["name"] == "run_sql"
    assert convo[-1]["text"] == "Most orders are Shipped."


def test_transcript_is_owner_or_admin_only(tmp_path):
    svc = make_service(tmp_path, FakeLLM([AIMessage("hi")]))
    sid = svc.new_session()
    svc.chat("alice", sid, "hello")
    assert svc.transcript("alice", sid)[0]["text"] == "hello"
    assert svc.transcript("ceo", sid)  # admin
    with pytest.raises(AccessDenied):
        svc.transcript("bob", sid)


def test_metrics_report_cost_calls_bytes_and_layers(tmp_path):
    llm = FakeLLM([tool_call("run_sql", sql="SELECT COUNT(*) n FROM orders", purpose="n"), AIMessage("ok")])
    svc = make_service(tmp_path, llm)
    svc.chat("ceo", svc.new_session(), "count")
    m = svc.metrics("ceo")
    for key in ("est_cost_usd_per_turn", "avg_llm_calls_per_turn", "pii_redactions_by_layer", "privacy_suppressions"):
        assert key in m
    assert m["sql"]["bytes_scanned_total"] == 1000
