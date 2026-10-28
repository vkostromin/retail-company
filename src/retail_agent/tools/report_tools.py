"""Saved-report tools and preference memory.

`request_report_deletion` never deletes anything. It resolves the exact set of the
user's own reports, freezes their ids into `pending_action`, and the graph routes to a
deterministic confirmation node (human-in-the-loop) before any deletion happens.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from ..memory.preferences import ALLOWED_KEYS
from ..safety.pii import redact_text
from .base import ToolContext, ToolSpec


class SaveReportArgs(BaseModel):
    title: str = Field(max_length=150, description="Short, descriptive report title.")
    body_markdown: str = Field(description="The full report in Markdown (summary, findings, action items).")
    tags: list[str] = Field(default_factory=list, description="A few keywords (brands, topics, period).")


def save_report(ctx: ToolContext, args: SaveReportArgs) -> dict:
    body, counts = redact_text(args.body_markdown)
    title, tcounts = redact_text(args.title)
    if counts or tcounts:
        ctx.turn.emit("pii_redaction", layer="report", counts=dict(counts + tcounts))
    report = ctx.services.reports.save(
        owner_id=ctx.principal.user_id, conversation_id=ctx.session_id, title=title, body_md=body,
        sql_used=list(ctx.turn.sql_executed), tags=args.tags,
    )
    ctx.turn.emit("report_saved", report_id=report.id, title=title)
    return {"status": "saved", "report_id": report.id, "title": title}


class ListReportsArgs(BaseModel):
    text: str | None = Field(None, description="Only reports whose title/body/tags contain this text.")
    this_conversation: bool = Field(False, description="Only reports created in the current conversation.")


def list_reports(ctx: ToolContext, args: ListReportsArgs) -> dict:
    reports = ctx.services.reports.search(
        ctx.principal.user_id, text=args.text,
        conversation_id=ctx.session_id if args.this_conversation else None, limit=30,
    )
    return {"count": len(reports), "reports": [r.summary() for r in reports]}


class DeleteReportsArgs(BaseModel):
    text: str | None = Field(None, description="Delete reports whose title/body/tags mention this text "
                                               "(e.g. a client, brand or topic).")
    this_conversation: bool = Field(False, description="Delete reports created in the current conversation.")
    report_ids: list[int] | None = Field(None, description="Specific report ids.")
    all_my_reports: bool = Field(False, description="Only if the user explicitly asked to delete ALL their reports.")


def request_report_deletion(ctx: ToolContext, args: DeleteReportsArgs) -> dict:
    if not (args.text or args.this_conversation or args.report_ids or args.all_my_reports):
        return {"status": "needs_criteria",
                "error": "Specify which reports: text, this_conversation, report_ids or all_my_reports."}
    matches = ctx.services.reports.search(
        ctx.principal.user_id,  # ownership is enforced here, never chosen by the model
        text=args.text,
        conversation_id=ctx.session_id if args.this_conversation else None,
        ids=args.report_ids,
    )
    criteria = {k: v for k, v in args.model_dump().items() if v}
    if not matches:
        ctx.turn.emit("deletion_no_match", criteria=criteria)
        return {"status": "no_match", "message": "None of the user's reports match these criteria. Nothing to delete."}
    token = uuid.uuid4().hex
    ctx.updates["pending_action"] = {
        "type": "delete_reports",
        "token": token,
        "owner_id": ctx.principal.user_id,
        "criteria": criteria,
        "reports": [r.summary() for r in matches],
        "ids": [r.id for r in matches],
        "requires_typed_confirmation": len(matches) > ctx.settings.bulk_delete_threshold,
    }
    ctx.turn.emit("confirmation_requested", action="delete_reports", count=len(matches), criteria=criteria)
    return {"status": "awaiting_user_confirmation", "count": len(matches),
            "message": "The user is being asked to confirm. Nothing has been deleted yet."}


class PreferenceArgs(BaseModel):
    key: Literal["format", "depth", "charts", "language", "focus", "note"] = Field(
        description="; ".join(f"{k}: {v}" for k, v in ALLOWED_KEYS.items()))
    value: str = Field(max_length=300, description="The preference value, e.g. 'tables' or 'brief'.")


def remember_preference(ctx: ToolContext, args: PreferenceArgs) -> dict:
    ctx.services.prefs.set(ctx.principal.user_id, args.key, args.value, source="explicit")
    ctx.turn.emit("preference_set", key=args.key, value=args.value)
    return {"status": "saved", "key": args.key, "value": args.value}


REPORT_TOOLS = [
    ToolSpec("save_report", "Save a finished report to the user's Saved Reports library.", SaveReportArgs,
             save_report, progress="Saving the report…"),
    ToolSpec("list_reports", "List the user's saved reports (optionally filtered).", ListReportsArgs,
             list_reports, progress="Looking up your reports…"),
    ToolSpec("request_report_deletion", "Request deletion of the user's own saved reports. The system shows "
             "the exact list to the user and deletes only after explicit confirmation.", DeleteReportsArgs,
             request_report_deletion, progress="Finding matching reports…"),
    ToolSpec("remember_preference", "Remember how this user likes answers (format, depth, charts, language, "
             "focus) for future conversations.", PreferenceArgs, remember_preference,
             progress="Saving your preference…"),
]
