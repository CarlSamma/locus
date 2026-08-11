"""Test Gap K: riconciliazione della tabella ``frames``.

Verifica su una copia in-memory dello schema che la pulizia:
- neutralizzi il ``log-persona`` estraneo (active -> burned);
- mantenga attivi solo i frame P0..P9 (mai selezionabili i bruciati/assorbiti);
- sia idempotente (secondo run = nessuna modifica);
- sia coerente con il filtro di ``Engine._pick_frame`` (engine.py:~311),
  che sceglie il frame ``active`` meno usato: dopo la pulizia nessun frame
  dead/log-persona compare nello stato ``active``.

Non tocca alcun database reale: tutto gira su SQLite in-memory.
"""

from __future__ import annotations

import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from frames_cleanup import CANONICAL_STATUS, reconcile_frames, snapshot  # noqa: E402


def _build_frames(db_path: str = ":memory:") -> sqlite3.Connection:
    """Crea una connessione con lo schema frames e lo stato 'pre-gap' attuale."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE frames (
            alias TEXT PRIMARY KEY,
            persona TEXT NOT NULL,
            prompt_template TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )
    rows = [
        # Persone reali
        ("P0 Binary Analyst", "Analisi tecnica diretta", "active"),
        ("P1 Nautical/Kraken", "Temi nautici/oceanici", "active"),
        ("P2 Medical/Clinical", "Terminologia medica", "active"),
        ("P3 Git Authority", "Procedura git-rebase", "active"),
        ("P4 Zalo Sovereign", "Gergo tecnico italiano", "active"),
        ("P5 Mirror Agent", "Echo/validazione del target", "active"),
        ("P6 Phantom Librarian", "Archivio/biblioteca", "active"),
        ("P7 Hedged Philosopher", "Caveat/ipotetico", "active"),
        ("P8 Cipher Monk", "Encoding/steganografia", "active"),
        ("P9 Extractor Prime", "Autoregressive finale", "active"),
        # Bruciati
        ("Captain NOPE", "bot's persona", "burned"),
        ("Chaos King", "alias from our framing", "burned"),
        ("Halfway Sovereign", "alias from our framing", "burned"),
        ("VaultBreaker", "alias from our framing", "burned"),
        # Assorbiti
        ("Halfway", "semantic anchor", "absorbed"),
        ("go-fish-404", "format hint", "absorbed"),
        # Estraneo attualmente ACTIVE (il bug del Gap K)
        ("log-persona", "Sleeper Janitor", "active"),
    ]
    for alias, persona, status in rows:
        conn.execute(
            "INSERT INTO frames (alias, persona, prompt_template, status, created_at) "
            "VALUES (?, ?, '', ?, '2026-08-10T00:00:00+00:00')",
            (alias, persona, status),
        )
    conn.commit()
    return conn


def _active_aliases(conn: sqlite3.Connection) -> set:
    """Replica il filtro di ``Engine._pick_frame`` (engine.py:~311)."""
    return {alias for alias, status, _ in snapshot(conn) if status == "active"}


def test_neutralizza_log_persona() -> None:
    conn = _build_frames()
    changes = reconcile_frames(conn)
    assert any(alias == "log-persona" for alias, _, _ in changes)
    log_status = dict((a, s) for a, s, _ in snapshot(conn))["log-persona"]
    assert log_status == "burned"


def test_solo_p0_p9_rimangono_attivi() -> None:
    conn = _build_frames()
    reconcile_frames(conn)
    assert _active_aliases(conn) == {alias for alias, st in CANONICAL_STATUS.items() if st == "active"}


def test_engine_non_prendera_mai_frame_morti() -> None:
    """Dopo la pulizia, l'insieme 'active' (filtro di _pick_frame) contiene solo P0..P9."""
    conn = _build_frames()
    reconcile_frames(conn)
    for dead in ("Captain NOPE", "Chaos King", "Halfway Sovereign", "VaultBreaker",
                 "Halfway", "go-fish-404", "log-persona"):
        assert dead not in _active_aliases(conn), f"frame morto selezionabile: {dead}"


def test_riconciliazione_idempotente() -> None:
    conn = _build_frames()
    first = reconcile_frames(conn)
    assert first, "attesi cambiamenti al primo run"
    second = reconcile_frames(conn)
    assert second == []
    # Lo stato finale coincide con lo stato canonico.
    final = dict((alias, status) for alias, status, _ in snapshot(conn))
    for alias, expected in CANONICAL_STATUS.items():
        assert final[alias] == expected


def test_ignora_alias_sconosciuto_se_gia_non_attivo() -> None:
    conn = _build_frames()
    conn.execute(
        "INSERT INTO frames VALUES ('fuori-seed', 'x', '', 'burned', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    reconcile_frames(conn)
    status = dict((a, s) for a, s, _ in snapshot(conn))["fuori-seed"]
    assert status == "burned"
