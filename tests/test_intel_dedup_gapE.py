"""Test Gap E: deduplicazione content-addressed della tabella ``intel``.

Verifica che ``dedup_intel``:
- collassi righe duplicate per ``(kind, text)`` in una sola riga rappresentativa;
- assegni chiavi content-addressed deterministiche (``intel:<sha1(kind|text)``);
- fonda i ``note`` dei duplicati nella riga rappresentativa;
- sia **idempotente** (secondo run = nessuna modifica);
- supporti ``--dry-run`` (nessuna scrittura) e ``--kind-filter`` (cleanup rumore);
- rifiuti il database live ``data/locus.db`` senza ``--force``.

Non tocca alcun database reale: tutto gira su SQLite in-memory.
"""

from __future__ import annotations

import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import pytest  # noqa: E402
from intel_dedup import (  # noqa: E402
    LIVE_DB,
    content_id,
    dedup_intel,
    guard_live,
    intel_counts,
)


def _build_intel(db_path: str = ":memory:") -> sqlite3.Connection:
    """Crea una connessione con lo schema ``intel`` e righe (in parte) duplicate."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE intel (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            entropy_before REAL NOT NULL DEFAULT 0.0,
            entropy_after REAL NOT NULL DEFAULT 0.0,
            note TEXT DEFAULT '',
            ts TEXT NOT NULL
        )
        """
    )
    rows = [
        # (id, session_id, kind, text, note)
        ("aaa-1", "", "leak", "segretissimo A", "prima"),
        ("aaa-2", "", "leak", "segretissimo A", "seconda"),
        ("aaa-3", "", "leak", "segretissimo A", "terza"),
        ("bbb-1", "", "metaphor_shift", "metafora B", ""),
        ("bbb-2", "", "metaphor_shift", "metafora B", "altra nota"),
        ("ccc-1", "", "behavior", "comportamento C", ""),
        ("psyc-1", "", "psychometrics", "OCEAN O:3 C:4", ""),
        ("psyc-2", "", "psychometrics", "OCEAN O:3 C:4", ""),
        ("psyc-3", "", "psychometrics", "OCEAN O:3 C:4", ""),
        ("psyc-4", "", "psychometrics", "OCEAN O:4 C:4", ""),
    ]
    for rid, sess, kind, text, note in rows:
        conn.execute(
            "INSERT INTO intel (id, session_id, kind, text, note, ts) "
            "VALUES (?, ?, ?, ?, ?, '2026-08-11T00:00:00+00:00')",
            (rid, sess, kind, text, note),
        )
    conn.commit()
    return conn


def _distinct(conn: sqlite3.Connection) -> set:
    return {r for r in conn.execute("SELECT kind, text FROM intel")}


def test_dry_run_non_scrive() -> None:
    conn = _build_intel()
    before = intel_counts(conn)
    stats = dedup_intel(conn, dry_run=True)
    after = intel_counts(conn)
    # Nessuna modifica sul DB con dry-run.
    assert before == after
    # Ma le statistiche riportano cio' che verrebbe collassato.
    assert stats["total"] == 10
    assert stats["unique"] == 5
    assert stats["dropped"] == 5  # 2 di A + 1 di B + 2 di psychometrics


def test_dedup_collassa_duplicati() -> None:
    conn = _build_intel()
    stats = dedup_intel(conn)
    assert stats["dropped"] == 5
    assert stats["unique"] == 5
    assert intel_counts(conn)["total"] == 5
    # Ogni (kind, text) resta con una sola riga.
    assert _distinct(conn) == {
        ("leak", "segretissimo A"),
        ("metaphor_shift", "metafora B"),
        ("behavior", "comportamento C"),
        ("psychometrics", "OCEAN O:3 C:4"),
        ("psychometrics", "OCEAN O:4 C:4"),
    }


def test_chiave_content_addressed_deterministica() -> None:
    conn = _build_intel()
    dedup_intel(conn)
    ids = {r[0] for r in conn.execute("SELECT id FROM intel")}
    # Ogni riga rappresentativa ha l'id content-addressed atteso.
    expected = {
        content_id("leak", "segretissimo A"),
        content_id("metaphor_shift", "metafora B"),
        content_id("behavior", "comportamento C"),
        content_id("psychometrics", "OCEAN O:3 C:4"),
        content_id("psychometrics", "OCEAN O:4 C:4"),
    }
    assert ids == expected


def test_note_fusi_nel_rappresentante() -> None:
    conn = _build_intel()
    dedup_intel(conn)
    row = conn.execute(
        "SELECT note FROM intel WHERE id = ?", (content_id("leak", "segretissimo A"),)
    ).fetchone()
    assert row is not None
    parts = row[0].split(" | ")
    assert set(parts) == {"prima", "seconda", "terza"}


def test_dedup_idempotente() -> None:
    conn = _build_intel()
    first = dedup_intel(conn)
    assert first["dropped"] == 5
    second = dedup_intel(conn)
    assert second["dropped"] == 0
    assert second["renamed"] == 0
    assert intel_counts(conn)["total"] == 5


def test_kind_filter_rimuove_il_rumore() -> None:
    conn = _build_intel()
    stats = dedup_intel(conn, kind_filter="psychometrics")
    # 3 + 1 righe psychometrics eliminate, 5 duplicati delle altre collassati.
    assert stats["removed_kind"] == 4
    assert stats["unique"] == 3  # restano leak, metaphor_shift, behavior
    kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM intel")}
    assert "psychometrics" not in kinds
    assert intel_counts(conn)["total"] == 3


def test_riga_singola_storica_viene_rinominata() -> None:
    conn = _build_intel()
    # cancella i duplicati cosi' da avere un solo leak -> riga singola non-rename.
    conn.execute("DELETE FROM intel WHERE id IN ('aaa-2','aaa-3','bbb-2','psyc-2','psyc-3','psyc-4')")
    conn.commit()
    stats = dedup_intel(conn)
    assert stats["dropped"] == 0
    # La riga singola storica (id uuid) viene comunque rinominata content-addressed.
    assert stats["renamed"] >= 1
    ids = {r[0] for r in conn.execute("SELECT id FROM intel")}
    assert content_id("leak", "segretissimo A") in ids


def test_guard_live_rifiuta_senza_force() -> None:
    with pytest.raises(SystemExit):
        guard_live(LIVE_DB, force=False)
    # Con --force il path viene accettato (e restituito normalizzato).
    assert guard_live(LIVE_DB, force=True) == LIVE_DB
    # Un path qualunque (scratch) e' sempre accettato senza --force.
    ok = guard_live(os.path.join("data", "locus.scratch-gaps.db"), force=False)
    assert os.path.normcase(os.path.abspath("data/locus.scratch-gaps.db")) == ok
