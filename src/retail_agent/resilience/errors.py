"""Error taxonomy shared by all external dependencies."""

from __future__ import annotations


class DependencyUnavailable(RuntimeError):
    """A third-party dependency (LLM, BigQuery, embeddings) is down after retries/fallbacks."""

    def __init__(self, dependency: str, message: str):
        super().__init__(f"{dependency} unavailable: {message}")
        self.dependency = dependency


class CircuitOpenError(DependencyUnavailable):
    """Fast-fail: the circuit breaker is open, we do not even try the call."""


class InjectedFault(ConnectionError):
    """Raised by fault injection (RA_FAKE_FAIL) to demo resilience paths."""
