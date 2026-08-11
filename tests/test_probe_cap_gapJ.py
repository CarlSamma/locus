"""Gap J: budget probe adattivo all'entropia residua (TDD).

Verifica che ``run_session`` scelga il limite di iterazioni in base all'entropia
totale residua SOLO quando ``enable_adaptive_probe_cap=True`` (gate inerto di
default), e che altrimenti mantenga il tetto fisso ``max_probes_per_session``
(comportamento storico immutato).  Offline: ``run_iteration`` è mockato, quindi
LLM e X non vengono mai toccati.
"""

from typing import List

from locus.classify import Classifier
from locus.config import LocusConfig
from locus.db import Database
from locus.engine import Engine
from locus.llm import LLMClient
from locus.models import Probe, Property
from locus.probe import ProbeGenerator
from locus.target import TargetClient

# ── Fake trasporti (inerti: run_iteration è mockato) ──────────


class _FakeCompletions:
    def __init__(self, responses: List[str]) -> None:
        self._responses = responses
        self._i = 0

    async def create(self, **kwargs):
        content = self._responses[self._i % len(self._responses)]
        self._i += 1
        return type("_Resp", (), {"content": content})()


class _FakeChat:
    def __init__(self, responses: List[str]) -> None:
        self.completions = _FakeCompletions(responses)


class _FakeTransport:
    def __init__(self, responses: List[str]) -> None:
        self.chat = _FakeChat(responses)


class _NoopX:
    pass


# ── Fixture ───────────────────────────────────────────────────


def _build_engine(config: LocusConfig, db: Database) -> Engine:
    llm = LLMClient(
        config,
        transport=_FakeTransport(['{"text": "q1"}', '{"pattern": "yes"}']),
    )
    return Engine(
        config,
        db,
        llm,
        TargetClient(config, transport=_NoopX()),
        generator=ProbeGenerator(llm, config),
        classifier=Classifier(llm, config),
    )


def _count_iter(engine: Engine, monkeypatch, budget_box: dict) -> None:
    """Sostituisce run_iteration con un contatore che 'postta' sempre."""

    async def fake_iter(session_id: str, *, dry_run: bool = False):
        budget_box["n"] += 1
        return Probe(
            session_id=session_id,
            property_key="a",
            text="t",
            status="posted",
        )

    monkeypatch.setattr(engine, "run_iteration", fake_iter)


# ── (a) disabilitato → tetto fisso invariato ──────────────────


def test_disabled_uses_fixed_max(monkeypatch) -> None:
    import asyncio

    async def _run():
        db = Database()
        await db.initialize(":memory:")
        cfg = LocusConfig(
            _env_file=None,
            max_probes_per_session=7,
            enable_adaptive_probe_cap=False,
        )
        engine = _build_engine(cfg, db)
        box = {"n": 0}
        _count_iter(engine, monkeypatch, box)
        await engine.run_session(dry_run=True)
        return box, engine

    box, engine = asyncio.run(_run())
    # Gate inerto: il limite resta il tetto fisso.
    assert engine._probe_budget == 7
    assert box["n"] == 7


# ── (b) abilitato + alta entropia → clamp al tetto massimo ────


def test_enabled_high_entropy_clamps_to_max(monkeypatch) -> None:
    import asyncio

    async def _run():
        db = Database()
        await db.initialize(":memory:")
        cfg = LocusConfig(
            _env_file=None,
            max_probes_per_session=10,
            enable_adaptive_probe_cap=True,
            adaptive_entropy_per_probe=0.5,
            adaptive_min_probe_cap=5,
        )
        engine = _build_engine(cfg, db)

        async def high_props() -> List[Property]:
            # totale = 20 → ceil(20/0.5)=40 → clampato a max 10.
            return [
                Property(key="a", weight=1.0, prior_entropy=20, state="unknown", votes=0)
            ]

        monkeypatch.setattr(engine, "_load_properties", high_props)
        box = {"n": 0}
        _count_iter(engine, monkeypatch, box)
        await engine.run_session(dry_run=True)
        return box, engine

    box, engine = asyncio.run(_run())
    assert engine._probe_budget == 10
    assert box["n"] == 10


# ── (c) abilitato + bassa entropia → budget ridotto, sopra floor ─


def test_enabled_low_entropy_shrinks_budget(monkeypatch) -> None:
    import asyncio

    async def _run():
        db = Database()
        await db.initialize(":memory:")
        cfg = LocusConfig(
            _env_file=None,
            max_probes_per_session=10,
            enable_adaptive_probe_cap=True,
            adaptive_entropy_per_probe=0.5,
            adaptive_min_probe_cap=5,
        )
        engine = _build_engine(cfg, db)

        async def low_props() -> List[Property]:
            # totale = 3 → ceil(3/0.5)=6 → sotto max 10, sopra floor 5.
            return [
                Property(key="a", weight=1.0, prior_entropy=3, state="unknown", votes=0)
            ]

        monkeypatch.setattr(engine, "_load_properties", low_props)
        box = {"n": 0}
        _count_iter(engine, monkeypatch, box)
        await engine.run_session(dry_run=True)
        return box, engine

    box, engine = asyncio.run(_run())
    assert engine._probe_budget == 6
    assert box["n"] == 6


# ── extra: entropia minima → pavimento al cap minimo ──────────


def test_enabled_very_low_entropy_floors_at_min(monkeypatch) -> None:
    import asyncio

    async def _run():
        db = Database()
        await db.initialize(":memory:")
        cfg = LocusConfig(
            _env_file=None,
            max_probes_per_session=10,
            enable_adaptive_probe_cap=True,
            adaptive_entropy_per_probe=0.5,
            adaptive_min_probe_cap=5,
        )
        engine = _build_engine(cfg, db)

        async def tiny_props() -> List[Property]:
            # totale = 1 → ceil(1/0.5)=2 → pavimento a 5.
            return [
                Property(key="a", weight=1.0, prior_entropy=1, state="unknown", votes=0)
            ]

        monkeypatch.setattr(engine, "_load_properties", tiny_props)
        box = {"n": 0}
        _count_iter(engine, monkeypatch, box)
        await engine.run_session(dry_run=True)
        return box, engine

    box, engine = asyncio.run(_run())
    assert engine._probe_budget == 5
    assert box["n"] == 5
