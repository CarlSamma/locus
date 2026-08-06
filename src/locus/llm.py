"""Unified LLM gateway — a single resilient client for all LLM calls.

Ported and simplified from TAP's ``llm_client.py`` (Aware repo):
- Centralized retry with exponential backoff + model fallback
- Circuit breaker (trips after N consecutive failures, half-open probe after timeout)
- Robust JSON parsing (code-fence stripping + regex extraction fallback)
- Token usage tracking + cost estimation
- One structured call per reply (classification, score and leaks in one JSON)

Usage:
    client = LLMClient(config)
    result = await client.generate_json(
        system="You are a classifier.",
        user="Classify this reply: ...",
        temperature=0.1,
        model_tier=ModelTier.PRIMARY,
    )
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from locus.config import LocusConfig
from locus.exceptions import LLMError

# Code fence stripping regexes
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_INLINE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]", re.DOTALL)


class ModelTier(str, Enum):
    """Model selection tiers for different task complexity levels."""

    PRIMARY = "primary"  # routine tasks (classification, scoring)
    HARD = "hard"  # complex reasoning (probe generation, phase 5)


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Circuit breaker for LLM calls.

    Trips after `failure_threshold` consecutive failures. After
    `recovery_timeout` seconds, enters HALF_OPEN and allows one probe call.
    """

    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    _failure_count: int = 0
    _state: CircuitState = CircuitState.CLOSED
    _last_failure_time: float = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
        elif self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN

    @property
    def is_call_allowed(self) -> bool:
        return self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)


@dataclass
class TokenUsage:
    """Tracks cumulative token usage and estimated cost."""

    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_cost_usd: float = 0.0
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(
        self,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        success: bool = True,
    ) -> None:
        self.total_calls += 1
        if not success:
            self.total_failures += 1
            return
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        stats = self.per_model.setdefault(
            model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        stats["calls"] += 1
        stats["prompt_tokens"] += prompt_tokens
        stats["completion_tokens"] += completion_tokens
        p_cost, c_cost = 3.0, 15.0
        self.total_cost_usd += (prompt_tokens / 1_000_000) * p_cost
        self.total_cost_usd += (completion_tokens / 1_000_000) * c_cost

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "per_model": dict(self.per_model),
        }


