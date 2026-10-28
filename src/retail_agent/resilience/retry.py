"""Retry policy: exponential backoff with jitter, only for transient failures."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .errors import InjectedFault

log = logging.getLogger(__name__)
T = TypeVar("T")

_TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def is_transient(exc: BaseException) -> bool:
    """True for errors worth retrying (rate limits, 5xx, timeouts, network)."""
    if isinstance(exc, (InjectedFault, ConnectionError, TimeoutError)):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _TRANSIENT_HTTP:
        return True
    try:  # google-api-core (BigQuery)
        from google.api_core import exceptions as gexc

        if isinstance(
            exc,
            (
                gexc.TooManyRequests,
                gexc.ServiceUnavailable,
                gexc.InternalServerError,
                gexc.BadGateway,
                gexc.GatewayTimeout,
                gexc.DeadlineExceeded,
            ),
        ):
            return True
    except ImportError:  # pragma: no cover
        pass
    name = type(exc).__name__.lower()
    return any(k in name for k in ("timeout", "connection", "unavailable", "resourceexhausted"))


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    initial: float = 1.0,
    max_wait: float = 20.0,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Run `fn`, retrying transient failures. Non-transient errors propagate immediately."""

    def _before_sleep(state):
        exc = state.outcome.exception()
        log.warning("retrying after %s (attempt %s)", type(exc).__name__, state.attempt_number)
        if on_retry:
            on_retry(state.attempt_number, exc)

    retrying = Retrying(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=initial, max=max_wait),
        before_sleep=_before_sleep,
        reraise=True,
    )
    return retrying(fn)
