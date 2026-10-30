"""Self-correction, budgets, outages, fallbacks and circuit breaking."""

import json

import pandas as pd
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_google_genai.chat_models import GoogleInvalidRequestError, GoogleRateLimitError

from retail_agent.agent.llm import LLMGateway
from retail_agent.config import load_settings
from retail_agent.resilience.breaker import CircuitBreaker
from retail_agent.resilience.errors import CircuitOpenError, DependencyUnavailable, InjectedFault
from retail_agent.resilience.faults import FaultInjector
from retail_agent.resilience.retry import is_transient
from tests.fakes import FakeBQ, FakeLLM, make_service, tool_call


def events(svc, trace_id, kind):
    return [e for e in svc.services.tracer.read_trace(trace_id) if e["event"] == kind]


def tool_results(llm_call_messages):
    return [json.loads(m.content) for m in llm_call_messages if isinstance(m, ToolMessage)]


def test_sql_error_is_fed_back_and_self_corrected(tmp_path):
    llm = FakeLLM([
        tool_call("run_sql", sql="SELECT bad_col FROM orders", purpose="try 1"),
        tool_call("run_sql", sql="SELECT status, COUNT(*) n FROM orders GROUP BY 1", purpose="try 2"),
        AIMessage("Most orders are Shipped."),
    ])
    svc = make_service(tmp_path, llm)
    r = svc.chat("ceo", svc.new_session(), "orders by status")
    assert r.status == "ok" and r.text == "Most orders are Shipped."
    attempts = events(svc, r.trace_id, "sql_attempt")
    assert [a["status"] for a in attempts] == ["error", "ok"]
    first_feedback = tool_results(llm.calls[1])[0]
    assert first_feedback["status"] == "error" and "bad_col" in first_feedback["error"]
    assert "[1:8]" not in first_feedback["error"]  # positions stripped


def test_guard_rejection_is_explained_to_the_model(tmp_path):
    llm = FakeLLM([tool_call("run_sql", sql="SELECT email FROM users", purpose="emails"), AIMessage("I can't share emails.")])
    svc = make_service(tmp_path, llm)
    r = svc.chat("ceo", svc.new_session(), "top customers")
    feedback = tool_results(llm.calls[1])[0]
    assert feedback["code"] == "pii_column" and "users.id" in feedback["hint"]
    assert events(svc, r.trace_id, "guardrail_block")[0]["layer"] == "sql_guard"
    assert svc.services.bq.executed == []  # never reached BigQuery


def test_attempt_budget_stops_retry_loops(tmp_path):
    bad = [tool_call("run_sql", sql="SELECT bad_col FROM orders", purpose=f"try {i}") for i in range(8)]
    llm = FakeLLM([*bad, AIMessage("I couldn't compute that.")])
    svc = make_service(tmp_path, llm)
    r = svc.chat("ceo", svc.new_session(), "something hard")
    statuses = [json.loads(m.content)["status"] for m in llm.calls[-1] if isinstance(m, ToolMessage)]
    assert statuses.count("error") == svc.settings.max_sql_attempts
    assert "budget_exhausted" in statuses
    assert r.status == "ok"


def test_tool_call_budget_forces_final_answer(tmp_path):
    calls = [tool_call("describe_dataset") for _ in range(30)]
    llm = FakeLLM([*calls])
    svc = make_service(tmp_path, llm)
    r = svc.chat("ceo", svc.new_session(), "describe everything forever")
    assert llm.tools_seen[-1] is None  # final call made without tools
    assert r.status == "ok"
    assert events(svc, r.trace_id, "budget_exhausted")


def test_empty_result_returns_valid_filter_values(tmp_path):
    def responder(sql):
        if "@probe" in sql:
            return pd.DataFrame({"value": ["Brasil", "China"], "n": [10, 5]})
        return pd.DataFrame({"n": []})

    llm = FakeLLM([tool_call("run_sql", sql="SELECT COUNT(*) n FROM users WHERE country = 'Brazil'", purpose="br"),
                   AIMessage("ok")])
    svc = make_service(tmp_path, llm, FakeBQ(responder))
    svc.chat("ceo", svc.new_session(), "customers in Brazil")
    result = tool_results(llm.calls[1])[0]
    assert result["row_count"] == 0
    assert result["valid_values"][0]["closest_valid_values"][0] == "Brasil"


def test_bigquery_outage_degrades_gracefully(tmp_path):
    llm = FakeLLM([tool_call("run_sql", sql="SELECT COUNT(*) FROM orders", purpose="n"),
                   AIMessage("The data source is temporarily unavailable.")])
    svc = make_service(tmp_path, llm, FakeBQ(down=True))
    r = svc.chat("ceo", svc.new_session(), "how many orders")
    assert r.status == "degraded"
    assert tool_results(llm.calls[1])[0]["status"] == "unavailable"


def test_llm_outage_returns_friendly_message(tmp_path):
    llm = FakeLLM([DependencyUnavailable("llm", "all models down")])
    svc = make_service(tmp_path, llm)
    r = svc.chat("ceo", svc.new_session(), "revenue?")
    assert r.status == "degraded" and "try again" in r.text


def test_unexpected_crash_is_contained_and_history_repaired(tmp_path):
    llm = FakeLLM([RuntimeError("boom"), AIMessage("Recovered.")])
    svc = make_service(tmp_path, llm)
    sid = svc.new_session()
    r = svc.chat("ceo", sid, "first")
    assert r.status == "error" and r.trace_id in r.text
    assert svc.chat("ceo", sid, "second").text == "Recovered."


