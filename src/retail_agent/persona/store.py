"""Versioned persona store (Req 8): non-developers change the agent's voice at runtime.

Only the *style* block is editable. Safety rules live in code (agent/prompts.py) and are
always placed above the persona, so a tone change cannot weaken guardrails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ..data.sqlite import Database


class Persona(BaseModel):
    name: str = Field("Executive Analyst", max_length=80)
    tone: str = Field(..., max_length=400)
    audience: str = Field("Non-technical retail executives", max_length=200)
    detail_level: Literal["brief", "standard", "deep"] = "standard"
    report_sections: list[str] = Field(default_factory=list, max_length=12)
    formatting: str = Field("", max_length=600)
    avoid: list[str] = Field(default_factory=list, max_length=20)
    extra_instructions: str = Field("", max_length=1500)

    def render(self) -> str:
        lines = [
            f"Persona: {self.name}",
            f"Tone: {self.tone}",
            f"Audience: {self.audience}",
            f"Detail level: {self.detail_level}",
        ]
        if self.report_sections:
            lines.append("Report sections (in order): " + " | ".join(self.report_sections))
        if self.formatting:
            lines.append(f"Formatting: {self.formatting}")
        if self.avoid:
            lines.append("Avoid: " + "; ".join(self.avoid))
        if self.extra_instructions:
            lines.append(f"Additional style instructions: {self.extra_instructions}")
        return "\n".join(lines)


class PersonaVersion(BaseModel):
    id: int
    created_at: str
    author: str
    note: str
    persona: Persona
    is_active: bool


class PersonaStore:
    def __init__(self, db: Database, seed_file: Path | None = None):
        self.db = db
        if seed_file and not self.db.query("SELECT 1 FROM persona_versions LIMIT 1"):
            seed = Persona(**yaml.safe_load(seed_file.read_text()))
            self.publish(seed, author="system", note="seed from config/persona.yaml")

    def _version(self, r) -> PersonaVersion:
        return PersonaVersion(
            id=r["id"], created_at=r["created_at"], author=r["author"], note=r["note"],
            persona=Persona(**json.loads(r["data"])), is_active=bool(r["is_active"]),
        )

    def active(self) -> PersonaVersion:
        rows = self.db.query("SELECT * FROM persona_versions WHERE is_active=1 ORDER BY id DESC LIMIT 1")
        if not rows:
            raise LookupError("no active persona")
        return self._version(rows[0])

    def history(self, limit: int = 50) -> list[PersonaVersion]:
        return [self._version(r) for r in self.db.query(
            "SELECT * FROM persona_versions ORDER BY id DESC LIMIT ?", (limit,))]

    def publish(self, persona: Persona, author: str, note: str = "") -> PersonaVersion:
        """Versions are immutable; publishing appends a new one and makes it active."""
        with self.db.transaction() as conn:
            conn.execute("UPDATE persona_versions SET is_active=0 WHERE is_active=1")
            cur = conn.execute(
                "INSERT INTO persona_versions (author, note, data, is_active) VALUES (?,?,?,1)",
                (author, note[:300], persona.model_dump_json()),
            )
            new_id = cur.lastrowid
        return self._version(self.db.query("SELECT * FROM persona_versions WHERE id=?", (new_id,))[0])

    def rollback(self, version_id: int, author: str) -> PersonaVersion:
        rows = self.db.query("SELECT * FROM persona_versions WHERE id=?", (version_id,))
        if not rows:
            raise LookupError(f"persona version {version_id} not found")
        old = self._version(rows[0])
        return self.publish(old.persona, author=author, note=f"rollback to v{version_id}")
