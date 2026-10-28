"""Tool registry: adding a capability = registering one ToolSpec.

A tool is a pydantic args model + a plain function. The agent gets the JSON schema; the
graph's tool node validates arguments, runs the function with a ToolContext (who is
asking, which services are available) and records a trace event. Future capabilities
(charts, e-mailing reports, web trend search) plug in here; outward-facing ones reuse the
same confirmation node as deletions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..observability.tracer import TurnContext
    from ..safety.entitlements import Principal
    from ..services import Services


@dataclass
class ToolContext:
    principal: Principal
    session_id: str
    settings: Settings
    services: Services
    turn: TurnContext
    state: dict
    updates: dict = field(default_factory=dict)  # state updates requested by tools

    def bump(self, key: str, by: int = 1) -> int:
        value = self.updates.get(key, self.state.get(key, 0) or 0) + by
        self.updates[key] = value
        return value

    def current(self, key: str, default: Any = 0) -> Any:
        return self.updates.get(key, self.state.get(key, default))


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[[ToolContext, BaseModel], dict]
    progress: str = "Working…"

    def schema(self) -> dict:
        params = self.args.model_json_schema()
        params.pop("title", None)
        for prop in params.get("properties", {}).values():
            prop.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                  "parameters": params}}


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None):
        self._specs: dict[str, ToolSpec] = {}
        for s in specs or []:
            self.register(s)

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def schemas(self) -> list[dict]:
        return [s.schema() for s in self._specs.values()]

    def names(self) -> list[str]:
        return list(self._specs)
