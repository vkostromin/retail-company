"""Principals and their data entitlements (which products they may analyse)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class ProductScope(BaseModel):
    all_products: bool = False
    brands: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        if self.all_products:
            return "all products"
        parts = []
        if self.departments:
            parts.append("departments: " + ", ".join(self.departments))
        if self.categories:
            parts.append("categories: " + ", ".join(self.categories))
        if self.brands:
            parts.append("brands: " + ", ".join(self.brands))
        return "; ".join(parts) or "no products"


class Principal(BaseModel):
    user_id: str
    display_name: str
    role: Literal["admin", "analyst"] = "analyst"
    scope: ProductScope = Field(default_factory=ProductScope)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class UnknownUserError(KeyError):
    pass


class Directory:
    """Looks up principals. Re-reads users.yaml when it changes (no restart needed)."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime = 0.0
        self._users: dict[str, Principal] = {}

    def _load(self) -> None:
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        data = yaml.safe_load(self.path.read_text()) or {}
        self._users = {
            uid: Principal(user_id=uid, **spec) for uid, spec in (data.get("users") or {}).items()
        }
        self._mtime = mtime

    def get(self, user_id: str) -> Principal:
        self._load()
        try:
            return self._users[user_id]
        except KeyError:
            raise UnknownUserError(user_id) from None

    def all(self) -> list[Principal]:
        self._load()
        return list(self._users.values())
