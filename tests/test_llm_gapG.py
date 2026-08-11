"""Test per il Gap G: costi LLM da config e retry 4xx-aware.

- I prezzi (USD per milione di token) provengono da LocusConfig, non hardcoded.
- Il retry avviene solo su errori server/transitori (5xx, 429, timeout,
  connessione); gli errori client 4xx falliscono subito (fail fast).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import pytest

from locus.config import LocusConfig
from locus.exceptions import LLMError
from locus.llm import LLMClient

# ── Fake LLM transport con contatore call e factory di errori ─────────


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 20


class FakeChoices:
    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content: str = "ok") -> None:
        self.choices = [FakeChoices(content)]
        self.usage = FakeUsage()


class FakeCompletions:
    def __init__(self, transport: "CountingTransport") -> None:
        self._transport = transport

    async def create(self, **kwargs: Any):
        self._transport.calls += 1
        # La factory restituisce un'eccezione da sollevare oppure None per riuscire.
        error = self._transport._error_factory()
        if error is not None:
            raise error
        return FakeResponse()


class FakeChat:
    def __init__(self, transport: "CountingTransport") -> None:
        self.completions = FakeCompletions(transport)


class CountingTransport:
    """Trasporto fake che conta le chiamate e può sollevare errori."""

    def __init__(
        self, error_factory: Callable[[], Optional[Exception]] = lambda: None
    ) -> None:
        self.calls = 0
        self._error_factory = error_factory
        self.chat = FakeChat(self)


class HTTPError(Exception):
    """Errore HTTP duck-typed (come le eccezioni openai con status_code)."""

    def __init__(self, status_code: int, message: str = "http error") -> None:
        super().__init__(message)
        self.status_code = status_code


def _http(status: int) -> Exception:
    return HTTPError(status)


# ── Costi da config (Gap G) ───────────────────────────────────────────


def test_cost_defaults_preserve_historic_values() -> None:
    """I default di config conservano i valori storici (3.0 / 15.0)."""
    cfg = LocusConfig(_env_file=None)
    assert cfg.llm_input_price_per_m == 3.0
    assert cfg.llm_output_price_per_m == 15.0


async def test_cost_uses_config_values() -> None:
    """I prezzi usati per il calcolo costi arrivano dalla configurazione."""
    cfg = LocusConfig(_env_file=None, llm_input_price_per_m=10.0, llm_output_price_per_m=20.0)
    client = LLMClient(cfg, transport=CountingTransport())
    await client.generate(system="s", user="u")
    # FakeUsage: 10 prompt + 20 completion token.
    expected = (10 / 1_000_000) * 10.0 + (20 / 1_000_000) * 20.0
    assert client.usage.snapshot()["total_cost_usd"] == round(expected, 4)


def test_cost_env_override() -> None:
    """Le variabili LOCUS_* sovrascrivono i prezzi (convenzione env_prefix)."""
    import os

    os.environ["LOCUS_LLM_INPUT_PRICE_PER_M"] = "7.5"
    try:
        cfg = LocusConfig(_env_file=None)
        assert cfg.llm_input_price_per_m == 7.5
    finally:
        del os.environ["LOCUS_LLM_INPUT_PRICE_PER_M"]


# ── Retry 4xx-aware (Gap G) ───────────────────────────────────────────

# La semantica di retry vive in _call_with_retry: lo testiamo direttamente
# così il conteggio chiamate riflette SOLO i retry (niente fallback di modello).


async def _run_call(client: LLMClient) -> str:
    return await client._call_with_retry(  # noqa: SLF001 — test diretto del retry
        system="s",
        user="u",
        model="test-model",
        temperature=0.7,
        max_tokens=100,
        response_format=None,
    )


async def test_4xx_does_not_retry() -> None:
    """Un errore client 4xx non viene ritentato: una sola chiamata, fail fast."""
    transport = CountingTransport(error_factory=lambda: _http(400))
    client = LLMClient(LocusConfig(_env_file=None), transport=transport)
    with pytest.raises(LLMError):
        await _run_call(client)
    assert transport.calls == 1


async def test_5xx_does_retry_then_succeeds() -> None:
    """Un errore server 5xx viene ritentato e la seconda chiamata riesce."""
    state = {"failures": 1}

    def factory() -> Optional[Exception]:
        if state["failures"] > 0:
            state["failures"] -= 1
            return _http(500)
        return None  # nessun altro errore: la chiamata riesce

    transport = CountingTransport(error_factory=factory)
    client = LLMClient(LocusConfig(_env_file=None), transport=transport)
    client.RETRY_BASE_DELAY = 0  # niente sleep nei test
    result = await _run_call(client)
    assert result == "ok"
    assert transport.calls == 2


async def test_429_is_retriable() -> None:
    """Il rate-limit (429) viene ritentato."""
    state = {"failures": 1}

    def factory() -> Optional[Exception]:
        if state["failures"] > 0:
            state["failures"] -= 1
            return _http(429)
        return None  # nessun altro errore: la chiamata riesce

    transport = CountingTransport(error_factory=factory)
    client = LLMClient(LocusConfig(_env_file=None), transport=transport)
    client.RETRY_BASE_DELAY = 0
    result = await _run_call(client)
    assert transport.calls == 2
    assert result == "ok"


async def test_auth_4xx_fails_with_single_call() -> None:
    """Errori di autenticazione 4xx (401/403) falliscono subito senza retry."""
    for status in (401, 403):
        transport = CountingTransport(error_factory=lambda s=status: _http(s))
        client = LLMClient(LocusConfig(_env_file=None), transport=transport)
        with pytest.raises(LLMError):
            await _run_call(client)
        assert transport.calls == 1, f"status {status} non deve ritentare"

