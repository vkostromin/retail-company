"""Fault injection for demos and tests: RA_FAKE_FAIL=llm_primary,bigquery,embeddings,..."""

from __future__ import annotations

import threading

from .errors import InjectedFault


class FaultInjector:
    """Points: llm_primary, llm_all, guard, bigquery, bigquery_once, embeddings."""

    def __init__(self, points: frozenset[str] | set[str] = frozenset()):
        self.points = set(points)
        self._lock = threading.Lock()

    def check(self, point: str) -> None:
        with self._lock:
            once = f"{point}_once"
            if once in self.points:
                self.points.discard(once)
                raise InjectedFault(f"injected transient failure at '{point}' (once)")
        if point in self.points:
            raise InjectedFault(f"injected failure at '{point}'")
