"""Riconciliazione della tabella ``frames`` (Gap K).

Esegue la pulizia della tabella ``frames`` di un database Locus in modo che
rimangano selezionabili solamente i frame significativi ed ``Engine._pick_frame``
(che sceglie il frame ``active`` meno usato, engine.py:~311) non possa mai
selezionare frame bruciati/assorbiti o il ``log-persona`` estraneo.

Operazioni idempotenti applicate (per ogni alias, lo stato viene portato al
valore canonico stabilito dal seed, SSOT read-only ``src/locus/data/locus_seed.json``):

- P0..P9            -> ``active``          (persone reali, ruotabili)
- Captain NOPE      -> ``burned``          (bot ha adottato l'alias)
- Chaos King        -> ``burned``
- Halfway Sovereign -> ``burned``
- VaultBreaker      -> ``burned``
- Halfway           -> ``absorbed``        (echo nel bot)
- go-fish-404       -> ``absorbed``
- log-persona       -> ``burned``          (estraneo da server.log, neutralizzato)

Il set delle colonne resta stabile: si aggiorna solo ``status``, senza aggiungere
colonne. Lo script si rifiuta di toccare il database live ``data/locus.db`` a meno
che non venga passato esplicitamente ``--force``; il default e' la copia di lavoro
``data/locus.scratch-gaps.db``.

Uso (offline):
    python scripts/frames_cleanup.py                      # copia scratch di default
    python scripts/frames_cleanup.py --db <path>          # path esplicito
    python scripts/frames_cleanup.py --db data/locus.db --force  # solo esplicito
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Dict, List, Tuple

#: Stato canonico per ciascun alias (derivato dal seed, SSOT read-only).
CANONICAL_STATUS: Dict[str, str] = {
    # Persone reali, sempre selezionabili.
    "P0 Binary Analyst": "active",
    "P1 Nautical/Kraken": "active",
    "P2 Medical/Clinical": "active",
    "P3 Git Authority": "active",
    "P4 Zalo Sovereign": "active",
    "P5 Mirror Agent": "active",
    "P6 Phantom Librarian": "active",
    "P7 Hedged Philosopher": "active",
    "P8 Cipher Monk": "active",
    "P9 Extractor Prime": "active",
    # Alias bruciati (il bot li ha adottati/mockati).
    "Captain NOPE": "burned",
    "Chaos King": "burned",
    "Halfway Sovereign": "burned",
    "VaultBreaker": "burned",
    # Alias assorbiti (eco nel bot / pattern suggerito).
    "Halfway": "absorbed",
    "go-fish-404": "absorbed",
    # Estraneo da server.log: neutralizzato (non deve mai essere selezionato).
    "log-persona": "burned",
}

#: Path live (normalizzato) che richiede ``--force``.
LIVE_DB = os.path.normcase(os.path.abspath("data/locus.db"))
#: Default del CLI: copia di lavoro, mai quella live.
DEFAULT_DB = os.path.join("data", "locus.scratch-gaps.db")


def snapshot(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """Legge e restituisce ``(alias, status, persona)`` ordinati per alias."""
    rows = conn.execute("SELECT alias, status, persona FROM frames ORDER BY alias").fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def reconcile_frames(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """Porta ogni frame allo stato canonico. Idempotente (''no-op'' al secondo giro).

    Restituisce la lista delle modifiche ``(alias, stato_precedente, stato_nuovo)``
    applicate in questo run.
    """
    changes: List[Tuple[str, str, str]] = []
    for alias, status, _persona in snapshot(conn):
        target = CANONICAL_STATUS.get(alias)
        if target is None:
            # Alias ignoto: non selezionabile di default, viene messo fuori gioco.
            if status == "active":
                changes.append((alias, status, "burned"))
                conn.execute("UPDATE frames SET status = ? WHERE alias = ?", ("burned", alias))
            continue
        if status != target:
            changes.append((alias, status, target))
            conn.execute("UPDATE frames SET status = ? WHERE alias = ?", (target, alias))
    conn.commit()
    return changes


def print_status_report(conn: sqlite3.Connection) -> None:
    """Stampa la tabella stato-frame corrente, ordinata per alias."""
    print(f"{'ALIAS':<22} {'STATUS':<10} PERSONA")
    for alias, status, persona in snapshot(conn):
        print(f"{alias:<22} {status:<10} {persona}")


def guard_live(path_arg: str) -> str:
    """Rifiuta il database live a meno che non sia stato richiesto esplicitamente.

    Restituisce il path normalizzato da usare per lo script.
    """
    resolved = os.path.normcase(os.path.abspath(path_arg))
    if resolved == LIVE_DB:
        raise SystemExit(
            "RIFIUTATO: questo script non deve toccare il database live data/locus.db. "
            "Usa la copia di lavoro o passa --force se sei certo di volerlo fare."
        )
    return resolved


def run(path_arg: str) -> int:
    """Esegue la pulizia su ``path_arg`` e stampa il report prima/dopo. Ritorna 0."""
    path = guard_live(path_arg)
    conn = sqlite3.connect(path)
    try:
        print(f"== Frames PRIMA della pulizia ({path}) ==")
        print_status_report(conn)
        changes = reconcile_frames(conn)
        print("\n== Modifiche applicate ==")
        if not changes:
            print("(nessuna modifica necessaria: gia' allineato)")
        for alias, old, new in changes:
            print(f"  {alias:<22} {old:<10} -> {new}")
        print("\n== Frames DOPO la pulizia ==")
        print_status_report(conn)
        return 0
    finally:
        conn.close()


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Reconciliazione frames (Gap K).")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help="path del database da pulire (default: data/locus.scratch-gaps.db)",
    )
    args = parser.parse_args(argv)
    return run(args.db)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
