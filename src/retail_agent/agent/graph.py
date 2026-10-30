"""LangGraph state machine.

START -> guard_input --blocked--> END
              |
              v
         load_context -> agent <-> tools --pending_action--> confirm (interrupt) -> execute_deletion -> END
                           |
                           v
                      guard_output -> END

`confirm` and `execute_deletion` are deterministic nodes. The model can only *request* a
destructive action; executing it requires a human decision delivered via
Command(resume=...), validated against a one-time token and the frozen id list.
"""

from __future__ import annotations

import json
import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import ValidationError

from ..observability.tracer import TurnContext, safe_preview
from ..resilience.errors import DependencyUnavailable
from ..safety.grounding import ungrounded_numbers
from ..safety.input_guard import REFUSAL_EN, GuardVerdict, build_classifier_messages, prefilter
from ..safety.pii import redact_text
from ..tools.base import ToolContext, ToolRegistry
from .prompts import build_system_prompt

LLM_DOWN_MESSAGE = (
    "I'm having trouble reaching the AI service right now, so I can't complete this analysis. "
    "Nothing was lost - please try again in a minute."
)
EMPTY_RESPONSE_MESSAGE = "I couldn't complete that analysis. Could you rephrase or narrow the question?"
HISTORY_WINDOW = 40
NO_CONTEXT_CATEGORIES = {"smalltalk", "preference", "report_management", "help"}


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    session_id: str
    verdict: dict | None
    context: dict | None
    sql_queries: int  # all run_sql calls this turn
    sql_errors: int  # failed / empty ones (self-corrections)
    tool_calls: int
    pending_action: dict | None


def _turn(config) -> TurnContext:
    return config["configurable"]["turn"]


def _text(msg: AnyMessage) -> str:
    return msg.text if hasattr(msg, "text") else str(msg.content)


