"""LLM gateway: one place for retries, circuit breaking, model fallback and rate limiting.

Call order for every request:
  rate limiter -> primary model (retry transient errors w/ backoff, circuit breaker)
               -> fallback model (different generation, independent failure modes)
               -> DependencyUnavailable (the graph turns this into a graceful answer)
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from ..config import Settings
from ..observability.tracer import TurnContext
from ..resilience.breaker import CircuitBreaker
from ..resilience.errors import DependencyUnavailable
from ..resilience.faults import FaultInjector
from ..resilience.retry import call_with_retry, is_transient


class LLMGateway:
    def __init__(self, settings: Settings, faults: FaultInjector | None = None):
        self.settings = settings
        self.faults = faults or FaultInjector()
        self.rate_limiter = InMemoryRateLimiter(
            requests_per_second=max(settings.llm_requests_per_minute, 1) / 60,
            check_every_n_seconds=0.05,
            max_bucket_size=5,
        )
        self.primary = self._model(settings.agent_model)
        self.fallback = self._model(settings.fallback_model) if settings.fallback_model else None
        self.guard = self._model(settings.guard_model, thinking="minimal")
        self._breakers: dict[str, CircuitBreaker] = {}

    def _model(self, name: str, thinking: str | None = None, timeout_s: int | None = None) -> ChatGoogleGenerativeAI:
        kwargs: dict[str, Any] = dict(
            model=name,
            temperature=0,
            max_retries=1,  # retries are owned by our policy, not the SDK
            timeout=timeout_s or self.settings.llm_timeout_s,
            rate_limiter=self.rate_limiter,
        )
        if thinking and name.startswith("gemini-3"):
            kwargs["thinking_level"] = thinking
        if self.settings.use_vertex:
            kwargs.update(vertexai=True, project=self.settings.gcp_project, location=self.settings.vertex_location)
        else:
            kwargs.update(api_key=self.settings.google_api_key)
        return ChatGoogleGenerativeAI(**kwargs)

    def _breaker(self, name: str) -> CircuitBreaker:
        return self._breakers.setdefault(name, CircuitBreaker(f"llm:{name}", failure_threshold=4, reset_timeout_s=60))

    def _attempt(self, model: ChatGoogleGenerativeAI, fault_point: str, fn, turn: TurnContext | None):
        def call():
            self.faults.check("llm_all")
            self.faults.check(fault_point)
            return fn(model)

        def on_retry(attempt: int, exc: BaseException):
            if turn:
                turn.emit("retry", dependency=f"llm:{model.model}", attempt=attempt, error_type=type(exc).__name__)
                turn.progress(f"Model busy, retrying ({attempt})…")

        return self._breaker(model.model).call(
            lambda: call_with_retry(call, attempts=3, initial=1.0, max_wait=15, on_retry=on_retry),
            counts_as_failure=is_transient,
        )

    def _with_failover(self, fn, turn: TurnContext | None, chain: list[tuple[ChatGoogleGenerativeAI, str]]):
        last: BaseException | None = None
        for i, (model, point) in enumerate(chain):
            try:
                result = self._attempt(model, point, fn, turn)
                if i > 0 and turn:
                    turn.emit("fallback_used", model=model.model, after_error=type(last).__name__)
                    turn.flags.add("fallback")
                return result
            except Exception as exc:  # noqa: BLE001 - every failure tries the next model
                last = exc
                if turn:
                    turn.emit("llm_failover" if i + 1 < len(chain) else "llm_unavailable",
                              model=model.model, error_type=type(exc).__name__, error=str(exc)[:300])
        raise DependencyUnavailable("llm", f"{type(last).__name__}: {last}")

    def chat(self, messages: list[BaseMessage], tools: list[dict] | None = None,
             turn: TurnContext | None = None) -> AIMessage:
        chain = [(self.primary, "llm_primary")]
        if self.fallback:
            chain.append((self.fallback, "llm_fallback"))

        def fn(model: ChatGoogleGenerativeAI) -> AIMessage:
            runnable = model.bind_tools(tools) if tools else model
            return runnable.invoke(messages)

        return self._with_failover(fn, turn, chain)

    def classify(self, schema: type[BaseModel], messages: list[BaseMessage],
                 turn: TurnContext | None = None) -> BaseModel:
        chain = [(self.guard, "guard"), (self.primary, "llm_primary")]

        def fn(model: ChatGoogleGenerativeAI):
            result = model.with_structured_output(schema).invoke(messages)
            if result is None:
                raise ValueError("empty structured output")
            return result

        return self._with_failover(fn, turn, chain)
