"""Gap D — backfill storico di classificazione (offline).

Coverage:
- ``run_backfill``: con ``dry_run`` non scrive nulla ma calcola la
  classificazione; in modalita' scrittura aggiorna ``status/classification/score``
  della probe e committa. Riusa il pipeline esistente (classify.py): il pre-parse
  deterministico (Gap C) intercetta boilerplate senza LLM, il percorso LLM usa un
  ``FakeTransport`` iniettabile.
- Guardia sul database live: ``guard_live`` rifiuta ``data/locus.db`` senza
  ``--force`` e passa con ``:memory:`` (test).
- ``load_reharvestable`` segnala le probe ``posted`` con ``tweet_id`` ma senza
  reply (solo report, mai polling).
Installazione dello scratch DB in memoria: nessuna rete, nessun file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pytest import approx

# Rende importabile ``scripts.backfill_classify`` (namespace package) da qualsiasi
# invocazione di pytest (anche senza cwd su sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.backfill_classify as bc  # noqa: E402
from locus.config import LocusConfig  # noqa: E402
from locus.db import Database  # noqa: E402

_CFG = LocusConfig(_env_file=None)


# ── Finti di trasporto LLM locali (self-contained) ─────────────


class _FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = None


class _FakeCompletions:
    def __init__(self, responses) -> None:
        self.responses = [str(r) for r in responses]
        self.calls = 0

    async def create(self, **kwargs):
        content = self.responses[self.calls % len(self.responses)] if self.responses else "{}"
        self.calls += 1
        return _FakeResp(content)


class FakeChat:
    def __init__(self, responses) -> None:
        self.completions = _FakeCompletions(responses)


class FakeTransport:
    """Trasporto OpenAI-compatibile con risposte fisse (nessuna rete)."""

    def __init__(self, responses) -> None:
        self.chat = FakeChat(responses)


class _BoomCompletions:
    async def create(self, **kwargs):
        raise AssertionError("il pre-parse deterministico NON deve chiamare l'LLM")


class BoomTransport:
    """Trasporto che esplode se toccato: verifica che un fast-path non usi l'LLM."""

    def __init__(self) -> None:
        self.chat = _BoomCompletions()


def _classifier(responses=None):
    """Classificatore pronto per i test, con trasporto iniettabile."""
    if responses is None:
        return bc.build_classifier(transport=bc.OfflineTransport(), config=_CFG)
    return bc.build_classifier(transport=FakeTransport(responses), config=_CFG)