def _last_human(messages: list[AnyMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return _text(m)
    return ""


def _window(messages: list[AnyMessage], n: int = HISTORY_WINDOW) -> list[AnyMessage]:
    """Recent history that starts at a user message (never with an orphan tool result)."""
    recent = messages[-n:]
    while recent and not isinstance(recent[0], HumanMessage):
        recent = recent[1:]
    return recent or messages[-1:]


def build_graph(services, registry: ToolRegistry, checkpointer=None):
    settings = services.settings

    # ------------------------------------------------------------------ guard_input
    def guard_input(state: AgentState, config) -> dict:
        turn = _turn(config)
        turn.progress("Checking the request…")
        messages = state.get("messages", [])
        text = _last_human(messages)
        history = [f"{'User' if isinstance(m, HumanMessage) else 'Assistant'}: {_text(m)[:200]}"
                   for m in messages[:-1] if isinstance(m, HumanMessage | AIMessage) and _text(m)]
        verdict, source = prefilter(text), "prefilter"
        if verdict is None:
            source = "classifier"
            try:
                verdict = services.llm.classify(GuardVerdict, build_classifier_messages(text, history), turn)
            except DependencyUnavailable as e:
                # Fail open: enforcement is in the data layer, keep the assistant available.
                turn.emit("guard_degraded", error=str(e)[:200])
                verdict = None
        reset: dict[str, Any] = {"sql_queries": 0, "sql_errors": 0, "tool_calls": 0, "pending_action": None,
                                 "verdict": verdict.model_dump() if verdict else None}
        turn.emit("guard_verdict", source=source, category=verdict.category if verdict else "unknown",
                  language=verdict.language if verdict else None, reason=verdict.reason if verdict else None,
                  input_preview=safe_preview(text))
        if verdict and verdict.blocked:
            turn.emit("guardrail_block", layer=source, category=verdict.category, reason=verdict.reason)
            turn.flags.add("blocked")
            return {**reset, "messages": [AIMessage(verdict.reply or REFUSAL_EN)]}
        return reset

    def after_guard(state: AgentState) -> str:
        v = state.get("verdict") or {}
        return END if v.get("category") in {"off_topic", "malicious"} else "load_context"

    # ------------------------------------------------------------------ load_context
    def load_context(state: AgentState, config) -> dict:
        turn = _turn(config)
        persona = services.personas.active()
        override = config["configurable"].get("persona_override")
        if override is not None:  # admin preview of an unpublished draft
            persona = persona.model_copy(update={"id": 0, "persona": override})
        prefs = services.prefs.get_all(state["user_id"])
        category = (state.get("verdict") or {}).get("category")
        examples, ids, mode = [], [], "skipped"
        if category not in NO_CONTEXT_CATEGORIES:
            turn.progress("Recalling how analysts approached similar questions…")
            hits, mode = services.golden.search(_last_human(state["messages"]), exclude=turn.golden_exclude)
            examples = [t.as_prompt() for t, _ in hits]
            ids = [(t.id, round(s, 3)) for t, s in hits]
        turn.emit("context_loaded", persona_version=persona.id, preferences=list(prefs), golden=ids,
                  golden_mode=mode)
        return {"context": {"persona_version": persona.id, "persona_text": persona.persona.render(),
                            "preferences": prefs, "examples": examples}}

    # ------------------------------------------------------------------ agent
    def agent(state: AgentState, config) -> dict:
        turn = _turn(config)
        principal = services.directory.get(state["user_id"])
        ctx = state.get("context") or {}
        system = build_system_prompt(
            user_name=principal.display_name, scope=principal.scope.describe(),
            persona_text=ctx.get("persona_text", ""), persona_version=ctx.get("persona_version", 0),
            preferences=ctx.get("preferences", {}), examples=ctx.get("examples", []),
            max_sql=settings.max_sql_queries, max_sql_errors=settings.max_sql_errors,
        )
        messages: list[AnyMessage] = [SystemMessage(system), *_window(state["messages"])]
        over_budget = (state.get("tool_calls", 0) >= settings.max_tool_calls
                       or turn.tokens >= settings.turn_token_budget)
        if over_budget:
            turn.emit("budget_exhausted", tool_calls=state.get("tool_calls", 0), tokens=turn.tokens)
            messages.append(HumanMessage("[system note] The analysis budget for this question is used up. "
                                         "Answer now with the results you already have; say what is missing."))
        turn.progress("Thinking…")
        try:
            response = services.llm.chat(messages, None if over_budget else registry.schemas(), turn)
            if over_budget and response.tool_calls:  # tools are off: never loop back into the tool node
                response = AIMessage(_text(response) or EMPTY_RESPONSE_MESSAGE)
            if not response.tool_calls and not _text(response).strip():
                turn.emit("empty_llm_response", finish_reason=response.response_metadata.get("finish_reason"))
                messages.append(HumanMessage("[system note] Your last reply was empty. Reply to the user now."))
                response = services.llm.chat(messages, None, turn)
                if not _text(response).strip() and not response.tool_calls:
                    response = AIMessage(EMPTY_RESPONSE_MESSAGE)
            if not response.tool_calls and _text(response).strip():
                response = ground_numbers(response, messages, state, turn)
        except DependencyUnavailable as e:
            turn.emit("dependency_down", dependency="llm", error=str(e)[:300])
            turn.flags.add("degraded")
            response = AIMessage(LLM_DOWN_MESSAGE)
        return {"messages": [response]}

    def ground_numbers(response: AIMessage, messages: list[AnyMessage], state: AgentState,
                       turn: TurnContext) -> AIMessage:
        """Every figure in the final answer must come from (or derive from) a query result."""
        evidence = [_text(m) for m in state["messages"] if isinstance(m, ToolMessage)]
        allowed = [_text(m) for m in state["messages"] if isinstance(m, HumanMessage)]
        missing = ungrounded_numbers(_text(response), evidence, allowed)
        if not missing:
            return response
        turn.emit("grounding_check", ungrounded=missing[:20], action="revise")
        turn.progress("Double-checking the numbers…")
        nudge = HumanMessage(
            "[system check] These figures in your answer do not appear in your query results and cannot be "
            f"derived from them: {', '.join(missing[:15])}. Rewrite the full answer: remove each unsupported "
            "figure or replace it with one from the results. Keep figures you derived by simple arithmetic "
            "from the results. Do not mention this check."
        )
        revised = services.llm.chat([*messages, response, nudge], None, turn)
        if not _text(revised).strip():
            return response
        remaining = ungrounded_numbers(_text(revised), evidence, allowed)
        turn.emit("grounding_revised", remaining=remaining[:20])
        return AIMessage(_text(revised))

    def after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "guard_output"

    # ------------------------------------------------------------------ tools
    def tools(state: AgentState, config) -> dict:
        turn = _turn(config)
        principal = services.directory.get(state["user_id"])
        tctx = ToolContext(principal=principal, session_id=state["session_id"], settings=settings,
                           services=services, turn=turn, state=dict(state))
        results: list[ToolMessage] = []
        for call in state["messages"][-1].tool_calls:
            name = call["name"]
            spec = registry.get(name)
            tctx.bump("tool_calls")
            started = time.monotonic()
            if spec is None:
                content: dict = {"status": "error", "error": f"Unknown tool '{name}'."}
            else:
                turn.progress(spec.progress)
                try:
                    content = spec.fn(tctx, spec.args.model_validate(call.get("args") or {}))
                except ValidationError as e:
                    content = {"status": "error", "error": f"Invalid arguments: {e.errors()[:3]}"}
                except Exception as e:  # noqa: BLE001 - a tool bug must not crash the turn
                    turn.emit("tool_error", tool=name, error_type=type(e).__name__, error=str(e)[:300])
                    content = {"status": "error", "error": "The tool failed unexpectedly."}
            args_preview = {k: (safe_preview(v, 500) if isinstance(v, str) else v)
                            for k, v in (call.get("args") or {}).items()}
            turn.emit("tool_call", tool=name, args=args_preview, status=content.get("status", "ok"),
                      latency_ms=int((time.monotonic() - started) * 1000))
            results.append(ToolMessage(content=json.dumps(content, default=str), tool_call_id=call["id"], name=name))
        return {"messages": results, **tctx.updates}

    def after_tools(state: AgentState) -> str:
        return "confirm" if state.get("pending_action") else "agent"

    # ------------------------------------------------------------------ confirmation
    def confirm(state: AgentState, config) -> dict:
        pa = state["pending_action"]
        # No side effects before interrupt(): the node is re-executed on resume.
        decision = interrupt({
            "type": pa["type"], "token": pa["token"], "reports": pa["reports"], "count": len(pa["ids"]),
            "requires_typed_confirmation": pa["requires_typed_confirmation"],
            "expected_phrase": f"delete {len(pa['ids'])}",
        })
        return {"pending_action": {**pa, "decision": decision}}

    def execute_deletion(state: AgentState, config) -> dict:
        turn = _turn(config)
        pa = state["pending_action"] or {}
        decision = pa.get("decision") or {}
        n = len(pa.get("ids", []))
        approved = (
            decision.get("confirmed") is True
            and decision.get("token") == pa.get("token")
            and pa.get("owner_id") == state["user_id"]
            and (not pa.get("requires_typed_confirmation")
                 or str(decision.get("typed", "")).strip().lower() == f"delete {n}")
        )
        if not approved:
            turn.emit("confirmation_cancelled", action=pa.get("type"), count=n,
                      reason="user_cancelled" if not decision.get("confirmed") else "validation_failed")
            text = "Cancelled - nothing was deleted. What would you like to do next?"
        else:
            deleted = services.reports.soft_delete(
                state["user_id"], pa["ids"],
                reason={"criteria": pa.get("criteria"), "session_id": state["session_id"], "trace_id": turn.trace_id},
            )
            turn.emit("confirmation_accepted", action=pa["type"], count=n)
            turn.emit("deletion_executed", ids=deleted, requested=n)
            titles = {r["id"]: r["title"] for r in pa["reports"]}
            listing = "\n".join(f"- #{i} {titles.get(i, '')}" for i in deleted)
            text = f"Deleted {len(deleted)} report(s):\n{listing}"
            if len(deleted) < n:
                text += f"\n({n - len(deleted)} had already been removed.)"
            text += (f"\n\nChanged your mind? Undo within {settings.undo_window_minutes} minutes "
                     "(`/undo` in the CLI, or *Undo last delete* in the web app).")
        return {"messages": [AIMessage(text)], "pending_action": None}

    # ------------------------------------------------------------------ guard_output
    def guard_output(state: AgentState, config) -> dict:
        turn = _turn(config)
        last = state["messages"][-1]
        if not isinstance(last, AIMessage):
            return {}
        redacted, counts = redact_text(_text(last))
        if counts:
            turn.emit("pii_redaction", layer="final_answer", counts=dict(counts))
            return {"messages": [AIMessage(content=redacted, id=last.id)]}
        return {}

    g = StateGraph(AgentState)
    g.add_node("guard_input", guard_input)
    g.add_node("load_context", load_context)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("confirm", confirm)
    g.add_node("execute_deletion", execute_deletion)
    g.add_node("guard_output", guard_output)
    g.add_edge(START, "guard_input")
    g.add_conditional_edges("guard_input", after_guard, ["load_context", END])
    g.add_edge("load_context", "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "guard_output"])
    g.add_conditional_edges("tools", after_tools, ["confirm", "agent"])
    g.add_edge("confirm", "execute_deletion")
    g.add_edge("execute_deletion", END)
    g.add_edge("guard_output", END)
    return g.compile(checkpointer=checkpointer)
