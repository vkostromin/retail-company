"""Saved Reports library. Every read/write is scoped to the owner in code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..data.sqlite import Database


@dataclass
class Report:
    id: int
    owner_id: str
    conversation_id: str
    title: str
    body_md: str
    sql_used: list[str]
    tags: list[str]
    created_at: str
    deleted_at: str | None = None

    def summary(self) -> dict:
        snippet = re.sub(r"\s+", " ", self.body_md)[:140]
        return {"id": self.id, "title": self.title, "created_at": self.created_at,
                "conversation_id": self.conversation_id, "snippet": snippet}


def _row(r) -> Report:
    return Report(
        id=r["id"], owner_id=r["owner_id"], conversation_id=r["conversation_id"], title=r["title"],
        body_md=r["body_md"], sql_used=json.loads(r["sql_used"]), tags=json.loads(r["tags"]),
        created_at=r["created_at"], deleted_at=r["deleted_at"],
    )


class ReportStore:
    def __init__(self, db: Database, export_dir: Path | None = None):
        self.db = db
        self.export_dir = export_dir

    def save(self, owner_id: str, conversation_id: str, title: str, body_md: str,
             sql_used: list[str] | None = None, tags: list[str] | None = None) -> Report:
        cur = self.db.execute(
            "INSERT INTO reports (owner_id, conversation_id, title, body_md, sql_used, tags) VALUES (?,?,?,?,?,?)",
            (owner_id, conversation_id, title, body_md, json.dumps(sql_used or []), json.dumps(tags or [])),
        )
        report = self.get(owner_id, cur.lastrowid)
        self._audit(owner_id, "report.create", [report.id], {"title": title})
        if self.export_dir:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
            (self.export_dir / f"{report.id:04d}-{slug}.md").write_text(f"# {title}\n\n{body_md}\n")
        return report

    def get(self, owner_id: str, report_id: int) -> Report | None:
        rows = self.db.query(
            "SELECT * FROM reports WHERE id=? AND owner_id=? AND deleted_at IS NULL", (report_id, owner_id)
        )
        return _row(rows[0]) if rows else None

    def search(self, owner_id: str, text: str | None = None, conversation_id: str | None = None,
               ids: list[int] | None = None, limit: int = 200) -> list[Report]:
        """Owner-scoped search. `text` matches title, body and tags (case-insensitive)."""
        sql = "SELECT * FROM reports WHERE owner_id=? AND deleted_at IS NULL"
        params: list = [owner_id]
        if text:
            sql += " AND (title LIKE ? OR body_md LIKE ? OR tags LIKE ?)"
            like = f"%{text}%"
            params += [like, like, like]
        if conversation_id:
            sql += " AND conversation_id=?"
            params.append(conversation_id)
        if ids:
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params += list(ids)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [_row(r) for r in self.db.query(sql, params)]

    def soft_delete(self, owner_id: str, ids: list[int], reason: dict) -> list[int]:
        """Soft-delete only ids that still exist and belong to owner. Returns deleted ids."""
        if not ids:
            return []
        with self.db.transaction() as conn:
            q = ",".join("?" * len(ids))
            live = [r["id"] for r in conn.execute(
                f"SELECT id FROM reports WHERE owner_id=? AND deleted_at IS NULL AND id IN ({q})",
                [owner_id, *ids],
            )]
            if live:
                q2 = ",".join("?" * len(live))
                conn.execute(
                    f"UPDATE reports SET deleted_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id IN ({q2})", live
                )
                conn.execute(
                    "INSERT INTO audit_log (actor_id, action, target_ids, details) VALUES (?,?,?,?)",
                    (owner_id, "report.delete", json.dumps(live), json.dumps(reason)),
                )
        return live

    def undo_last_delete(self, owner_id: str, window_minutes: int) -> list[int]:
        rows = self.db.query(
            "SELECT id, ts, target_ids FROM audit_log WHERE actor_id=? AND action='report.delete' "
            "ORDER BY id DESC LIMIT 1",
            (owner_id,),
        )
        if not rows:
            return []
        ts = datetime.strptime(rows[0]["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if datetime.now(UTC) - ts > timedelta(minutes=window_minutes):
            return []
        ids = json.loads(rows[0]["target_ids"])
        q = ",".join("?" * len(ids))
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE reports SET deleted_at=NULL WHERE owner_id=? AND id IN ({q})", [owner_id, *ids]
            )
            conn.execute(
                "INSERT INTO audit_log (actor_id, action, target_ids, details) VALUES (?,?,?,?)",
                (owner_id, "report.restore", json.dumps(ids), json.dumps({"undo_of": rows[0]["id"]})),
            )
            # mark the delete as consumed so a second /undo does nothing
            conn.execute("UPDATE audit_log SET action='report.delete.undone' WHERE id=?", (rows[0]["id"],))
        return ids

    def _audit(self, actor: str, action: str, ids: list[int], details: dict) -> None:
        self.db.execute(
            "INSERT INTO audit_log (actor_id, action, target_ids, details) VALUES (?,?,?,?)",
            (actor, action, json.dumps(ids), json.dumps(details)),
        )
