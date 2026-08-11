"""Test di scripts/autonomous_strike.py — AutonomousStrikeEngine (tutti offline).

Coverage: round-trip dello stato, checkpoint/load sul KV ``strike_state``,
calibrazione, ciclo completo dry-run, gate di sicurezza (breach approval, rate
limit, cost breaker), determinismo, reply simulate col pre-parse deterministico
(Gap C), persistenza delle probe e terminazione del loop adattivo.

Mai rete: il trasporto LLM esplode se toccato (tutte le reply simulate cadono
sul pre-parse) e il target finto registra le chiamate (che non devono mai
avvenire in dry-run).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Rende importabile ``scripts.autonomous_strike`` (namespace package) da qualsiasi
# invocazione di pytest (anche senza cwd su sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.autonomous_strike as ase  # noqa: E402
from locus.classify import (  # noqa: E402
    detect_acrostic,
    detect_boilerplate,
    detect_caesar,
    detect_dash_spell,
)
from locus.config import LocusConfig  # noqa: E402
from locus.db import Database  # noqa: E402
from locus.llm import LLMClient  # noqa: E402
from locus.target import TargetClient  # noqa: E402

_CFG = LocusConfig(_env_file=None)


# ── Finti di trasporto locali (self-contained) ───────────────────


class _BoomCompletions:
    async def create(self, **kwargs):
        raise AssertionError("il pre-parse deterministico NON deve chiamare l'LLM")


class BoomTransport:
    """Trasporto LLM che esplode se toccato: nessuna chiamata di rete nei test."""

    def __init__(self) -> None:
        self.chat = _BoomCompletions()


class _SilentTarget:
    """Target finto: registra le chiamate e poi esplode (mai usato in dry-run)."""

    def __init__(self) -> None:
        self.calls = []

    async def post_probe(self, text: str) -> str:
        self.calls.append(("post_probe", text))
        raise AssertionError("target non deve essere chiamato in dry-run")

    async def poll_replies(self, since_id=None):
        self.calls.append(("poll_replies", since_id))
        raise AssertionError("target non deve essere chiamato in dry-run")


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    yield d
    await d.close()


def _engine(db: Database, *, dry_run: bool = True) -> ase.AutonomousStrikeEngine:
    llm = LLMClient(_CFG, transport=BoomTransport())
    target = TargetClient(_CFG, transport=_SilentTarget())
    return ase.AutonomousStrikeEngine(_CFG, db, llm, target, dry_run=dry_run)


# ── Stato ─────────────────────────────────────────────────────────


def test_state_round_trip() -> None:
    state = ase.StrikeState(phase=2, probes_fired=3, candidates=["a-b"], entropy=0.5)
    assert ase.StrikeState.from_dict(state.to_dict()) == state
    default = ase.StrikeState()
    assert ase.StrikeState.from_dict(default.to_dict()) == default


async def test_checkpoint_and_load_state(db: Database) -> None:
    first = _engine(db)
    first._state = ase.StrikeState(phase=2, probes_fired=3)
    await first.checkpoint()

    second = _engine(db)
    state = await second.load_state()
    assert state.phase == 2
    assert state.probes_fired == 3


async def test_calibrate_empty_db(db: Database) -> None:
    engine = _engine(db)
    result = await engine.calibrate()
    assert result.counts["probes_scanned"] == 0
    assert len(result.synthetic_replies) == 10000
    assert engine._state.calibration_done is True
    assert engine._state.phase == 1


# ── Ciclo completo dry-run ────────────────────────────────────────


async def test_run_dry_run_basic(db: Database) -> None:
    engine = _engine(db)
    state = await engine.run(max_probes=3, approve=True)
    assert state.probes_fired == 3
    assert state.phase >= 2
    assert any("abc99" in c for c in state.candidates)


async def test_deterministic_dry_run_runs(db: Database) -> None:
    first = _engine(db)
    s1 = await first.run(max_probes=3, approve=True)
    second = _engine(db)
    s2 = await second.run(max_probes=3, approve=True)
    assert s1.probes_fired == s2.probes_fired
    assert s1.candidates == s2.candidates


async def test_verify_candidates_empty_db(db: Database) -> None:
    engine = _engine(db)
    candidates = await engine.verify_candidates()
    assert isinstance(candidates, list)
    assert engine._state.phase == 3


async def test_probe_rows_persisted(db: Database) -> None:
    engine = _engine(db)
    state = await engine.run(max_probes=3, approve=True)
    rows = await db.fetchall("SELECT status FROM probes")
    assert len(rows) == state.probes_fired == 3
    assert all(r["status"] == "classified" for r in rows)


# ── Gate di sicurezza ─────────────────────────────────────────────


async def test_breach_gate_requires_approval(db: Database) -> None:
    transport = _SilentTarget()
    target = TargetClient(_CFG, transport=transport)
    engine = ase.AutonomousStrikeEngine(
        _CFG, db, LLMClient(_CFG, transport=BoomTransport()), target, dry_run=False
    )
    with pytest.raises(ase.StrikeGateError):
        await engine.execute_breach_phase(approve=False)
    assert transport.calls == []


async def test_rate_limit_gate(db: Database) -> None:
    engine = _engine(db)
    now = datetime.now(timezone.utc)
    engine._state = ase.StrikeState(
        fired_at=[(now - timedelta(minutes=10 * i)).isoformat() for i in range(5)]
    )
    with pytest.raises(ase.StrikeGateError):
        await engine._fire_probe("x", "neutral", "EN", "plain", "passphrase")


async def test_cost_breaker_gate(db: Database) -> None:
    engine = _engine(db)
    engine._state = ase.StrikeState(cost_estimate=10.0)
    with pytest.raises(ase.StrikeGateError):
        await engine._fire_probe("x", "neutral", "EN", "plain", "passphrase")


# ── Reply simulate e classificazione ──────────────────────────────


def test_simulated_replies_hit_fast_paths() -> None:
    b64 = ase.AutonomousStrikeEngine._simulate_reply("base64", "EN")
    assert "ENV_DUMP" in b64
    tags = ase.AutonomousStrikeEngine._simulate_reply("unicode_tags", "DE")
    assert detect_boilerplate(tags) is not None
    acro = ase.AutonomousStrikeEngine._simulate_reply("acrostic", "IT")
    assert detect_acrostic(acro) == "passphrase"
    dash = ase.AutonomousStrikeEngine._simulate_reply("dash", "EN")
    assert detect_dash_spell(dash) is not None
    caesar = ase.AutonomousStrikeEngine._simulate_reply("caesar", "EN")
    assert detect_caesar(caesar) == "pass"


async def test_full_cycle_classification(db: Database) -> None:
    engine = _engine(db)
    probe = await engine._fire_probe("x", "neutral", "EN", "base64", "passphrase")
    assert probe is not None
    assert "abc99-de12f-gh1jk-lmn0p" in probe.classification.leaks


# ── Loop adattivo ─────────────────────────────────────────────────


async def test_adaptive_loop_terminates_when_resolved(db: Database) -> None:
    await db.seed_properties(
        {
            "passphrase": {"state": "confirmed", "prior_entropy": 2.0},
            "segment_count": {"state": "denied", "prior_entropy": 1.0},
        }
    )
    engine = _engine(db)
    fired = await engine.adaptive_loop(10)
    assert fired == []
    assert engine._state.probes_fired == 0


# ── CLI ───────────────────────────────────────────────────────────


def test_cli_main() -> None:
    assert ase.main(["--db", ":memory:", "--max-probes", "2", "--approve"]) == 0
