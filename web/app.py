"""Retail Insights web app: chat + admin pages (Persona editor, Traces).

Run: streamlit run web/app.py   (or `make web`)
Identity is a simple user picker for the prototype; production sits behind IAP/OIDC.
"""

import streamlit as st
from common import service

st.set_page_config(page_title="Retail Insights", page_icon="📊", layout="wide")
svc = service()

with st.sidebar:
    st.markdown("### 📊 Retail Insights")
    users = {p.user_id: p for p in svc.users()}
    chosen = st.selectbox(
        "Signed in as", list(users), index=list(users).index(st.session_state.get("user_id", "ceo")),
        format_func=lambda u: f"{users[u].display_name}",
        help="Prototype login. In production identity comes from IAP / OIDC.",
    )
    if chosen != st.session_state.get("user_id"):
        st.session_state.clear()
        st.session_state["user_id"] = chosen
    me = users[chosen]
    st.caption(f"Role: **{me.role}** · Data access: {me.scope.describe()}")

pages = [st.Page("views/chat.py", title="Chat", icon="💬", default=True)]
if me.is_admin:
    pages += [st.Page("views/persona.py", title="Persona (admin)", icon="🎭"),
              st.Page("views/traces.py", title="Traces & metrics (admin)", icon="🔎")]
st.navigation(pages).run()
