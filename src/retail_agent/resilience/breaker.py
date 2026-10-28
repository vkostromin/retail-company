"""Minimal thread-safe circuit breaker (closed -> open -> half-open)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from .errors import CircuitOpenError

T = TypeVar("T")


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 4, reset_timeout_s: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if time.monotonic() - self._opened_at >= self.reset_timeout_s:
                return "half_open"
            return "open"

    def call(self, fn: Callable[[], T], *, counts_as_failure: Callable[[BaseException], bool] | None = None) -> T:
        if self.state == "open":
            raise CircuitOpenError(self.name, f"circuit open after {self._failures} consecutive failures")
        try:
            result = fn()
        except BaseException as exc:
            if counts_as_failure is None or counts_as_failure(exc):
                self._record_failure()
            raise
        self._record_success()
        return result

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()

    def _record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
