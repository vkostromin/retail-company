"""Structured, append-only event log (JSONL) + per-turn context.

Every event carries trace_id (one user turn), session_id (conversation) and user_id, so
a whole conversation can be replayed and any failure traced to the exact LLM call, SQL
attempt or guardrail decision. Production: the same events go to Cloud Logging (JSON),
are exported to BigQuery via a log sink, and spans go to Cloud Trace via OpenTelemetry.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from ..safety.pii import redact_text

# USD per 1M tokens (input, output). Estimates for dashboards; adjust to your contract.
MODEL_PRICES = {
    "flash-lite": (0.10, 0.40),
    "flash": (0.30, 2.50),
    "pro": (1.25, 10.00),
    "argon": (1.25, 10.00),  # placeholder: set to your contract price
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    model = (model or "").lower()
    key = ("flash-lite" if "lite" in model else "argon" if "argon" in model
           else "pro" if "pro" in model else "flash")
    pin, pout = MODEL_PRICES[key]
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


def _json_default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, Path):
        return str(o)
    return str(o)


class Tracer:
    def __init__(self, traces_dir: Path):
        self.dir = traces_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _file(self, day: datetime) -> Path:
        return self.dir / f"{day:%Y-%m-%d}.jsonl"

    def emit(self, event: str, *, trace_id: str | None, session_id: str | None, user_id: str | None,
             **payload: Any) -> dict:
        now = datetime.now(UTC)
        record = {
            "ts": now.isoformat(timespec="milliseconds"),
            "event": event,
            "trace_id": trace_id,
            "session_id": session_id,
            "user_id": user_id,
            **payload,
        }
        line = json.dumps(record, default=_json_default, ensure_ascii=False)
        with self._lock, self._file(now).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return record

    def iter_events(self, days: int = 7) -> Iterator[dict]:
        today = datetime.now(UTC)
        for i in range(days - 1, -1, -1):
            path = self._file(today - timedelta(days=i))
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    def read_trace(self, trace_id: str, days: int = 7) -> list[dict]:
        return [e for e in self.iter_events(days) if e.get("trace_id") == trace_id]

    def turns(self, days: int = 7, user_id: str | None = None, session_id: str | None = None) -> list[dict]:
        return [
            e for e in self.iter_events(days)
            if e["event"] == "turn_end"
            and (user_id is None or e.get("user_id") == user_id)
            and (session_id is None or e.get("session_id") == session_id)
        ]


@dataclass
class TurnContext:
    """Mutable per-turn accounting, shared by graph nodes, tools and callbacks."""

    tracer: Tracer
    session_id: str
    user_id: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started: float = field(default_factory=time.monotonic)
    tokens_in: int = 0
    tokens_out: int = 0  # includes reasoning (thinking) tokens - billed as output
    tokens_reasoning: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    sql_executed: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    progress: Callable[[str], None] = lambda _msg: None

    def emit(self, event: str, **payload: Any) -> dict:
        return self.tracer.emit(
            event, trace_id=self.trace_id, session_id=self.session_id, user_id=self.user_id, **payload
        )

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def safe_preview(text: str, limit: int = 300) -> str:
    """Truncated + PII-redacted copy of user text for logs."""
    redacted, _ = redact_text(text or "")
    return redacted[:limit]


class TraceCallback(BaseCallbackHandler):
    """Records every chat-model call: model, tokens, latency, finish reason, cost."""

    def __init__(self, turn: TurnContext):
        self.turn = turn
        self._starts: dict[uuid.UUID, tuple[float, str]] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        model = (kwargs.get("invocation_params") or {}).get("model") or (metadata or {}).get("ls_model_name", "")
        self._starts[run_id] = (time.monotonic(), model)

    def on_llm_end(self, response, *, run_id, **kwargs):
        started, model = self._starts.pop(run_id, (time.monotonic(), ""))
        usage, finish = {}, None
        try:
            msg = response.generations[0][0].message
            usage = msg.usage_metadata or {}
            finish = msg.response_metadata.get("finish_reason")
            model = msg.response_metadata.get("model_name") or model
        except (AttributeError, IndexError):
            pass
        tin, tout = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        # langchain-google-genai already folds thoughts into output_tokens; the detail shows how many.
        reasoning = int((usage.get("output_token_details") or {}).get("reasoning", 0))
        cost = estimate_cost(model, tin, tout)
        self.turn.tokens_in += tin
        self.turn.tokens_out += tout
        self.turn.tokens_reasoning += reasoning
        self.turn.llm_calls += 1
        self.turn.cost_usd += cost
        self.turn.emit(
            "llm_call", model=model, input_tokens=tin, output_tokens=tout, reasoning_tokens=reasoning,
            latency_ms=int((time.monotonic() - started) * 1000), finish_reason=finish,
            cost_usd=round(cost, 6),
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        started, model = self._starts.pop(run_id, (time.monotonic(), ""))
        self.turn.emit(
            "llm_error", model=model, error_type=type(error).__name__, error=str(error)[:300],
            latency_ms=int((time.monotonic() - started) * 1000),
        )
