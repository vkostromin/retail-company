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