class LLMClient:
    """Unified LLM gateway for all OpenRouter calls.

    Provides retry with exponential backoff, model fallback
    (hard → primary), circuit breaker and robust JSON parsing.

    The HTTP transport is injectable so tests can substitute a fake
    client without any network access.
    """

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2.0  # seconds

    def __init__(
        self,
        config: LocusConfig,
        transport: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._transport = transport  # injectable AsyncOpenAI-compatible client
        self._circuit = CircuitBreaker()
        self._usage = TokenUsage()
        self._models = {
            ModelTier.PRIMARY: config.llm_model_primary,
            ModelTier.HARD: config.llm_model_hard,
        }
        self._fallback_chain = [
            config.llm_model_hard,
            config.llm_model_primary,
        ]

    def _get_transport(self):
        if self._transport is not None:
            return self._transport
        if self._transport is None:
            from openai import AsyncOpenAI

            self._transport = AsyncOpenAI(
                base_url=self.config.llm_api_base,
                api_key=(
                    self.config.llm_api_key.get_secret_value()
                    if self.config.llm_api_key
                    else ""
                ),
            )
        return self._transport

    def _resolve_model(self, tier: ModelTier, explicit_model: Optional[str] = None) -> str:
        if explicit_model:
            return explicit_model
        return self._models.get(tier, self._models[ModelTier.PRIMARY])

    def _get_fallback_models(self, primary_model: str) -> list[str]:
        return [m for m in self._fallback_chain if m != primary_model]

    async def generate(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        model_tier: ModelTier = ModelTier.PRIMARY,
        model: Optional[str] = None,
        response_format: Optional[dict[str, str]] = None,
    ) -> str:
        """Generate a text completion from the LLM. Core method."""
        if not self._circuit.is_call_allowed:
            raise LLMError(
                "Circuit breaker is open — LLM calls suspended. "
                f"State: {self._circuit.state.value}"
            )

        primary_model = self._resolve_model(model_tier, model)
        models_to_try = [primary_model] + self._get_fallback_models(primary_model)
        last_error: Optional[Exception] = None

        for model_name in models_to_try:
            try:
                content = await self._call_with_retry(
                    system=system,
                    user=user,
                    model=model_name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                self._circuit.record_success()
                return content
            except LLMError as e:
                last_error = e
                continue
            except Exception as e:
                last_error = e
                continue

        self._circuit.record_failure()
        raise LLMError(
            f"All LLM models failed (tried {len(models_to_try)} models). "
            f"Last error: {last_error}",
            model=primary_model,
            original=last_error,
        )

    async def _call_with_retry(
        self,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        response_format: Optional[dict[str, str]],
    ) -> str:
        last_error: Optional[Exception] = None
        transport = self._get_transport()

        for attempt in range(self.MAX_RETRIES):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if response_format:
                    kwargs["response_format"] = response_format

                response = await transport.chat.completions.create(**kwargs)

                content = response.choices[0].message.content
                if not content:
                    raise LLMError("Empty response from LLM", model=model)

                if response.usage:
                    self._usage.record(
                        model=model,
                        prompt_tokens=response.usage.prompt_tokens or 0,
                        completion_tokens=response.usage.completion_tokens or 0,
                        success=True,
                    )
                else:
                    self._usage.record(model=model, success=True)
                return content.strip()

            except Exception as e:
                last_error = e
                wait_time = self.RETRY_BASE_DELAY ** (attempt + 1)
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(wait_time)

        self._usage.record(model=model, success=False)
        raise LLMError(
            f"LLM call failed after {self.MAX_RETRIES} retries: {last_error}",
            model=model,
            original=last_error,
        )

    async def generate_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 500,
        model_tier: ModelTier = ModelTier.PRIMARY,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Generate a JSON object with robust parsing."""
        content = await self.generate(
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
            model_tier=model_tier,
            model=model,
            response_format=(
                {"type": "json_object"} if self.config.llm_json_mode else None
            ),
        )
        return self._parse_json(content, model=model)

    async def generate_json_list(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.8,
        max_tokens: int = 2000,
        model_tier: ModelTier = ModelTier.HARD,
        model: Optional[str] = None,
    ) -> list[Any]:
        """Generate a JSON array without forcing json_object (which breaks lists)."""
        content = await self.generate(
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens,
            model_tier=model_tier,
            model=model,
        )
        return self._parse_json_list(content, model=model)

    @staticmethod
    def _strip_fences(text: str) -> str:
        match = _FENCE_RE.match(text.strip())
        if match:
            return match.group(1).strip()
        match = _INLINE_FENCE_RE.search(text)
        if match:
            return match.group(1).strip()
        return text.strip()

    def _parse_json(self, content: str, *, model: Optional[str] = None) -> dict[str, Any]:
        cleaned = self._strip_fences(content)
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        obj_match = _JSON_OBJECT_RE.search(cleaned)
        if obj_match:
            try:
                return json.loads(obj_match.group(0))
            except json.JSONDecodeError:
                pass
        raise LLMError(
            f"Failed to parse JSON from LLM response. Content: {content[:200]}",
            model=model,
        )

    def _parse_json_list(self, content: str, *, model: Optional[str] = None) -> list[Any]:
        cleaned = self._strip_fences(content)
        try:
            result = json.loads(cleaned)
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                for key in (
                    "probes",
                    "items",
                    "results",
                    "data",
                    "variants",
                    "options",
                    "choices",
                    "response",
                    "outputs",
                ):
                    if key in result and isinstance(result[key], list):
                        return result[key]
                return [result]
        except json.JSONDecodeError:
            pass
        list_match = _JSON_ARRAY_RE.search(cleaned)
        if list_match:
            try:
                result = json.loads(list_match.group(0))
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass
        lines = [
            line.strip().strip('"').strip("'").strip(",")
            for line in cleaned.split("\n")
        ]
        items = [line for line in lines if len(line) > 5]
        if items:
            return items
        raise LLMError(
            f"Failed to parse JSON list from LLM response. Content: {content[:200]}",
            model=model,
        )

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    @property
    def circuit_state(self) -> CircuitState:
        return self._circuit.state

    def get_health_status(self) -> dict[str, Any]:
        return {
            "circuit_state": self._circuit.state.value,
            "failure_count": self._circuit._failure_count,
            "models": dict(self._models),
            "usage": self._usage.snapshot(),
        }
