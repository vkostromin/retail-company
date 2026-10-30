"""Chat page: same AgentService as the CLI, including the human-in-the-loop confirmation."""

import pandas as pd
import streamlit as st
from common import current_user, render_messages, render_timeline, service

from retail_agent.service import AccessDenied

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

    if st.button("🧾 Show transcript", width="stretch", help="Whole conversation incl. tool calls (also: /transcript)"):
        st.session_state["panel"] = ("transcript", "")
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


COMMANDS_HELP = """**Commands** (handled by the app, not sent to the assistant)

| Command | |
|---|---|
| `/transcript` | whole conversation incl. tool calls and tool results (redacted) |
| `/trace [id]` | step-by-step trace of the last (or given) turn |
| `/reports [text]` | your saved reports |
| `/undo` | restore the last deleted reports |
| `/prefs [key=value \\| clear]` | view or set preferences (format, depth, charts, language, focus, note) |
| `/new` | start a new conversation |
| `/help` | this list |
"""


def handle_command(text: str) -> None:
    """Slash commands are UI actions, as in the CLI - never sent to the model."""
    cmd, _, arg = text[1:].partition(" ")
    arg = arg.strip()
    if cmd == "new":
        st.session_state["session_id"] = svc.new_session()
        st.session_state.pop("last", None)
        st.session_state.pop("panel", None)
    elif cmd == "undo":
        ids = svc.undo_delete(uid, sid)
        st.toast(f"Restored reports {ids}" if ids else "Nothing to undo")
    elif cmd == "prefs" and arg:
        if arg == "clear":
            svc.clear_preferences(uid)
        elif "=" in arg:
            k, _, v = arg.partition("=")
            try:
                svc.set_preference(uid, k.strip(), v.strip())
            except ValueError as e:
                st.session_state["panel"] = ("error", str(e))
                return
        st.session_state["panel"] = ("prefs", "")
    elif cmd in {"transcript", "trace", "reports", "prefs", "help"}:
        st.session_state["panel"] = (cmd, arg)
    else:
        st.session_state["panel"] = ("error", f"Unknown command `/{cmd}`. Type `/help`.")


def render_panel(kind: str, arg: str) -> None:
    with st.container(border=True):
        head, close = st.columns([8, 1])
        if close.button("✕", key="close-panel", help="Close"):
            st.session_state.pop("panel", None)
            st.rerun()
        try:
            if kind == "help":
                st.markdown(COMMANDS_HELP)
            elif kind == "error":
                st.warning(arg)
            elif kind == "transcript":
                messages = svc.transcript(uid, arg or sid)
                head.markdown(f"**Transcript** of `{arg or sid}` · {len(messages)} messages (redacted)")
                if messages:
                    render_messages(messages)
                else:
                    st.caption("No messages yet.")
            elif kind == "trace":
                tid = arg or (last.trace_id if last else None) or svc.last_trace_id(uid, sid)
                events = svc.trace(uid, tid) if tid else []
                head.markdown(f"**Trace** `{tid}`" if tid else "**Trace**")
                if events:
                    render_timeline(events)
                else:
                    st.caption("No trace yet.")
            elif kind == "reports":
                reports = svc.list_reports(uid, arg or None)
                head.markdown(f"**Your reports** ({len(reports)})")
                st.dataframe(pd.DataFrame([{"id": r.id, "title": r.title, "created": r.created_at,
                                            "conversation": r.conversation_id} for r in reports]),
                             hide_index=True, width="stretch")
            elif kind == "prefs":
                head.markdown("**Your preferences**")
                st.json(svc.preferences(uid) or {})
        except AccessDenied as e:
            st.error(str(e))


def run(fn, *args):
    def progress(message: str) -> None:
        # A new input triggers a Streamlit rerun, raised at the next st.* call. Swallow it here so
        # the running turn finishes and is saved; the new input is handled by the next script run.
        try:
            status.update(label=message)
        except BaseException:  # noqa: BLE001
            pass

    with st.status("Thinking…", expanded=False) as status:
        try:
            result = fn(*args, progress=progress)
        finally:
            st.session_state.pop("pending_prompt", None)
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

if panel := st.session_state.get("panel"):
    render_panel(*panel)

# Two-phase submit: store the prompt and rerun with the input disabled, then run the turn. A second
# message can therefore never interrupt (and orphan) a turn that is still running.
busy = st.session_state.get("pending_prompt")
if prompt := st.chat_input("Working on your question…" if busy else "Ask a question, or type /help for commands",
                           disabled=bool(busy)):
    if prompt.strip().startswith("/"):
        handle_command(prompt.strip())
    else:
        st.session_state["pending_prompt"] = prompt
    st.rerun()

if busy:
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(busy)
    run(svc.chat, uid, sid, busy)
