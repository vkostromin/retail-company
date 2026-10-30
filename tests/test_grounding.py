"""Every figure in an answer must come from (or be derived from) the query results."""

from langchain_core.messages import AIMessage

from retail_agent.safety.grounding import ungrounded_numbers
from tests.fakes import FakeLLM, make_service, tool_call

EVIDENCE = ['{"rows": [{"year": 2024, "returned_items": 1380, "return_rate": 0.1176, "returned_revenue": 75737.0},'
            ' {"year": 2025, "returned_items": 2089, "return_rate": 0.1207, "returned_revenue": 107538.0}]}']


def test_direct_rounded_and_derived_figures_are_grounded():
    answer = ("In 2025 the return rate was 12.1% (2,089 items, $107,538), up 0.3 percentage points from 11.8% "
              "in 2024; returned revenue grew 42.0% (+$31,801).")
    assert ungrounded_numbers(answer, EVIDENCE) == []


def test_invented_figure_is_flagged():
    assert ungrounded_numbers("2024 net revenue was $599,232.", EVIDENCE) == ["599,232"]


def test_numbers_from_the_question_are_allowed():
    assert ungrounded_numbers("Here are the top 25 brands for Q3", [], ["top 25 brands"]) == []


def test_agent_revises_an_answer_with_an_invented_figure(tmp_path):
    llm = FakeLLM([
        tool_call("run_sql", sql="SELECT COUNT(*) n FROM orders", purpose="n"),
        AIMessage("We had 1,000 orders and $599,232 in revenue."),  # revenue was never queried
        AIMessage("We had 1,000 orders."),
    ])
    import pandas as pd

    from tests.fakes import FakeBQ
    svc = make_service(tmp_path, llm, FakeBQ(lambda sql: pd.DataFrame({"n": [1000]})))
    r = svc.chat("ceo", svc.new_session(), "orders and revenue?")
    assert r.text == "We had 1,000 orders."
    events = svc.services.tracer.read_trace(r.trace_id)
    check = next(e for e in events if e["event"] == "grounding_check")
    assert check["ungrounded"] == ["599,232"]
    assert next(e for e in events if e["event"] == "grounding_revised")["remaining"] == []
