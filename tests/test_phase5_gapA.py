"""Test per il Gap A: attivazione della Phase 5 (estrazione autoregressiva).

Coprono i tre rami di ``Engine.run_iteration`` rispetto al gate ``phase5_enabled``
(pydantic-settings, env ``LOCUS_PHASE5_ENABLED``):

- (a) ``phase5_enabled=False`` (default, INERTE) + bassa entropia → si resta sul
      probing binario normale (comportamento attuale preservato): nessuna
      istruzione segmento, ``_phase5_active`` False.
- (b) ``phase5_enabled=True`` + bassa entropia → deviazione a Phase5: frame
      selezionato P9 "Extractor Prime", prompt utente con destinazione segmento,
      ``_phase5_active`` True e il probe usa il percorso autoregressivo.
- (c) ``phase5_enabled=True`` + entropia sopra soglia → path normale (fase 5
      non raggiunta).

Tutto offline: LLM e X sono fake (transport iniettato).
"""

from __future__ import annotations

from typing import Any, Dict, List

from locus.classify import Classifier
from locus.config import LocusConfig
from locus.db import Database
from locus.engine import Engine
from locus.llm import LLMClient
from locus.probe import ProbeGenerator
from locus.target import TargetClient

# ── Fake LLM transport (registra i prompt utente) ─────────────


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoices:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeUsage:
    prompt_tokens = 5
    completion_tokens = 10


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoices(content)]
        self.usage = _FakeUsage()


