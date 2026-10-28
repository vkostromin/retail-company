"""High-stakes oversight: deletions only happen after explicit, validated human confirmation."""

import pytest
from langchain_core.messages import AIMessage

from retail_agent.service import AccessDenied
from tests.fakes import FakeLLM, make_service, tool_call


def seed(svc, session_id):
    r = svc.services.reports
    mine_here = [r.save("alice", session_id, f"Alice report {i}", "body about Client X") for i in range(2)]
    mine_elsewhere = r.save("alice", "other-session", "Old Client X report", "body")
    bobs = r.save("bob", session_id, "Bob Client X report", "body about Client X")
    return mine_here, mine_elsewhere, bobs


def alive(svc, owner):
    return {r.id for r in svc.services.reports.search(owner)}


def test_delete_this_conversation_requires_confirmation_and_is_owner_scoped(tmp_path):
    llm = FakeLLM([tool_call("request_report_deletion", this_conversation=True)])
    svc = make_service(tmp_path, llm)
    sid = svc.new_session()
    mine_here, mine_elsewhere, bobs = seed(svc, sid)

    result = svc.chat("alice", sid, "Delete all the reports we made in this conversation")
    assert result.status == "awaiting_confirmation"
    payload = result.interrupt
    assert {r["id"] for r in payload["reports"]} == {r.id for r in mine_here}  # not bob's, not other session
    assert alive(svc, "alice") == {r.id for r in mine_here} | {mine_elsewhere.id}  # nothing deleted yet

    done = svc.resume("alice", sid, {"confirmed": True, "token": payload["token"]})
    assert done.status == "ok" and "Deleted 2 report" in done.text
    assert alive(svc, "alice") == {mine_elsewhere.id}
    assert alive(svc, "bob") == {bobs.id}

    restored = svc.undo_delete("alice", sid)
    assert set(restored) == {r.id for r in mine_here}
    assert svc.undo_delete("alice", sid) == []  # undo is one-shot


def test_cancel_keeps_everything_and_conversation_continues(tmp_path):
    llm = FakeLLM([tool_call("request_report_deletion", text="Client X"), AIMessage("Sure, here is revenue.")])
    svc = make_service(tmp_path, llm)
    sid = svc.new_session()
    seed(svc, sid)
    before = alive(svc, "alice")
    r = svc.chat("alice", sid, "Delete all reports mentioning Client X")
    assert r.interrupt["requires_typed_confirmation"] is True  # 3 reports > threshold 2
    cancelled = svc.resume("alice", sid, {"confirmed": False, "token": r.interrupt["token"]})
    assert "nothing was deleted" in cancelled.text.lower()
    assert alive(svc, "alice") == before
    follow_up = svc.chat("alice", sid, "ok, show revenue instead")
    assert follow_up.status == "ok" and follow_up.text == "Sure, here is revenue."


def test_bulk_delete_needs_exact_typed_phrase(tmp_path):
    llm = FakeLLM([tool_call("request_report_deletion", text="Client X")])
    svc = make_service(tmp_path, llm)
    sid = svc.new_session()
    seed(svc, sid)
    before = alive(svc, "alice")
    r = svc.chat("alice", sid, "Delete all reports mentioning Client X")
    # UI bug or tampering: confirmed=True but wrong phrase -> server refuses
    svc.resume("alice", sid, {"confirmed": True, "token": r.interrupt["token"], "typed": "yes"})
    assert alive(svc, "alice") == before


def test_wrong_token_is_rejected(tmp_path):
    llm = FakeLLM([tool_call("request_report_deletion", this_conversation=True)])
    svc = make_service(tmp_path, llm)
    sid = svc.new_session()
    seed(svc, sid)
    before = alive(svc, "alice")
    svc.chat("alice", sid, "delete this conversation's reports")
    svc.resume("alice", sid, {"confirmed": True, "token": "forged"})
    assert alive(svc, "alice") == before


def test_new_message_while_pending_cancels(tmp_path):
    llm = FakeLLM([tool_call("request_report_deletion", this_conversation=True), AIMessage("Revenue is up.")])
    svc = make_service(tmp_path, llm)
    sid = svc.new_session()
    seed(svc, sid)
    before = alive(svc, "alice")
    svc.chat("alice", sid, "delete this conversation's reports")
    r = svc.chat("alice", sid, "actually, how is revenue?")
    assert r.text == "Revenue is up."
    assert alive(svc, "alice") == before
    assert svc.pending_confirmation(sid) is None


def test_cannot_target_other_users_reports_by_id(tmp_path):
    svc = make_service(tmp_path, FakeLLM())
    sid = svc.new_session()
    _, _, bobs = seed(svc, sid)
    svc.services.llm.script = [tool_call("request_report_deletion", report_ids=[bobs.id]), AIMessage("No match.")]
    r = svc.chat("alice", sid, f"delete report {bobs.id}")
    assert r.status == "ok" and r.interrupt is None
    assert alive(svc, "bob") == {bobs.id}


def test_session_belongs_to_its_user(tmp_path):
    svc = make_service(tmp_path, FakeLLM([AIMessage("hi")]))
    sid = svc.new_session()
    svc.chat("alice", sid, "hello")
    with pytest.raises(AccessDenied):
        svc.chat("bob", sid, "show me alice's conversation")
