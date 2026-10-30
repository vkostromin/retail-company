"""AgentService: the one entry point shared by the CLI and the web app.

In production this is the Chat API (Cloud Run) - clients are thin and call chat()/resume().
Every turn is wrapped in an error boundary: the UI never sees an exception, only a
TurnResult with a status and a trace id that links to the full event log.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from .agent.graph import build_graph
from .config import Settings, get_settings
from .observability.metrics import compute_metrics
from .observability.tracer import TraceCallback, TurnContext, compact_message, safe_preview
from .persona.store import Persona, PersonaVersion
from .safety.entitlements import Principal
from .services import Services, build_services
from .tools.base import ToolRegistry
from .tools.data_tools import DATA_TOOLS
from .tools.report_tools import REPORT_TOOLS

log = logging.getLogger(__name__)
MAX_INPUT_CHARS = 4000


@dataclass
class TurnResult:
    text: str
    trace_id: str
    status: str  # ok | blocked | degraded | error | awaiting_confirmation
    interrupt: dict | None = None
    latency_ms: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    flags: list[str] = field(default_factory=list)


class AccessDenied(PermissionError):
    pass


def _langfuse_handler():
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as e:  # noqa: BLE001 - observability must never break the app
        log.warning("Langfuse disabled: %s", e)
        return None


class AgentService:
    def __init__(self, settings: Settings | None = None, services: Services | None = None, checkpointer=None):
        self.settings = settings or get_settings()
        self.services = services or build_services(self.settings)
        self.registry = ToolRegistry(DATA_TOOLS + REPORT_TOOLS)
        if checkpointer is None:
            conn = sqlite3.connect(str(self.settings.checkpoint_db), check_same_thread=False)
            checkpointer = SqliteSaver(conn)
        self.graph = build_graph(self.services, self.registry, checkpointer)
        self.langfuse = _langfuse_handler()

    # ------------------------------------------------------------------ identity
    def principal(self, user_id: str) -> Principal:
        return self.services.directory.get(user_id)

    def users(self) -> list[Principal]:
        return self.services.directory.all()

    def new_session(self) -> str:
        return uuid.uuid4().hex[:16]

    def _thread(self, session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}}

    def _check_owner(self, session_id: str, user_id: str) -> None:
        values = self.graph.get_state(self._thread(session_id)).values
        owner = values.get("user_id")
        if owner and owner != user_id:
            raise AccessDenied("This conversation belongs to another user.")

    # ------------------------------------------------------------------ chat
    def chat(self, user_id: str, session_id: str, text: str,
             progress: Callable[[str], None] | None = None, persona_override: Persona | None = None,
             golden_exclude: frozenset[str] = frozenset()) -> TurnResult:
        self.principal(user_id)  # raises UnknownUserError
        self._check_owner(session_id, user_id)
        if self.pending_confirmation(session_id):
            # A new message while a confirmation is open means "no".
            self.resume(user_id, session_id, {"confirmed": False}, progress)
        turn = TurnContext(self.services.tracer, session_id, user_id, progress=progress or (lambda _m: None))
        turn.golden_exclude = frozenset(golden_exclude)
        text = (text or "").strip()
        if len(text) > MAX_INPUT_CHARS:
            turn.emit("input_truncated", chars=len(text))
            text = text[:MAX_INPUT_CHARS]
        turn.emit("turn_start", kind="message", input_preview=safe_preview(text), chars=len(text))
        payload = {"messages": [HumanMessage(text)], "user_id": user_id, "session_id": session_id}
        return self._run(turn, payload, persona_override)

    def resume(self, user_id: str, session_id: str, decision: dict,
               progress: Callable[[str], None] | None = None) -> TurnResult:
        self._check_owner(session_id, user_id)
        turn = TurnContext(self.services.tracer, session_id, user_id, progress=progress or (lambda _m: None))
        turn.emit("turn_start", kind="confirmation_response", confirmed=bool(decision.get("confirmed")))
        return self._run(turn, Command(resume=decision))

    def pending_confirmation(self, session_id: str) -> dict | None:
        snap = self.graph.get_state(self._thread(session_id))
        for it in getattr(snap, "interrupts", ()) or ():
            return it.value
        return None

    def _run(self, turn: TurnContext, payload, persona_override: Persona | None = None) -> TurnResult:
        callbacks = [TraceCallback(turn)]
        if self.langfuse:
            callbacks.append(self.langfuse)
        config = {
            "configurable": {"thread_id": turn.session_id, "turn": turn, "persona_override": persona_override},
            "callbacks": callbacks,
            "recursion_limit": 4 * self.settings.max_tool_calls + 10,
            "metadata": {"langfuse_session_id": turn.session_id, "langfuse_user_id": turn.user_id,
                         "trace_id": turn.trace_id},
            "run_name": "retail-agent-turn",
        }
        turn.message_offset = len(self.graph.get_state(self._thread(turn.session_id)).values.get("messages", []))
        try:
            out = self.graph.invoke(payload, config)
        except GraphRecursionError:
            turn.emit("error", error_type="GraphRecursionError", error="step limit reached")
            self._repair(turn.session_id)
            return self._finish(turn, "error", "That analysis took too many steps. Could you narrow the question?")
        except Exception as e:  # noqa: BLE001 - the UI must never crash
            turn.emit("error", error_type=type(e).__name__, error=str(e)[:500],
                      traceback=traceback.format_exc(limit=6)[-2000:])
            self._repair(turn.session_id)
            return self._finish(
                turn, "error",
                f"Sorry - something went wrong on my side and I couldn't finish. Your conversation is safe; "
                f"please try again. (reference: {turn.trace_id})",
            )
        except BaseException as e:  # Ctrl-C, UI reruns: close the trace and repair history, then propagate
            turn.emit("turn_interrupted", error_type=type(e).__name__)
            self._repair(turn.session_id)
            self._finish(turn, "error", "")
            raise
        interrupts = out.get("__interrupt__") if isinstance(out, dict) else None
        if interrupts:
            value = interrupts[0].value
            turn.emit("confirmation_shown", action=value.get("type"), count=value.get("count"))
            return self._finish(turn, "awaiting_confirmation", "", interrupt=value)
        last = out["messages"][-1]
        text = last.text if isinstance(last, AIMessage) else ""
        status = "blocked" if "blocked" in turn.flags else "degraded" if "degraded" in turn.flags else "ok"
        return self._finish(turn, status, text)

    def _finish(self, turn: TurnContext, status: str, text: str, interrupt: dict | None = None) -> TurnResult:
        try:  # the turn's own messages, so a trace alone shows the full correspondence
            messages = self.graph.get_state(self._thread(turn.session_id)).values.get("messages", [])
            turn.emit("turn_messages", messages=[compact_message(m, 6000) for m in messages[turn.message_offset:]])
        except Exception as e:  # noqa: BLE001 - observability must never break the turn
            log.warning("transcript capture failed: %s", e)
        turn.emit("turn_end", status=status, latency_ms=turn.elapsed_ms, tokens=turn.tokens,
                  tokens_in=turn.tokens_in, tokens_out=turn.tokens_out,
                  tokens_reasoning=turn.tokens_reasoning, llm_calls=turn.llm_calls,
                  cost_usd=round(turn.cost_usd, 6), sql_queries=len(turn.sql_executed), flags=sorted(turn.flags),
                  answer_preview=safe_preview(text, 400))
        return TurnResult(text=text, trace_id=turn.trace_id, status=status, interrupt=interrupt,
                          latency_ms=turn.elapsed_ms, tokens=turn.tokens, cost_usd=turn.cost_usd,
                          flags=sorted(turn.flags))

    def _repair(self, session_id: str) -> None:
        """After a crash, close dangling tool calls so the next turn has a valid history."""
        try:
            config = self._thread(session_id)
            messages = self.graph.get_state(config).values.get("messages", [])
            answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
            last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
            patch = []
            if last_ai is not None and last_ai.tool_calls:
                patch = [ToolMessage(content='{"status": "error", "error": "aborted"}', tool_call_id=c["id"],
                                     name=c["name"]) for c in last_ai.tool_calls if c["id"] not in answered]
            if patch or (messages and isinstance(messages[-1], HumanMessage | ToolMessage)):
                patch.append(AIMessage("(The previous request failed before completing.)"))
                self.graph.update_state(config, {"messages": patch, "pending_action": None})
        except Exception as e:  # noqa: BLE001
            log.warning("history repair failed: %s", e)

    # ------------------------------------------------------------------ conversation helpers
    def history(self, session_id: str) -> list[tuple[str, str]]:
        values = self.graph.get_state(self._thread(session_id)).values
        out = []
        for m in values.get("messages", []):
            if isinstance(m, HumanMessage):
                out.append(("user", m.text))
            elif isinstance(m, AIMessage) and m.text.strip():
                out.append(("assistant", m.text))
        return out

    def transcript(self, requester: str, session_id: str) -> list[dict]:
        """Full redacted conversation incl. tool calls and tool results (owner or admin only)."""
        values = self.graph.get_state(self._thread(session_id)).values
        owner = values.get("user_id")
        if owner and owner != requester and not self.principal(requester).is_admin:
            raise AccessDenied("You can only inspect your own conversations.")
        return [compact_message(m, 3000) for m in values.get("messages", [])]

    def sessions(self, user_id: str, limit: int = 15) -> list[dict]:
        seen: dict[str, dict] = {}
        for t in self.services.tracer.turns(days=30, user_id=user_id):
            seen.setdefault(t["session_id"], {"session_id": t["session_id"], "first": t["ts"]})["last"] = t["ts"]
        return sorted(seen.values(), key=lambda s: s["last"], reverse=True)[:limit]

    # ------------------------------------------------------------------ reports & preferences
    def list_reports(self, user_id: str, text: str | None = None):
        return self.services.reports.search(user_id, text=text)

    def get_report(self, user_id: str, report_id: int):
        return self.services.reports.get(user_id, report_id)

    def undo_delete(self, user_id: str, session_id: str | None = None) -> list[int]:
        ids = self.services.reports.undo_last_delete(user_id, self.settings.undo_window_minutes)
        self.services.tracer.emit("deletion_undone", trace_id=None, session_id=session_id, user_id=user_id, ids=ids)
        return ids

    def preferences(self, user_id: str) -> dict[str, str]:
        return self.services.prefs.get_all(user_id)

    def set_preference(self, user_id: str, key: str, value: str) -> None:
        self.services.prefs.set(user_id, key, value)

    def clear_preferences(self, user_id: str) -> None:
        self.services.prefs.clear(user_id)

    def feedback(self, user_id: str, session_id: str, trace_id: str | None, rating: int, comment: str = "") -> None:
        self.services.db.execute(
            "INSERT INTO feedback (user_id, session_id, trace_id, rating, comment) VALUES (?,?,?,?,?)",
            (user_id, session_id, trace_id, rating, comment[:500]),
        )
        self.services.tracer.emit("feedback", trace_id=trace_id, session_id=session_id, user_id=user_id,
                                  rating=rating, comment=safe_preview(comment, 200))

    # ------------------------------------------------------------------ persona (admin)
    def _require_admin(self, user_id: str) -> None:
        if not self.principal(user_id).is_admin:
            raise AccessDenied("Only admins can change the assistant's persona.")

    def persona(self) -> PersonaVersion:
        return self.services.personas.active()

    def persona_history(self) -> list[PersonaVersion]:
        return self.services.personas.history()

    def publish_persona(self, user_id: str, persona: Persona, note: str) -> PersonaVersion:
        self._require_admin(user_id)
        v = self.services.personas.publish(persona, author=user_id, note=note)
        self.services.tracer.emit("persona_published", trace_id=None, session_id=None, user_id=user_id,
                                  version=v.id, note=note)
        return v

    def preview_persona(self, user_id: str, draft: Persona, question: str,
                        progress: Callable[[str], None] | None = None) -> tuple[TurnResult, TurnResult]:
        """Answer the same question with the active persona and with an unpublished draft."""
        self._require_admin(user_id)
        current = self.chat(user_id, f"preview-{self.new_session()}", question, progress)
        drafted = self.chat(user_id, f"preview-{self.new_session()}", question, progress, persona_override=draft)
        return current, drafted

    def rollback_persona(self, user_id: str, version_id: int) -> PersonaVersion:
        self._require_admin(user_id)
        v = self.services.personas.rollback(version_id, author=user_id)
        self.services.tracer.emit("persona_published", trace_id=None, session_id=None, user_id=user_id,
                                  version=v.id, note=f"rollback to v{version_id}")
        return v

    # ------------------------------------------------------------------ observability
    def trace(self, requester: str, trace_id: str) -> list[dict]:
        events = self.services.tracer.read_trace(trace_id)
        if events and not self.principal(requester).is_admin and events[0].get("user_id") != requester:
            raise AccessDenied("You can only inspect your own traces.")
        return events

    def last_trace_id(self, user_id: str, session_id: str | None = None) -> str | None:
        turns = self.services.tracer.turns(days=2, user_id=user_id, session_id=session_id)
        return turns[-1]["trace_id"] if turns else None

    def metrics(self, requester: str, days: int = 7) -> dict:
        scope = None if self.principal(requester).is_admin else requester
        return compute_metrics(self.services.tracer, days=days, user_id=scope)
