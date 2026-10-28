"""Per-user preferences (Req 4a). Explicit today; implicit extraction is a design item."""

from __future__ import annotations

from ..data.sqlite import Database

ALLOWED_KEYS = {
    "format": "How answers are laid out: tables | bullets | prose | mixed",
    "depth": "How deep the analysis goes: brief | standard | deep",
    "charts": "Visuals: prefer | avoid (charts are rendered as text tables in the CLI)",
    "language": "Reply language, e.g. English",
    "focus": "Business areas the user cares about most",
    "note": "Any other standing instruction from the user",
}


class PreferenceStore:
    def __init__(self, db: Database):
        self.db = db

    def get_all(self, user_id: str) -> dict[str, str]:
        rows = self.db.query("SELECT key, value FROM user_preferences WHERE user_id=? ORDER BY key", (user_id,))
        return {r["key"]: r["value"] for r in rows}

    def set(self, user_id: str, key: str, value: str, source: str = "explicit") -> None:
        if key not in ALLOWED_KEYS:
            raise ValueError(f"unknown preference '{key}'. Allowed: {', '.join(ALLOWED_KEYS)}")
        self.db.execute(
            "INSERT INTO user_preferences (user_id, key, value, source) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, source=excluded.source, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')",
            (user_id, key, value[:300], source),
        )

    def clear(self, user_id: str, key: str | None = None) -> None:
        if key:
            self.db.execute("DELETE FROM user_preferences WHERE user_id=? AND key=?", (user_id, key))
        else:
            self.db.execute("DELETE FROM user_preferences WHERE user_id=?", (user_id,))
