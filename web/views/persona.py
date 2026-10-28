"""Persona editor (admins): change the assistant's voice without a redeploy."""

import difflib

import pandas as pd
import streamlit as st
from common import current_user, service
from pydantic import ValidationError

from retail_agent.persona.store import Persona
from retail_agent.service import AccessDenied

svc = service()
me = current_user()
if not me.is_admin:
    st.error("Admins only.")
    st.stop()

active = svc.persona()
p = active.persona
st.title("🎭 Assistant persona")
st.caption(f"Active version **v{active.id}** by {active.author} at {active.created_at}. Changes apply to the "
           "**next message** in every open chat (web and CLI) - no restart, no redeploy. Safety rules are "
           "managed in code and cannot be changed here.")

TONES = {
    "(keep current)": p.tone,
    "Executive brief": "Very concise. Bottom line first, then at most 3 bullets. No jargon.",
    "Analytical deep-dive": "Thorough and analytical. Show the breakdown behind every number and state assumptions.",
    "Upbeat & motivating": "Positive, energetic and forward-looking, while staying factual about bad news.",
    "Formal board style": "Formal, measured and precise, suitable for a board pack.",
}

preset = st.selectbox("Start from a tone preset", list(TONES), help="Fills the Tone field below; edit it freely.")

with st.form("persona"):
    c1, c2 = st.columns(2)
    name = c1.text_input("Persona name", p.name, max_chars=80)
    detail = c2.selectbox("Detail level", ["brief", "standard", "deep"], ["brief", "standard", "deep"].index(p.detail_level))
    tone = st.text_area("Tone", TONES[preset], max_chars=400, height=80)
    audience = st.text_input("Audience", p.audience, max_chars=200)
    sections = st.text_area("Report sections (one per line, in order)", "\n".join(p.report_sections), height=120)
    formatting = st.text_area("Formatting guidance", p.formatting, max_chars=600, height=80)
    avoid = st.text_area("Avoid (one per line)", "\n".join(p.avoid), height=80)
    extra = st.text_area("Additional instructions", p.extra_instructions, max_chars=1500, height=80)
    note = st.text_input("Change note (for the audit trail)", placeholder="e.g. CEO tone for week 44")
    sample = st.text_input("Preview question", "Give me a quick summary of last month's revenue vs the month before.")
    b1, b2 = st.columns(2)
    do_preview = b1.form_submit_button("👀 Preview (does not publish)", width="stretch")
    do_publish = b2.form_submit_button("🚀 Publish", type="primary", width="stretch")

lines = lambda s: [x.strip() for x in s.splitlines() if x.strip()]  # noqa: E731
try:
    draft = Persona(name=name, tone=tone, audience=audience, detail_level=detail, report_sections=lines(sections),
                    formatting=formatting, avoid=lines(avoid), extra_instructions=extra)
except ValidationError as e:
    st.error(f"Invalid persona: {e.errors()[0]['msg']}")
    st.stop()

diff = list(difflib.unified_diff(p.render().splitlines(), draft.render().splitlines(), "active", "draft", lineterm=""))
with st.expander("Diff vs active version", expanded=bool(diff)):
    st.code("\n".join(diff) or "No changes.", language="diff")

if do_preview:
    with st.status("Answering with both personas…") as status:
        cur, new = svc.preview_persona(me.user_id, draft, sample, progress=lambda m: status.update(label=m))
        status.update(label="Preview ready", state="complete")
    a, b = st.columns(2)
    a.markdown(f"**Current (v{active.id})**")
    a.markdown(cur.text)
    b.markdown("**Draft**")
    b.markdown(new.text)

if do_publish:
    if not diff:
        st.info("Nothing to publish - the draft equals the active version.")
    else:
        try:
            v = svc.publish_persona(me.user_id, draft, note or "updated from admin panel")
            st.success(f"Published v{v.id}. It is live from the next message.")
            st.rerun()
        except AccessDenied as e:
            st.error(str(e))

st.subheader("History")
hist = svc.persona_history()
st.dataframe(pd.DataFrame([{"version": f"v{v.id}", "active": "●" if v.is_active else "", "author": v.author,
                            "created": v.created_at, "note": v.note, "tone": v.persona.tone} for v in hist]),
             hide_index=True, width="stretch")
c1, c2 = st.columns([3, 1])
target = c1.selectbox("Roll back to", [v.id for v in hist if not v.is_active], format_func=lambda i: f"v{i}")
if c2.button("⏪ Roll back", width="stretch", disabled=target is None):
    v = svc.rollback_persona(me.user_id, target)
    st.success(f"Rolled back: v{target} is live again as v{v.id}.")
    st.rerun()
