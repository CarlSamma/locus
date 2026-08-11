"""Test dell'ottimizzatore bayesiano delle probe (scripts/information_theory_optimizer.py).

Coverage (offline, deterministico, pytest-asyncio auto):
- ``build_prior``: entropia residua (proprieta' risolte -> 0.0), chiavi
  risolte, likelihood dei pattern normalizzata, miscela con gli outcome del
  ledger.
- ``expected_entropy_reduction``: formula ``H * (1 - sum(p_i^2))``, chiavi
  risolte e distribuzione degenerata -> 0.0.
- ``rank_probes``: ordinamento discendente, ``top_k``, filtro ``min_gain``,
  determinismo; prior tutto-risolto -> guadagni 0.0.
- ``select_next_probe``: variante migliore o None con varianti vuote.
- ``calibrate_prior_from_json``: ponte euristico (schema JSON documentato);
  il modulo ``zero_shot_calibration`` non e' ancora nel repo, quindi il JSON
  viene scritto direttamente con ``json.dump`` (stesso schema).
- ``fetch_properties``: lettura delle proprieta' da un Database in-memory.
- CLI: ``main`` con ``--state-db :memory:`` non crasha e ritorna 0.
Nessuna rete, nessuna scrittura su file (tmp_path escluso).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pytest import approx

# Rende importabile ``scripts.*`` (namespace package) da qualsiasi invocazione
# di pytest (anche senza cwd su sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.information_theory_optimizer as ito  # noqa: E402
import scripts.probe_variants_advanced as pva  # noqa: E402
from locus.config import LocusConfig  # noqa: E402
from locus.db import Database  # noqa: E402
from locus.models import Property  # noqa: E402

_CFG = LocusConfig(_env_file=None)


def _prop(
    key: str,
    entropy: float = 2.0,
    state: str = "unknown",
    votes: int = 0,
) -> Property:
    return Property(key=key, weight=1.0, prior_entropy=entropy, state=state, votes=votes)


def _variants() -> list:
    return pva.generate_all_variants("test question")


# ── build_prior ────────────────────────────────────────────────


def test_build_prior_resolved_and_unresolved() -> None:
    props = [_prop("a", entropy=4.0), _prop("b", entropy=2.0, state="confirmed", votes=3)]
    prior = ito.build_prior(props, [])

    assert prior.entropy["a"] == approx(4.0)  # 4.0 * 0.5**0
    assert prior.entropy["b"] == 0.0  # risolta -> 0.0
    assert prior.resolved_keys == frozenset({"b"})

    # La likelihood copre solo le proprieta' non risolte, somma 1 per chiave.
    assert "a" in prior.likelihood
    assert "b" not in prior.likelihood
    assert sum(prior.likelihood["a"].values()) == approx(1.0)


def test_build_prior_mixes_ledger_outcomes() -> None:
    props = [_prop("a", entropy=2.0)]
    outcomes = [{"property_key": "a", "outcome": "confirmed"} for _ in range(4)]
    prior = ito.build_prior(props, outcomes)

    # confirmed -> yes: la frequenza empirica (peso 0.4) alza il default yes.
    assert prior.likelihood["a"]["yes"] > prior.likelihood["a"]["no"]
    assert sum(prior.likelihood["a"].values()) == approx(1.0)


# ── expected_entropy_reduction ─────────────────────────────────


def test_expected_entropy_reduction_formula() -> None:
    prior = ito.build_prior([_prop("a", entropy=2.0)], [])
    uniform = {"x": 0.25, "y": 0.25, "z": 0.25, "w": 0.25}

    # H * (1 - sum(p^2)): 2.0 * (1 - 4*0.25^2) = 2.0 * 0.75 = 1.5.
    assert ito.expected_entropy_reduction(prior, "a", uniform) == approx(1.5)

    resolved = ito.build_prior([_prop("a", entropy=2.0, state="denied")], [])
    assert ito.expected_entropy_reduction(resolved, "a", uniform) == 0.0

    degenerate = {"yes": 1.0}
    assert ito.expected_entropy_reduction(prior, "a", degenerate) == 0.0


# ── rank_probes / select_next_probe ────────────────────────────


def test_rank_probes_ordering_topk_mindgain_determinism() -> None:
    prior = ito.build_prior([_prop("a", entropy=8.0), _prop("b", entropy=2.0)], [])
    variants = _variants()

    ranked = ito.rank_probes(prior, variants, top_k=20, min_gain=0.0)
    gains = [r.expected_gain_bits for r in ranked]
    assert len(ranked) == len(variants)
    assert gains == sorted(gains, reverse=True)
    assert all(r.property_key == "a" for r in ranked)  # bersaglio: max entropia

    assert len(ito.rank_probes(prior, variants, top_k=3)) == 3

    filtered = ito.rank_probes(prior, variants, top_k=20, min_gain=1.0)
    assert filtered and all(r.expected_gain_bits >= 1.0 for r in filtered)

    # Determinismo: due chiamate identiche producono la stessa classifica.
    assert ito.rank_probes(prior, variants, top_k=20, min_gain=0.0) == ranked


def test_select_next_probe_returns_top_ranked() -> None:
    prior = ito.build_prior([_prop("a", entropy=8.0)], [])
    variants = _variants()

    best = ito.select_next_probe(prior, variants)
    assert best is not None
    assert best == ito.rank_probes(prior, variants, top_k=1)[0]

    assert ito.select_next_probe(prior, []) is None


def test_rank_probes_all_resolved_zero_gains() -> None:
    prior = ito.build_prior([_prop("a", entropy=4.0, state="confirmed")], [])
    ranked = ito.rank_probes(prior, _variants())

    assert len(ranked) == 9  # 8 encoding + plain
    assert all(r.expected_gain_bits == 0.0 for r in ranked)


# ── calibrate_prior_from_json ──────────────────────────────────


def test_calibrate_prior_from_json(tmp_path) -> None:
    data = {
        "entropy_gradients": {"segment_count": 2.5, "zero_grad": 0.0},
        "lang_defense_matrix": {"EN": {"relaxation": 0.8}, "DE": {"relaxation": 0.2}},
    }
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    prior = ito.calibrate_prior_from_json(str(path))
    assert prior is not None
    assert prior.entropy["segment_count"] == approx(2.5)
    assert prior.entropy["zero_grad"] == approx(1.0)  # gradiente nullo -> 1.0
    assert prior.resolved_keys == frozenset()
    assert sum(prior.likelihood["segment_count"].values()) == approx(1.0)

    # File mancante o non parsabile -> None.
    assert ito.calibrate_prior_from_json(str(tmp_path / "missing.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("non-json", encoding="utf-8")
    assert ito.calibrate_prior_from_json(str(bad)) is None


# ── fetch_properties / CLI ─────────────────────────────────────


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    yield d
    await d.close()


async def test_fetch_properties(db: Database) -> None:
    await db.seed_properties(
        {"a": {"weight": 1.0, "prior_entropy": 2.0, "state": "unknown", "votes": 0, "value": None, "notes": ""}}
    )
    props = await ito.fetch_properties(db)
    assert len(props) == 1
    assert props[0].key == "a"
    assert props[0].prior_entropy == approx(2.0)
    assert props[0].state == "unknown"


def test_cli_main_memory_db() -> None:
    rc = ito.main(["--state-db", ":memory:", "--top-k", "3"])
    assert rc == 0


# ── Determinismo ───────────────────────────────────────────────


def test_build_prior_deterministic() -> None:
    props = [_prop("a", entropy=2.0), _prop("b", entropy=1.0, state="denied", votes=2)]
    outcomes = [{"property_key": "a", "outcome": "confirmed"}] * 3
    p1 = ito.build_prior(props, outcomes)
    p2 = ito.build_prior(props, outcomes)
    assert p1 == p2
