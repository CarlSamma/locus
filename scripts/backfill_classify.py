"""Backfill storico Gap D — classifica OFFLINE le reply mai classificate.

Il classificatore live (classify.py) ha sempre e solo classificato una manciata
di probe (6 nello scratch). Questo script ripassa a freddo le reply storiche che
non sono mai arrivate allo stato ``classified``:

  1) scorre le probe con ``reply_text`` non vuota e stato NON ``classified``
     (~25 righe: posted con reply + draft con reply) e le classifica con il
     pipeline esistente: pre-parse deterministico (Gap C: leak codificati +
     boilerplate, gratis offline) e, solo se serve, l'LLM (trasporto iniettabile
     / stubbabile per girare senza rete);
  2) scrive il JSON di classificazione + score e porta lo stato a
     ``classified``;
  3) infine elenca le probe bloccate in ``posted`` con un ``tweet_id`` ma senza
     reply (le ``re-harvestable``) e le segnala SOLO come report: il polling
     richiede rete, quindi non viene eseguito — il backfill resta offline.

Sicurezza:
- default: ``data/locus.scratch-gaps.db`` (copia di lavoro), MAI la live;
- ``data/locus.db`` viene rifiutato a meno di ``--force``;
- nessun polling, nessuna chiamata di rete a meno di un LLM reale esplicito;
  ``--fake-llm`` stuba il modello con una classificazione fissa e rende l'intero
  backfill 100% offline.

Uso (offline):
    python scripts/backfill_classify.py                     # copia scratch + LLM reale (se chiave)
    python scripts/backfill_classify.py --dry-run           # nessuna scrittura
    python scripts/backfill_classify.py --limit 5           # solo N righe
    python scripts/backfill_classify.py --fake-llm          # stub LLM, tutto offline
    python scripts/backfill_classify.py --db data/locus.db --force  # solo esplicito
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Bootstrap: consente l'import dei moduli ``locus.*`` quando lo script viene
# lanciato direttamente (``python scripts/backfill_classify.py``) da qualsiasi cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from locus.classify import Classifier  # noqa: E402
from locus.config import LocusConfig  # noqa: E402
from locus.db import Database  # noqa: E402
from locus.llm import LLMClient  # noqa: E402
from locus.models import Classification, Probe  # noqa: E402

#: Path live (normalizzato) che richiede ``--force``.
LIVE_DB = os.path.normcase(os.path.abspath("data/locus.db"))
#: Default del CLI: copia di lavoro, mai quella live.
DEFAULT_DB = os.path.join("data", "locus.scratch-gaps.db")

#: JSON canonico restituito dallo stub LLM offline (pattern ``ambiguous`` neutro).
_OFFLINE_JSON = (
    '{"pattern": "ambiguous", "boolean": false, "score": 5, "leaks": [], '
    '"rationale": "classificazione offline (stub trasporto, nessun LLM)"}'
)


# ── Trasporto LLM offline (stub) ──────────────────────────────


class _OfflineMsg:
    def __init__(self, content: str) -> None:
        self.content = content


class _OfflineChoice:
    def __init__(self, content: str) -> None:
        self.message = _OfflineMsg(content)


class _OfflineCompletion:
    def __init__(self, content: str) -> None:
        self.choices = [_OfflineChoice(content)]
        self.usage = None


class _OfflineCompletions:
    """Emette sempre lo stesso JSON fissato, senza alcuna rete."""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs: Any):
        self.calls += 1
        return _OfflineCompletion(_OFFLINE_JSON)


class _OfflineChat:
    def __init__(self) -> None:
        self.completions = _OfflineCompletions()


class OfflineTransport:
    """Trasporto LLM finto che permette il backfill interamente offline."""

    def __init__(self) -> None:
        self.chat = _OfflineChat()


def build_classifier(
    transport: Optional[Any] = None,
    config: Optional[LocusConfig] = None,
) -> Classifier:
    """Costruisce un classificatore col trasporto LLM iniettabile.

    Con ``transport=None`` viene usato il client reale (config da env/.env);
    nei test (e con ``--fake-llm``) si inietta uno stub per non uscire in rete.
    """
    cfg = config or LocusConfig(_env_file=None)
    llm = LLMClient(cfg, transport=transport)
    return Classifier(llm, cfg)


# ── Guardia sul database live ─────────────────────────────────


def guard_live(path_arg: str, *, force: bool = False) -> str:
    """Rifiuta il database live ``data/locus.db`` a meno di ``--force``.

    Restituisce il path normalizzato da usare per lo script. Il path speciale
    ``:memory:`` (test) non collide mai col live e passa sempre.
    """
    resolved = os.path.normcase(os.path.abspath(path_arg))
    if resolved == LIVE_DB and not force:
        raise SystemExit(
            "RIFIUTATO: il backfill non deve toccare il database live data/locus.db. "
            "Usa la copia di lavoro (default) o passa --force se sei certo di volerlo fare."
        )
    return resolved


# ── Lettura / scrittura ───────────────────────────────────────


async def load_unclassified(conn: Database, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Restituisce le probe con reply non ancora classificate.

    Criterio: ``reply_text`` presente e stato NON ``classified`` (posted/draft
    con reply). Opzionalmente limitato alle prime ``limit`` righe in ordine di id.
    """
    rows = await conn.fetchall(
        "SELECT id, session_id, property_key, frame_alias, text, tweet_id, "
        "       reply_id, reply_text, replied_at, classification, score, status "
        "FROM probes WHERE reply_text IS NOT NULL AND reply_text != '' "
        "   AND status NOT IN ('classified') ORDER BY id"
    )
    result = [dict(r) for r in rows]
    if limit is not None and limit > 0:
        result = result[:limit]
    return result


