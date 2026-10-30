"""Shared helpers for the Streamlit pages."""

from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
warnings.filterwarnings("ignore")
logging.getLogger("retail_agent").setLevel(logging.ERROR)

from retail_agent.service import AgentService  # noqa: E402


@st.cache_resource(show_spinner="Starting the assistant…")
def service() -> AgentService:
    return AgentService()


def current_user():
    uid = st.session_state.get("user_id")
    return service().principal(uid) if uid else None


TOOL_RESULT_HEIGHT = 340  # ~15 lines of code, then the box scrolls vertically


def _height(text: str, wrapped: bool) -> int | str:
    lines = sum(max(1, len(ln) // 110 + 1) for ln in text.splitlines()) if wrapped else text.count("\n") + 1
    return TOOL_RESULT_HEIGHT if lines > 15 else "content"


def render_tool_result(text: str, key: str) -> None:
    """Tool output in a fixed-height scrolling box, with an optional pretty-printed JSON view."""
    import json

    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None  # not JSON, or cut short by the trace size limit
    if parsed is not None and st.toggle("Format JSON", key=f"fmt-{key}"):
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        st.code(pretty, language="json", height=_height(pretty, wrapped=False))
        return
    if parsed is None and text.lstrip().startswith("{"):
        st.caption("Result truncated in the trace - shown as raw text.")
    st.code(text, language="json", wrap_lines=True, height=_height(text, wrapped=True))


def render_messages(messages: list[dict], key: str = "msg") -> None:
    """Redacted transcript: user/assistant text, tool calls with arguments, tool results."""
    import json

    icons = {"user": "🧑‍💼", "assistant": "📊", "tool": "🛠"}
    for i, m in enumerate(messages):
        st.markdown(f"{icons.get(m['role'], '•')} **{m['role']}**" + (f" `{m['tool']}`" if m.get("tool") else ""))
        if m.get("text"):
            if m["role"] == "tool":
                render_tool_result(m["text"], key=f"{key}-{i}")
            else:
                st.markdown(m["text"])
        for c in m.get("tool_calls") or []:
            args = dict(c["args"])
            sql = args.pop("sql", None)
            st.code(f"{c['name']}({json.dumps(args, ensure_ascii=False, indent=1)})", wrap_lines=True)
            if isinstance(sql, str):
                st.code(sql, language="sql", wrap_lines=True, height=_height(sql, wrapped=True))


def render_timeline(events: list[dict]) -> None:
    import pandas as pd

    from retail_agent.observability.render import offset_ms, summarize

    st.dataframe(pd.DataFrame([{"+ms": offset_ms(events, e), "event": e["event"], "details": summarize(e)}
                               for e in events]), hide_index=True, width="stretch")
