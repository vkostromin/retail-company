"""Human-readable one-line summaries of trace events (shared by CLI /trace and web Traces)."""

from __future__ import annotations

from datetime import datetime

_SKIP = {"ts", "event", "trace_id", "session_id", "user_id", "traceback"}


def summarize(e: dict) -> str:
    ev = e["event"]
    if ev == "turn_start":
        return e.get("input_preview") or f"(confirmation: {'yes' if e.get('confirmed') else 'no'})"
    if ev == "guard_verdict":
        return f"{e.get('category')} via {e.get('source')} - {e.get('reason') or ''}"
    if ev == "guardrail_block":
        return f"BLOCKED by {e.get('layer')}: {e.get('category')} {e.get('reason') or ''}"
    if ev == "context_loaded":
        golden = ", ".join(f"{i}({s})" for i, s in e.get("golden") or []) or "none"
        return f"persona v{e.get('persona_version')} | prefs {e.get('preferences')} | playbook[{e.get('golden_mode')}]: {golden}"
    if ev == "llm_call":
        return (f"{e.get('model')} in={e.get('input_tokens')} out={e.get('output_tokens')} "
                f"{e.get('latency_ms')}ms finish={e.get('finish_reason')} ${e.get('cost_usd')}")
    if ev == "tool_call":
        return f"{e.get('tool')} -> {e.get('status')} ({e.get('latency_ms')}ms)"
    if ev == "sql_attempt":
        base = f"#{e.get('attempt')} {e.get('status')}"
        if e.get("status") == "ok":
            base += f" rows={e.get('rows')} bytes={e.get('bytes_processed')} cache={e.get('cache_hit')}"
        if e.get("error"):
            base += f" error={e.get('error')}"
        return f"{base} | {e.get('purpose', '')}\n{e.get('sql', '')}"
    if ev == "turn_end":
        return (f"{e.get('status')} {e.get('latency_ms')}ms tokens={e.get('tokens')} llm_calls={e.get('llm_calls')} "
                f"sql={e.get('sql_queries')} ${e.get('cost_usd')} flags={e.get('flags')}")
    rest = {k: v for k, v in e.items() if k not in _SKIP}
    return ", ".join(f"{k}={v}" for k, v in rest.items())[:600]


def offset_ms(events: list[dict], e: dict) -> int:
    t0 = datetime.fromisoformat(events[0]["ts"])
    return int((datetime.fromisoformat(e["ts"]) - t0).total_seconds() * 1000)