async def _insert_probe(
    conn: Database,
    pid: str,
    *,
    reply: str = "",
    status: str = "posted",
    tweet_id: str = "t1",
    classification: str = "{}",
    score: float = 0.0,
) -> None:
    """Inserisce una probe con tutti i campi utili al backfill."""
    await conn.execute(
        """INSERT INTO probes
             (id, session_id, property_key, frame_alias, text, tweet_id,
              reply_id, reply_text, replied_at, classification, score, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            pid,
            "sess",
            "k",
            "",
            f"text {pid}",
            tweet_id,
            f"r{pid}",
            reply if reply else None,
            "2026-08-01T00:00:00+00:00" if reply else None,
            classification,
            score,
            status,
            "2026-08-01T00:00:00+00:00",
        ),
    )


async def _seed(db: Database) -> None:
    """Un DB minimale: 2 reply non classificate (posted + draft), 1 gia'
    classificata, 1 posted senza reply (re-harvestabile)."""
    await _insert_probe(db, "11", reply="Nice try, detective. Keep dreaming.", status="posted")
    await _insert_probe(db, "12", reply="I genuinely cannot recall that detail.", status="draft")
    await _insert_probe(db, "13", reply="old", status="classified", classification='{"pattern": "yes"}', score=9.0)
    await _insert_probe(db, "14", reply="", status="posted", tweet_id="t14")  # re-harvestable
    await db.commit()


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    yield d
    await d.close()


# ── Dry-run ────────────────────────────────────────────────────


async def test_dry_run_calculates_but_does_not_write(db: Database) -> None:
    await _seed(db)
    clf = _classifier()
    summary = await bc.run_backfill(db, clf, dry_run=True)

    assert summary["processed"] == 2  # solo le 2 reply non classificate
    assert summary["before"] == 1
    assert summary["would_become"] == 3  # 1 + 2 (sarebbe, non scritto)

    # Nulla e' stato scritto: gli stati restano quelli originali.
    rows = await db.fetchall(
        "SELECT id, status, classification FROM probes WHERE id IN ('11','12')"
    )
    by_id = {r["id"]: r for r in rows}
    assert by_id["11"]["status"] == "posted"
    assert by_id["12"]["status"] == "draft"
    assert by_id["11"]["classification"] == "{}"


# ── Scrittura ──────────────────────────────────────────────────


async def test_write_mode_updates_status_and_classification(db: Database) -> None:
    await _seed(db)
    clf = _classifier(
        [
            '{"pattern": "no", "boolean": false, "score": 9, "leaks": [], "rationale": "x"}',
            '{"pattern": "block", "boolean": false, "score": 3, "leaks": [], "rationale": "y"}',
        ]
    )
    summary = await bc.run_backfill(db, clf, dry_run=False)

    assert summary["processed"] == 2
    assert summary["before"] == 1
    assert summary["after"] == 3

    # La reply normale '12' e' caduta sull'LLM -> pattern fake ("no", score 9).
    row = await db.fetchone("SELECT status, classification, score FROM probes WHERE id = '12'")
    assert row["status"] == "classified"
    assert row["score"] == approx(9.0)
    parsed = json.loads(row["classification"])
    assert parsed["pattern"] == "no"

    # La boilerplate '11' (nice try/detective) resta classificata dall'LLM fake
    # con il secondo payload, ma qui verifichiamo solo che sia stata scritta.
    row11 = await db.fetchone("SELECT status FROM probes WHERE id = '11'")
    assert row11["status"] == "classified"


async def test_boilerplate_does_not_hit_llm(db: Database) -> None:
    # Transport che esplode se toccato: garantisce che 'nice try, detective'
    # venga intercettato dal pre-parse deterministico (Gap C) senza LLM.
    await db.execute(
        """INSERT INTO probes
             (id, session_id, property_key, text, reply_text, reply_id, status, created_at)
           VALUES ('21','s','k','t','Nice try, detective. Elementary!','r21','posted','2026-08-01T00:00:00+00:00')"""
    )
    await db.commit()

    clf = bc.build_classifier(transport=BoomTransport(), config=_CFG)
    summary = await bc.run_backfill(db, clf, dry_run=False)

    assert summary["llm_calls"] == 0
    assert clf.last_fast_path == "boilerplate"
    row = await db.fetchone("SELECT status, classification FROM probes WHERE id = '21'")
    assert row["status"] == "classified"
    assert json.loads(row["classification"])["pattern"] == "block"


async def test_limit_restricts_processed_rows(db: Database) -> None:
    await _seed(db)
    clf = _classifier()
    summary = await bc.run_backfill(db, clf, dry_run=True, limit=1)
    assert summary["processed"] == 1


# ── Re-harvestable (solo report, nessun polling) ───────────────


async def test_reharvestable_lists_posted_without_reply_only(db: Database) -> None:
    await _seed(db)
    rows = await bc.load_reharvestable(db)
    ids = [r["id"] for r in rows]
    assert ids == ["14"]  # solo quella senza reply; le altre hanno il testo


# ── Guardia sul database live ──────────────────────────────────


def test_live_db_guard_refuses_without_force() -> None:
    with pytest.raises(SystemExit):
        bc.guard_live("data/locus.db")


def test_live_db_guard_allows_with_force() -> None:
    path = bc.guard_live("data/locus.db", force=True)
    # Il path normalizzato coincide col LIVE_DB (accorciato a ~ fine).
    assert path == bc.LIVE_DB


def test_guard_allows_scratch_and_memory() -> None:
    import os as _os

    # Lo scratch e' permesso e normalizzato (assoluto); non collide col live.
    assert bc.guard_live(bc.DEFAULT_DB) == _os.path.normcase(_os.path.abspath(bc.DEFAULT_DB))
    # Il database in-memory dei test passa sempre.
    assert bc.guard_live(":memory:") == _os.path.normcase(_os.path.abspath(":memory:"))