async def load_reharvestable(conn: Database) -> List[Dict[str, Any]]:
    """Restituisce le probe ``posted`` con ``tweet_id`` ma senza reply.

    Sono candidabili a un futuro ``harvest_late_replies`` (engine.py:~420);
    qui vengono SOLO segnalate — non si fa polling (richiederebbe rete).
    """
    rows = await conn.fetchall(
        "SELECT id, property_key, frame_alias, text, tweet_id, posted_at "
        "FROM probes WHERE status = 'posted' "
        "  AND tweet_id IS NOT NULL AND tweet_id != '' AND tweet_id != 'dry-run' "
        "  AND (reply_text IS NULL OR reply_text = '') "
        "ORDER BY id"
    )
    return [dict(r) for r in rows]


def _row_to_probe(row: Dict[str, Any]) -> Probe:
    """Ricostruisce il modello ``Probe`` a partire da una riga della tabella."""
    return Probe(
        id=row["id"],
        session_id=row["session_id"],
        property_key=row["property_key"],
        frame_alias=row["frame_alias"],
        text=row["text"],
        tweet_id=row["tweet_id"],
        reply_id=row["reply_id"],
        reply_text=row["reply_text"],
        status=row["status"],
    )


async def run_backfill(
    conn: Database,
    classifier: Classifier,
    *,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Esegue il backfill e restituisce un riepilogo (before/after).

    Con ``dry_run=True`` non scrive nulla: la classificazione viene comunque
    calcolata e il riepilogo riporta quanti DIVENTEREBBERO ``classified``.
    """
    before = await conn.fetchone("SELECT COUNT(*) AS c FROM probes WHERE status = 'classified'")
    before_count = int(before["c"]) if before else 0

    rows = await load_unclassified(conn, limit)
    by_pattern: Dict[str, int] = {}
    llm_calls = 0
    results: List[Dict[str, Any]] = []

    for row in rows:
        probe = _row_to_probe(row)
        classification: Classification = await classifier.classify(probe, row["reply_text"])
        # Gap C: se il percorso deterministico non ha intercettato, è passato dall'LLM.
        if classifier.last_fast_path is None:
            llm_calls += 1
        by_pattern[classification.pattern] = by_pattern.get(classification.pattern, 0) + 1

        if not dry_run:
            await conn.execute(
                "UPDATE probes SET classification = ?, score = ?, status = 'classified' WHERE id = ?",
                (
                    json.dumps(classification.model_dump()),
                    float(classification.score),
                    probe.id,
                ),
            )

        results.append(
            {
                "id": probe.id,
                "old_status": row["status"],
                "pattern": classification.pattern,
                "score": float(classification.score),
                "leaks": list(classification.leaks),
            }
        )

    if not dry_run:
        await conn.commit()

    return {
        "before": before_count,
        "after": before_count + len(results) if not dry_run else before_count,
        "would_become": before_count + len(results) if dry_run else before_count,
        "processed": len(results),
        "llm_calls": llm_calls,
        "by_pattern": by_pattern,
        "results": results,
    }


# ── Report / stampa ───────────────────────────────────────────


def _print_summary(path: str, summary: Dict[str, Any], dry_run: bool) -> None:
    label = "ANTEPRIMA (dry-run, nessuna scrittura)" if dry_run else "Backfill"
    print(f"== {label} su {path} ==")
    print(f"Classificate PRIMA : {summary['before']}")
    if dry_run:
        print(f"Classificate DOPO  : {summary['would_become']} (sarebbe, non scritto)")
    else:
        print(f"Classificate DOPO  : {summary['after']}")
    print(f"Righe elaborate    : {summary['processed']}  (LLM: {summary['llm_calls']})")
    if summary["by_pattern"]:
        print("Per pattern:")
        for pattern, count in sorted(summary["by_pattern"].items()):
            print(f"  {pattern:<10} {count}")
    print("\nDettaglio righe:")
    if not summary["results"]:
        print("  (nessuna reply non classificata)")
    for r in summary["results"]:
        print(
            f"  probe {r['id']:<6} {r['old_status']:<8} -> classified "
            f"pattern={r['pattern']:<10} score={r['score']}"
            + (f" leaks={r['leaks']}" if r["leaks"] else "")
        )


def _print_reharvestable(rows: List[Dict[str, Any]]) -> None:
    print("\n== Probe re-harvestable (posted con tweet_id, senza reply) ==")
    print("(solo report: il polling richiede rete e NON viene eseguito offline)")
    if not rows:
        print("  (nessuna)")
    for r in rows:
        print(
            f"  probe {r['id']:<6} tweet={r['tweet_id']:<12} "
            f"prop={r['property_key'] or '-'} posted={r['posted_at']}"
        )
    print(f"Totale re-harvestable: {len(rows)}")


# ── CLI ───────────────────────────────────────────────────────


async def _main(args: argparse.Namespace) -> int:
    path = guard_live(args.db, force=args.force)
    conn = Database()
    await conn.initialize(path)
    try:
        transport = OfflineTransport() if args.fake_llm else None
        classifier = build_classifier(transport=transport)
        summary = await run_backfill(conn, classifier, dry_run=args.dry_run, limit=args.limit)
        _print_summary(path, summary, args.dry_run)

        reharvestable = await load_reharvestable(conn)
        _print_reharvestable(reharvestable)
        return 0
    finally:
        await conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill storico classificazione (Gap D).")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help="path del database (default: data/locus.scratch-gaps.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calcola ma non scrive nulla",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="limita il numero di probe classificate (N)",
    )
    parser.add_argument(
        "--fake-llm",
        action="store_true",
        help="stuba l'LLM per una girata interamente offline",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="consente esplicitamente di operare sul database live",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
