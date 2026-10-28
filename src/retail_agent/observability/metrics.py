"""Agent-level metrics computed from the event log (what a dashboard would show)."""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median

from .tracer import Tracer


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(p / 100 * (len(values) - 1))))
    return values[k]


def compute_metrics(tracer: Tracer, days: int = 7, user_id: str | None = None) -> dict:
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for e in tracer.iter_events(days):
        if user_id and e.get("user_id") != user_id:
            continue
        if e.get("trace_id"):
            by_trace[e["trace_id"]].append(e)

    turns = [ev for evs in by_trace.values() for ev in evs if ev["event"] == "turn_end"]
    status = Counter(t.get("status") for t in turns)
    latencies = [t.get("latency_ms", 0) for t in turns]
    tokens = [t.get("tokens", 0) for t in turns]
    costs = [t.get("cost_usd", 0.0) for t in turns]

    sql_turns = first_try_ok = corrected = gave_up = empty = 0
    errors_by_type: Counter = Counter()
    guard_blocks: Counter = Counter()
    redactions = fallbacks = retries = 0
    confirmations: Counter = Counter()
    for evs in by_trace.values():
        attempts = [e for e in evs if e["event"] == "sql_attempt"]
        if attempts:
            sql_turns += 1
            if attempts[0].get("status") == "ok":
                first_try_ok += 1
            elif any(a.get("status") == "ok" for a in attempts):
                corrected += 1
            else:
                gave_up += 1
            empty += sum(1 for a in attempts if a.get("status") == "ok" and a.get("rows") == 0)
        for e in evs:
            ev = e["event"]
            if ev == "guardrail_block":
                guard_blocks[e.get("category", "unknown")] += 1
            elif ev == "pii_redaction":
                redactions += sum((e.get("counts") or {}).values())
            elif ev == "fallback_used":
                fallbacks += 1
            elif ev == "retry":
                retries += 1
            elif ev.startswith("confirmation_"):
                confirmations[ev.removeprefix("confirmation_")] += 1
            elif ev in ("error", "llm_error", "tool_error"):
                errors_by_type[e.get("error_type", ev)] += 1

    feedback = Counter(
        "up" if e.get("rating", 0) > 0 else "down"
        for evs in by_trace.values() for e in evs if e["event"] == "feedback"
    )
    n = len(turns) or 1
    # Blocked turns and pending confirmations are intended outcomes, not failures.
    answered = status.get("ok", 0) + status.get("error", 0) + status.get("degraded", 0)
    return {
        "turns": len(turns),
        "status": dict(status),
        "success_rate": round(status.get("ok", 0) / answered, 3) if answered else None,
        "block_rate": round(status.get("blocked", 0) / n, 3),
        "latency_ms_p50": median(latencies) if latencies else 0,
        "latency_ms_p95": _pct(latencies, 95),
        "avg_tokens_per_turn": round(sum(tokens) / n),
        "est_cost_usd_total": round(sum(costs), 4),
        "sql": {
            "turns_with_sql": sql_turns,
            "first_try_success_rate": round(first_try_ok / sql_turns, 3) if sql_turns else None,
            "self_corrected": corrected,
            "gave_up": gave_up,
            "empty_results": empty,
        },
        "guardrail_blocks": dict(guard_blocks),
        "pii_redactions": redactions,
        "llm_fallbacks": fallbacks,
        "retries": retries,
        "confirmations": dict(confirmations),
        "errors_by_type": dict(errors_by_type),
        "feedback": dict(feedback),
    }