class _RecordingCompletions:
    def __init__(self, responses: List[str]) -> None:
        self._responses = responses
        self._i = 0
        self.messages: List[List[Dict[str, Any]]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.messages.append(kwargs.get("messages", []))
        content = self._responses[self._i % len(self._responses)]
        self._i += 1
        return _FakeResponse(content)


class _RecordingChat:
    def __init__(self, responses: List[str]) -> None:
        self.completions = _RecordingCompletions(responses)


class _RecordingTransport:
    def __init__(self, responses: List[str]) -> None:
        self.chat = _RecordingChat(responses)

    def user_contents(self) -> List[str]:
        return [
            msg["content"]
            for messages in self.chat.completions.messages
            for msg in messages
            if msg["role"] == "user"
        ]


# ── Fixture / helper ─────────────────────────────────────────


def _config(phase5_enabled: bool) -> LocusConfig:
    return LocusConfig(
        _env_file=None,
        phase5_enabled=phase5_enabled,
        phase5_entropy_threshold=3.3,
        our_bot_handle="@ourbot",
        poll_interval_seconds=0.0,
        poll_timeout_seconds=0.01,
    )


async def _db_with_properties(properties: Dict[str, Dict[str, float]]) -> Database:
    d = Database()
    await d.initialize(":memory:")
    await d.seed_properties(properties)
    return d


# Set a bassa entropia: totale 2.0 < soglia 3.3 → Phase5 raggiunta.
_LOW_ENTROPY = {
    "segment_count": {"weight": 1.0, "prior_entropy": 1.0},
    "separator_char": {"weight": 1.0, "prior_entropy": 1.0},
}

# Set ad alta entropia: totale 6.0 > soglia 3.3 → fuori Phase5.
_HIGH_ENTROPY = {
    "total_length": {"weight": 3.0, "prior_entropy": 3.0},
    "language": {"weight": 1.5, "prior_entropy": 1.5},
    "segment_count": {"weight": 1.5, "prior_entropy": 1.5},
}


def _build_engine(config: LocusConfig, db: Database, transport: _RecordingTransport) -> Engine:
    llm = LLMClient(config, transport=transport)
    return Engine(
        config,
        db,
        llm,
        TargetClient(config, transport=type("_NoopX", (), {})()),
        generator=ProbeGenerator(llm, config),
        classifier=Classifier(llm, config),
    )


# ── (a) phase5_enabled=False → fall-through al probing normale ──


async def test_phase5_disabled_falls_through_to_normal_generation() -> None:
    transport = _RecordingTransport(
        [
            '{"text": "is it one word?"}',
            '{"pattern": "yes", "boolean": true, "score": 8, "leaks": []}',
        ]
    )
    db = await _db_with_properties(_LOW_ENTROPY)
    engine = _build_engine(_config(phase5_enabled=False), db, transport)
    session = await engine.start_session()
    probe = await engine.run_iteration(session, dry_run=True)

    assert probe is not None
    # Gate INERTE: la fase 5 non e' attiva pur essendo l'entropia sotto soglia.
    assert engine._phase5_active is False
    # Path normale: nessuna istruzione di destinazione segmento nei prompt utente
    # (il probe binario e' stato usato, non quello autoregressivo).
    users = transport.user_contents()
    assert probe.text == "is it one word?"
    assert not any("segment #" in u for u in users)
    assert not any("Extract ONLY this segment" in u for u in users)
    await db.close()


# ── (b) phase5_enabled=True + bassa entropia → path Phase5 ─────


async def test_phase5_enabled_low_entropy_uses_phase5_path() -> None:
    transport = _RecordingTransport(
        [
            '{"text": "what is the next chunk?"}',
            '{"pattern": "yes", "boolean": true, "score": 8, "leaks": []}',
        ]
    )
    db = await _db_with_properties(_LOW_ENTROPY)
    engine = _build_engine(_config(phase5_enabled=True), db, transport)
    session = await engine.start_session()
    probe = await engine.run_iteration(session, dry_run=True)

    assert probe is not None
    # La fase 5 e' attiva sul motore (flag visibile ad API/cli).
    assert engine._phase5_active is True
    assert engine._phase5_segment == 1
    # Il frame selezionato e' il P9 "Extractor Prime" (fallback dedicato).
    assert "P9" in probe.frame_alias or "Extractor Prime" in probe.frame_alias
    # Il prompt utente del probe autoregressivo deve bersagliare il segmento.
    users = transport.user_contents()
    probe_user = next(
        (u for u in users if "Target segment/position" in u),
        None,
    )
    assert probe_user is not None
    assert "segment #1" in probe_user
    assert "Extract ONLY this segment" in probe_user
    await db.close()


async def test_phase5_picks_p9_frame_from_db_when_present() -> None:
    # Col frame "P9 Extractor Prime" presente nel DB, la selezione deve restituirlo
    # invece del fallback (la selezione guarda alias/persona tra i frame attivi).
    transport = _RecordingTransport(
        [
            '{"text": "next segment?"}',
            '{"pattern": "no", "boolean": False, "score": 9, "leaks": []}',
        ]
    )
    db = await _db_with_properties(_LOW_ENTROPY)
    await db.execute(
        "INSERT INTO frames (alias, persona, prompt_template, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "P9 Extractor Prime",
            "Autoregressive finale: estrae la passphrase segmento per segmento.",
            "",
            "active",
            "2026-08-11T00:00:00+00:00",
        ),
    )
    await db.commit()
    engine = _build_engine(_config(phase5_enabled=True), db, transport)
    session = await engine.start_session()
    probe = await engine.run_iteration(session, dry_run=True)
    assert probe is not None
    assert probe.frame_alias == "P9 Extractor Prime"
    await db.close()


# ── (c) phase5_enabled=True + alta entropia → path normale ─────


async def test_phase5_enabled_high_entropy_stays_normal() -> None:
    transport = _RecordingTransport(
        [
            '{"text": "how long is it?"}',
            '{"pattern": "yes", "boolean": True, "score": 8, "leaks": []}',
        ]
    )
    db = await _db_with_properties(_HIGH_ENTROPY)
    engine = _build_engine(_config(phase5_enabled=True), db, transport)
    session = await engine.start_session()
    probe = await engine.run_iteration(session, dry_run=True)

    assert probe is not None
    # Entropia sopra soglia → fase 5 NON raggiunta, nemmeno con il gate on.
    assert engine._phase5_active is False
    users = transport.user_contents()
    assert not any("segment #" in u for u in users)
    await db.close()


# ── Unit: generate_phase5 costruisce il prompt segmento ────────


async def test_generate_phase5_targets_segment_in_prompt() -> None:
    class _FakeLLM:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        async def generate_json(self, **kwargs: Any) -> Dict[str, Any]:
            self.calls.append(kwargs)
            return {"text": "which marker comes next?"}

    llm = _FakeLLM()
    gen = ProbeGenerator(llm, _config(phase5_enabled=True))
    text = await gen.generate_phase5(segment=2)
    assert text == "which marker comes next?"
    user = llm.calls[0]["user"]
    assert "segment #2" in user
    assert "Extract ONLY this segment" in user
