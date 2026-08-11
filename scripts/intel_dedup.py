"""Deduplicazione della tabella ``intel`` (Gap E).

Il DB live contiene ~34k righe di intel con solo 55 testi unici (97% rumore
storico, gran parte ``psychometrics``). Il seed importa ogni riga con
``uuid.uuid4()`` come id (seed.py:~194) percio' testi identici finiscono in
righe diverse: stesso contenuto, id diversi.

Questo script collassa le righe duplicate per contenuto usando chiavi
content-addressed (``intel:<sha1(kind|text)>``):

- raggruppa per ``(kind, text)`` (**chiave di contenuto**);
- tiene **una** riga rappresentativa per gruppo e le assegna un id deterministico
  content-addressed (se l'id e' gia' content-addressed, lo lascia invariato);
- **fonde** i ``note`` dei duplicati e **elimina** le righe duplicate in eccesso;
- e' **idempotente**: un secondo run non trova gruppi con piu' di una riga ed e'
  quindi un no-op, quindi va' eseguito piu' volte senza effetti collaterali.

E' inoltre possibile escludere del tutto un ''kind'' rumore con ``--kind-filter``
(es. ``--kind-filter psychometrics``) per rimuoverne tutte le righe, come misura
di cleanup oltre alla semplice dedup.

Lo script si rifiuta di toccare il database live ``data/locus.db`` a meno che non
venga passato esplicitamente ``--force``; il default e' la copia di lavoro
``data/locus.scratch-gaps.db``. Operazione offline: solo sqlite, nessun network.

Uso (offline):
    python scripts/intel_dedup.py --dry-run --report   # solo report, nessuna scrittura
    python scripts/intel_dedup.py --report             # report prima/dopo + dedup
    python scripts/intel_dedup.py --kind-filter psychometrics  # cleanup rumore
    python scripts/intel_dedup.py --db data/locus.db --force   # solo esplicito
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

#: Colonna ``kind`` considerata rumore storico (contata nel report di default).
NOISE_KIND = "psychometrics"
#: Prefisso delle chiavi content-addressed.
ID_PREFIX = "intel:"
#: Path live (normalizzato) che richiede ``--force``.
LIVE_DB = os.path.normcase(os.path.abspath("data/locus.db"))
#: Default del CLI: copia di lavoro, mai quella live.
DEFAULT_DB = os.path.join("data", "locus.scratch-gaps.db")


def content_id(kind: str, text: str) -> str:
    """Chiave content-addressed deterministica per ``(kind, text)``.

    Identici ``(kind, text)`` producono sempre lo stesso id, quindi lo stesso
    contenuto collassa sempre sulla stessa riga.
    """
    digest = hashlib.sha1(f"{kind}\x00{text}".encode("utf-8", "replace")).hexdigest()
    return f"{ID_PREFIX}{digest}"


def is_content_addressed(rid: str) -> bool:
    """True se l'id e' gia' una chiave content-addressed di questo script."""
    return rid.startswith(ID_PREFIX)


