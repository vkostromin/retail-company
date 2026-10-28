"""Chat page: same AgentService as the CLI, including the human-in-the-loop confirmation."""

import pandas as pd
import streamlit as st
from common import current_user, service

svc = service()
me = current_user()
uid = me.user_id

if "session_id" not in st.session_state:
    st.session_state["session_id"] = svc.new_session()
sid = st.session_state["session_id"]

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    if st.button("➕ New conversation", width="stretch"):
        st.session_state["session_id"] = svc.new_session()
        st.session_state.pop("last", None)
        st.rerun()
    past = [s["session_id"] for s in svc.sessions(uid) if not s["session_id"].startswith("preview-")]
    if past:
        pick = st.selectbox("Resume a conversation", ["—", *past], index=0)
        if pick != "—" and pick != sid:
            st.session_state["session_id"] = pick
            st.session_state.pop("last", None)
            st.rerun()

    st.markdown("#### 🗂 My reports")
    for r in svc.list_reports(uid)[:15]:
        with st.expander(f"#{r.id} {r.title}"):
            st.caption(f"{r.created_at} · conversation {r.conversation_id}")
            st.markdown(r.body_md)
    if st.button("↩️ Undo last delete", width="stretch"):
        ids = svc.undo_delete(uid, sid)
        st.toast(f"Restored reports {ids}" if ids else "Nothing to undo")
        st.rerun()

    st.markdown("#### ⚙️ My preferences")
    prefs = svc.preferences(uid)
    st.caption(", ".join(f"{k}: {v}" for k, v in prefs.items()) or "None yet - just tell the assistant, "
               "e.g. *I prefer bullet points*.")

st.title("💬 Ask about sales, products, customers and orders")
st.caption(f"Conversation `{sid}` · you can analyse **{me.scope.describe()}**")

# ------------------------------------------------------------------ history
for role, text in svc.history(sid):
    with st.chat_message(role, avatar="🧑‍💼" if role == "user" else "📊"):
        st.markdown(text)

last = st.session_state.get("last")
if last and last.status != "awaiting_confirmation":
    cols = st.columns([6, 1, 1])
    cols[0].caption(f"trace `{last.trace_id}` · {last.latency_ms / 1000:.1f}s · {last.tokens:,} tokens · "
                    f"~${last.cost_usd:.4f} · {last.status}")
    if cols[1].button("👍", key=f"up-{last.trace_id}"):
        svc.feedback(uid, sid, last.trace_id, 1)
        st.toast("Thanks!")
    if cols[2].button("👎", key=f"down-{last.trace_id}"):
        svc.feedback(uid, sid, last.trace_id, -1)
        st.toast("Thanks - we'll look into it.")


def run(fn, *args):
    with st.status("Thinking…", expanded=False) as status:
        result = fn(*args, progress=lambda m: status.update(label=m))
        status.update(label="Done", state="complete")
    st.session_state["last"] = result
    st.rerun()


# ------------------------------------------------------------------ confirmation (human in the loop)
pending = svc.pending_confirmation(sid)
if pending:
    with st.container(border=True):
        st.error(f"**Confirm deletion of {pending['count']} report(s).** This cannot be done without you.", icon="⚠️")
        st.dataframe(pd.DataFrame(pending["reports"])[["id", "title", "created_at", "snippet"]],
                     hide_index=True, width="stretch")
        typed = ""
        if pending["requires_typed_confirmation"]:
            typed = st.text_input(f"Type **{pending['expected_phrase']}** to confirm")
        c1, c2 = st.columns(2)
        can_confirm = not pending["requires_typed_confirmation"] or typed.strip().lower() == pending["expected_phrase"]
        if c1.button("🗑 Delete", type="primary", disabled=not can_confirm, width="stretch"):
            run(svc.resume, uid, sid, {"confirmed": True, "token": pending["token"], "typed": typed})
        if c2.button("Cancel", width="stretch"):
            run(svc.resume, uid, sid, {"confirmed": False, "token": pending["token"]})

if prompt := st.chat_input("e.g. Why did revenue change last month compared to the month before?"):
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(prompt)
    run(svc.chat, uid, sid, prompt)
