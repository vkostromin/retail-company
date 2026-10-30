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


def render_messages(messages: list[dict]) -> None:
    """Redacted transcript: user/assistant text, tool calls with arguments, tool results."""
    import json

    icons = {"user": "🧑‍💼", "assistant": "📊", "tool": "🛠"}
    for m in messages:
        st.markdown(f"{icons.get(m['role'], '•')} **{m['role']}**" + (f" `{m['tool']}`" if m.get("tool") else ""))
        if m.get("text"):
            if m["role"] == "tool":
                st.code(m["text"], language="json")
            else:
                st.markdown(m["text"])
        for c in m.get("tool_calls") or []:
            st.code(f"{c['name']}({json.dumps(c['args'], ensure_ascii=False, indent=1)})")


def render_timeline(events: list[dict]) -> None:
    import pandas as pd

    from retail_agent.observability.render import offset_ms, summarize

    st.dataframe(pd.DataFrame([{"+ms": offset_ms(events, e), "event": e["event"], "details": summarize(e)}
                               for e in events]), hide_index=True, width="stretch")