def test_tool_exception_does_not_crash_turn(tmp_path, monkeypatch):
    llm = FakeLLM([tool_call("list_reports"), AIMessage("fine")])
    svc = make_service(tmp_path, llm)
    monkeypatch.setattr(svc.services.reports, "search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    r = svc.chat("ceo", svc.new_session(), "list my reports")
    assert r.status == "ok" and tool_results(llm.calls[1])[0]["status"] == "error"


# ---------------------------------------------------------------- gateway: fallback & breaker
class NamedFake(GenericFakeChatModel):
    model: str = "fake"


def gateway(faults: set[str]) -> LLMGateway:
    settings = load_settings()
    settings.use_vertex, settings.google_api_key = False, "offline"
    gw = LLMGateway(settings, FaultInjector(faults))
    gw.primary = NamedFake(model="primary", messages=iter([AIMessage("from primary")] * 10))
    gw.fallback = NamedFake(model="fallback", messages=iter([AIMessage("from fallback")] * 10))
    return gw


def test_fallback_model_used_when_primary_fails(monkeypatch):
    monkeypatch.setattr("retail_agent.resilience.retry.wait_exponential_jitter", lambda **k: lambda rs: 0)
    out = gateway({"llm_primary"}).chat([HumanMessage("hi")])
    assert out.text == "from fallback"


class RateLimitedFake(NamedFake):
    """Raises the real langchain-google-genai 429 error `failures` times, then answers."""
    failures: int = 0

    def _generate(self, *args, **kwargs):
        if self.failures:
            self.failures -= 1
            raise GoogleRateLimitError("429 RESOURCE_EXHAUSTED")
        return super()._generate(*args, **kwargs)


def test_wrapped_model_errors_are_transient():
    assert is_transient(GoogleRateLimitError("429 RESOURCE_EXHAUSTED"))
    assert not is_transient(GoogleInvalidRequestError("400 bad request"))


def test_rate_limit_retries_primary_before_failover(monkeypatch):
    monkeypatch.setattr("retail_agent.resilience.retry.wait_exponential_jitter", lambda **k: lambda rs: 0)
    gw = gateway(set())
    gw.primary = RateLimitedFake(model="primary", failures=2, messages=iter([AIMessage("from primary")]))
    out = gw.chat([HumanMessage("hi")])
    assert out.text == "from primary"  # 2 x 429, then the 3rd attempt succeeds - no switch to fallback


def test_persistent_rate_limit_falls_back(monkeypatch):
    monkeypatch.setattr("retail_agent.resilience.retry.wait_exponential_jitter", lambda **k: lambda rs: 0)
    gw = gateway(set())
    gw.primary = RateLimitedFake(model="primary", failures=99, messages=iter([]))
    assert gw.chat([HumanMessage("hi")]).text == "from fallback"
    assert gw.primary.failures == 99 - 3  # all retries spent on the primary first


def test_all_models_down_raises_dependency_unavailable(monkeypatch):
    monkeypatch.setattr("retail_agent.resilience.retry.wait_exponential_jitter", lambda **k: lambda rs: 0)
    with pytest.raises(DependencyUnavailable):
        gateway({"llm_all"}).chat([HumanMessage("hi")])


def test_circuit_breaker_opens_and_fails_fast():
    cb = CircuitBreaker("x", failure_threshold=2, reset_timeout_s=60)
    for _ in range(2):
        with pytest.raises(InjectedFault):
            cb.call(lambda: (_ for _ in ()).throw(InjectedFault("down")))
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: "never called")


# ---------------------------------------------------------------- tracing: thinking tokens
def test_thinking_tokens_are_traced_and_billed_as_output(tmp_path):
    from langchain_core.outputs import ChatGeneration, LLMResult

    from retail_agent.observability.tracer import TraceCallback, Tracer, TurnContext, estimate_cost

    turn = TurnContext(tracer=Tracer(tmp_path), session_id="s", user_id="u")
    msg = AIMessage("answer", response_metadata={"model_name": "gemini-3.8-flash"}, usage_metadata={
        "input_tokens": 1000, "output_tokens": 900, "total_tokens": 1900,
        "output_token_details": {"reasoning": 800},  # SDK folds thoughts into output_tokens
    })
    TraceCallback(turn).on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]), run_id=None)
    call = [e for e in Tracer(tmp_path).read_trace(turn.trace_id) if e["event"] == "llm_call"][0]
    assert call["reasoning_tokens"] == 800 and turn.tokens_reasoning == 800
    assert turn.cost_usd == pytest.approx(estimate_cost("gemini-3.8-flash", 1000, 900))


def test_interrupted_turn_closes_trace_and_repairs_history(tmp_path):
    class Interrupt(BaseException):
        pass

    llm = FakeLLM([tool_call("describe_dataset"), Interrupt(), AIMessage("Recovered.")])
    svc = make_service(tmp_path, llm)
    sid = svc.new_session()
    with pytest.raises(Interrupt):
        svc.chat("ceo", sid, "first")
    ends = [t for t in svc.services.tracer.turns() if t["session_id"] == sid]
    assert ends and ends[-1]["status"] == "error"
    assert svc.chat("ceo", sid, "second").text == "Recovered."