def intel_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Conta righe totali, gruppi unici e rumore ``kind`` per il report e la stima."""
    total = conn.execute("SELECT COUNT(*) FROM intel").fetchone()[0]
    unique = conn.execute("SELECT COUNT(DISTINCT kind || '\\x00' || text) FROM intel").fetchone()[0]
    noise = conn.execute("SELECT COUNT(*) FROM intel WHERE kind = ?", (NOISE_KIND,)).fetchone()[0]
    return {"total": total, "unique": unique, "noise": noise}


def top_duplicates(
    conn: sqlite3.Connection, limit: int = 10
) -> List[Tuple[str, str, int]]:
    """Restituisce i gruppi ``(kind, text)`` piu' duplicati, ordinati per conteggio."""
    rows = conn.execute(
        "SELECT kind, text, COUNT(*) AS n FROM intel "
        "GROUP BY kind, text HAVING n > 1 ORDER BY n DESC, kind LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def dedup_intel(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    kind_filter: Optional[str] = None,
) -> Dict[str, int]:
    """Deduplica le righe ``intel`` per contenuto. Idempotente.

    Argomenti:
        conn: connessione sqlite aperta sulla tabella da deduplicare.
        dry_run: se True non scrive nulla, si limita a calcolare le statistiche.
        kind_filter: se valorizzato, elimina tutte le righe con quel ``kind``
            (misura di cleanup del rumore oltre alla dedup).

    Restituisce un dict di statistiche: ``total``, ``unique``, ``dropped``
    (righe duplicate eliminate), ``removed_kind`` (righe eliminate dal filtro),
    ``renamed`` (rappresentative rinominate con chiave content-addressed).
    """
    stats: Dict[str, int] = {"total": 0, "unique": 0, "dropped": 0, "renamed": 0, "removed_kind": 0}

    rows = conn.execute(
        "SELECT id, session_id, kind, text, note FROM intel ORDER BY rowid"
    ).fetchall()
    stats["total"] = len(rows)

    # Eliminazione del rumore per kind (cleanup).
    if kind_filter:
        targets = [r[0] for r in rows if r[2] == kind_filter]
        stats["removed_kind"] = len(targets)
        if not dry_run and targets:
            conn.executemany("DELETE FROM intel WHERE id = ?", [(i,) for i in targets])
        rows = [r for r in rows if r[2] != kind_filter]

    # Raggruppamento per chiave di contenuto (kind, text).
    groups: Dict[Tuple[str, str], List[Tuple[str, str, str, str, str]]] = {}
    for rid, session_id, kind, text, note in rows:
        groups.setdefault((kind, text), []).append((rid, session_id, kind, text, note))

    stats["unique"] = len(groups)

    for (kind, text), members in groups.items():
        if len(members) <= 1:
            # Gruppo gia' pulito: eventualmente normalizza solo l'id se non e'
            # content-addressed (riga singola storica non ancora rinominata).
            rid, session_id, _, _, note = members[0]
            cid = content_id(kind, text)
            if not is_content_addressed(rid) and rid != cid and not dry_run:
                conn.execute("UPDATE intel SET id = ? WHERE id = ?", (cid, rid))
                stats["renamed"] += 1
            continue

        # Gruppo duplicato: scegli il rappresentante e fondi i note.
        rep = members[0]
        notes = [m[4] for m in members if m[4]]
        merged_note = " | ".join(dict.fromkeys(notes))  # unico, preserva l'ordine
        dupes = members[1:]

        cid = content_id(kind, text)
        target_id = rep[0]
        if not is_content_addressed(rep[0]):
            target_id = cid
            if not dry_run:
                conn.execute("UPDATE intel SET id = ? WHERE id = ?", (cid, rep[0]))
                stats["renamed"] += 1

        if not dry_run:
            # Aggiorna il rappresentante: id content-addressed e note fuse.
            conn.execute(
                "UPDATE intel SET note = ? WHERE id = ?", (merged_note, target_id)
            )
            # Elimina le righe duplicate in eccesso.
            conn.executemany("DELETE FROM intel WHERE id = ?", [(d[0],) for d in dupes])
        stats["dropped"] += len(dupes)

    if not dry_run:
        conn.commit()
    return stats


def guard_live(path_arg: str, force: bool) -> str:
    """Rifiuta il database live a meno che non sia stato richiesto esplicitamente.

    Restituisce il path normalizzato da usare per lo script.
    """
    resolved = os.path.normcase(os.path.abspath(path_arg))
    if resolved == LIVE_DB and not force:
        raise SystemExit(
            "RIFIUTATO: questo script non deve toccare il database live data/locus.db. "
            "Usa la copia di lavoro o passa --force se sei certo di volerlo fare."
        )
    return resolved


def report(
    path: str,
    before: Dict[str, int],
    after: Dict[str, int],
    dropped: int,
    renamed: int,
    removed_kind: int,
    kinds: List[Tuple[str, str, int]],
) -> None:
    """Stampa il report prima/dopo con i gruppi duplicati principali."""
    print(f"== Intel dedup ({path}) ==")
    print(f"  PRIMA  : total={before['total']} unique={before['unique']} "
          f"noise({NOISE_KIND})={before['noise']}")
    print(f"  DOPO   : total={after['total']} unique={after['unique']} "
          f"noise({NOISE_KIND})={after['noise']}")
    print(f"  Collassate: {dropped} righe duplicate | Rinominate content-addressed: "
          f"{renamed} | Rimosse per kind-filter: {removed_kind}")
    if kinds:
        print("  Top gruppi duplicati (kind | testo -> count):")
        for kind, text, n in kinds:
            snippet = text[:60].replace("\n", " ")
            print(f"    {kind:<14} {snippet!r} -> {n}")


def run(path_arg: str, dry_run: bool, kind_filter: Optional[str], force: bool) -> int:
    """Esegue la dedup su ``path_arg`` e stampa il report. Ritorna 0."""
    path = guard_live(path_arg, force)
    conn = sqlite3.connect(path)
    try:
        before = intel_counts(conn)
        stats = dedup_intel(conn, dry_run=dry_run, kind_filter=kind_filter)
        after = intel_counts(conn)
        report(
            path,
            before,
            after,
            dropped=stats["dropped"],
            renamed=stats["renamed"],
            removed_kind=stats["removed_kind"],
            kinds=top_duplicates(conn) if before["unique"] > 0 else [],
        )
        if dry_run:
            print("\n(dry-run: nessuna scrittura effettuata)")
        return 0
    finally:
        conn.close()


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Dedup content-addressed intel (Gap E).")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help="path del database da pulire (default: data/locus.scratch-gaps.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="solo report, nessuna scrittura sul database",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        dest="report_flag",
        help="stampa il report prima/dopo (default: True quando non dry-run)",
    )
    parser.add_argument(
        "--kind-filter",
        default=None,
        help="elimina tutte le righe con questo kind (es. psychometrics)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="consente esplicitamente l'uso del database live data/locus.db",
    )
    args = parser.parse_args(argv)
    return run(args.db, args.dry_run, args.kind_filter, args.force)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
