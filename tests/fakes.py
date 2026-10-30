"""Offline fakes for the LLM gateway and BigQuery so graph flows are tested deterministically."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pandas as pd
from google.api_core.exceptions import BadRequest
from langchain_core.messages import AIMessage

from retail_agent.config import load_settings
from retail_agent.data.bq_client import QueryResult
from retail_agent.resilience.errors import DependencyUnavailable
from retail_agent.safety.input_guard import GuardVerdict


def tool_call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": uuid.uuid4().hex[:8], "type": "tool_call"}])


class FakeLLM:
    """Replays scripted responses. A script item may be an AIMessage, an exception or a callable(messages)."""

    def __init__(self, script: list | None = None, verdict: str = "analysis", classify_error: Exception | None = None):
        self.script = list(script or [])
        self.verdict = verdict
        self.classify_error = classify_error
        self.calls: list[list] = []
        self.tools_seen: list = []

    def chat(self, messages, tools=None, turn=None) -> AIMessage:
        self.calls.append(messages)
        self.tools_seen.append(tools)
        if not self.script:
            return AIMessage("Done.")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(messages)
        return item

    def classify(self, schema, messages, turn=None):
        if self.classify_error:
            raise self.classify_error
        return GuardVerdict(category=self.verdict, language="English", reason="test",
                            reply="Sorry, I can only help with retail data analysis." if self.verdict in {"off_topic", "malicious"} else "")


class FakeBQ:
    """`responder(sql) -> DataFrame` decides results; SQL containing 'bad_col' fails the dry-run."""

    def __init__(self, responder: Callable[[str], pd.DataFrame] | None = None, down: bool = False):
        self.responder = responder or (lambda sql: pd.DataFrame({"x": [1]}))
        self.down = down
        self.executed: list[tuple[str, list]] = []

    def dry_run(self, sql, params=()):
        if self.down:
            raise DependencyUnavailable("bigquery", "simulated outage")
        if "bad_col" in sql:
            raise BadRequest("Unrecognized name: bad_col at [1:8]")
        return 1000

    def run(self, sql, params=(), max_rows=None, labels=None, on_retry=None):
        if self.down:
            raise DependencyUnavailable("bigquery", "simulated outage")
        self.executed.append((sql, list(params)))
        return QueryResult(df=self.responder(sql), bytes_processed=1000, cache_hit=False, job_id="job", duration_ms=5)

    def get_table_schema(self, table):
        raise RuntimeError("offline")


class FakeGolden:
    def search(self, question, k=None, exclude=frozenset()):
        return [], "keyword"


def make_service(tmp_path, llm: FakeLLM, bq: FakeBQ | None = None):
    from langgraph.checkpoint.memory import InMemorySaver

    from retail_agent.service import AgentService
    from retail_agent.services import build_services

    settings = load_settings()
    settings.data_dir = tmp_path / "data"
    settings.reports_export_dir = tmp_path / "out"
    settings.fake_fail = frozenset()
    services = build_services(settings, bq=bq or FakeBQ(), llm=llm, golden=FakeGolden())
    return AgentService(settings=settings, services=services, checkpointer=InMemorySaver())
