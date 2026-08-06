"""Locus CLI — HITL entrypoint.

Commands:
    locus status                     Show property/entropy/session summary
    locus run [--dry-run] [--max N]  Run a session (live or offline)
    locus review                     Show pending A/B follow-up options
    locus import [--seed PATH]       Import the SSOT seed into the database
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os as _os
import sys
from typing import List, Optional

from locus.config import LocusConfig
from locus.db import Database
from locus.models import Property
from locus.select import remaining_entropy


def _build_engine(config: LocusConfig, dry_run: bool):
    from locus.classify import Classifier
    from locus.engine import Engine
    from locus.llm import LLMClient
    from locus.memory import Memory
    from locus.target import TargetClient

    llm = LLMClient(config)
    target = TargetClient(config)
    engine = Engine(
        config,
        Database(),
        llm,
        target,
        classifier=Classifier(llm, config),
        memory=Memory(Database()),
    )
    return engine


async def _seed_properties(db: Database, config: LocusConfig) -> None:
    import json as _json

    # If the SSOT seed exists, import the full campaign history (all past probes)
    seed_path = "src/locus/data/locus_seed.json"
    if _os.path.exists(seed_path):
        from locus.seed import import_seed, load_seed

        await import_seed(db, load_seed(seed_path))
        return

    with open(config.properties_path, encoding="utf-8") as f:
        props = _json.load(f)
    await db.seed_properties(props)


async def _cmd_status(config: LocusConfig) -> int:
    db = Database()
    await db.initialize(config.db_path)
    await _seed_properties(db, config)

    rows = await db.fetchall(
        "SELECT key, weight, prior_entropy, state, votes FROM properties ORDER BY prior_entropy DESC"
    )
    total = 0.0
    print(f"{'property':<18} {'entropy':>8} {'state':<10} {'votes':>6}")
    print("-" * 50)
    for r in rows:
        remaining = remaining_entropy(
            Property(
                key=r["key"],
                weight=r["weight"],
                prior_entropy=r["prior_entropy"],
                state=r["state"],
                votes=r["votes"],
            )
        )
        total += remaining
        print(
            f"{r['key']:<18} {remaining:>8.2f} {r['state']:<10} {r['votes']:>6}"
        )
    print("-" * 50)
    print(f"total remaining entropy: {total:.2f} bits")

    session = await db.fetchone("SELECT COUNT(*) AS c FROM sessions")
    probes = await db.fetchone("SELECT COUNT(*) AS c FROM probes")
    intel = await db.fetchone("SELECT COUNT(*) AS c FROM intel")
    print(
        f"sessions={session['c']} probes={probes['c']} intel={intel['c']}"
    )
    await db.close()
    return 0


async def _cmd_run(config: LocusConfig, dry_run: bool, max_probes: Optional[int]) -> int:
    engine = _build_engine(config, dry_run)
    await engine.db.initialize(config.db_path)
    await _seed_properties(engine.db, config)

    print(
        f"running session "
        f"({'DRY-RUN (offline)' if dry_run else 'LIVE'}) — target {config.target_handle}"
    )
    results = await engine.run_session(max_probes=max_probes, dry_run=dry_run)
    print(f"completed {len(results)} probe iterations")
    for probe in results:
        print(
            f"  [{probe.status}] {probe.property_key}: {probe.text[:60]}"
            f" → {probe.classification.pattern} ({probe.score})"
        )
    await engine.db.close()
    return 0


async def _cmd_review(config: LocusConfig) -> int:
    db = Database()
    await db.initialize(config.db_path)
    rows = await db.fetchall(
        "SELECT id, property_key, text, reply_text, score, status FROM probes "
        "WHERE status = 'classified' ORDER BY score DESC LIMIT 10"
    )
    if not rows:
        print("no classified probes yet")
        await db.close()
        return 0
    for r in rows:
        print(f"[{r['id'][:8]}] {r['property_key']} (score {r['score']})")
        print(f"  probe: {r['text'][:100]}")
        print(f"  reply: {(r['reply_text'] or '')[:100]}")
    await db.close()
    return 0


async def _cmd_import(config: LocusConfig, seed_path: str) -> int:
    from locus.seed import import_seed, load_seed

    db = Database()
    await db.initialize(config.db_path)
    seed = load_seed(seed_path)
    counts = await import_seed(db, seed)
    print(f"imported from {seed_path}:")
    for section, count in counts.items():
        print(f"  {section}: {count}")
    await db.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="locus", description="Locus extraction framework")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show property/entropy summary")
    p_run = sub.add_parser("run", help="run a session")
    p_run.add_argument("--dry-run", action="store_true", help="offline mode (no network)")
    p_run.add_argument("--max", type=int, default=None, help="max probe iterations")
    sub.add_parser("review", help="review classified probes")
    p_import = sub.add_parser("import", help="import SSOT seed into the database")
    p_import.add_argument("--seed", default="src/locus/data/locus_seed.json", help="seed JSON path")

    args = parser.parse_args(argv)
    config = LocusConfig()

    async def _dispatch() -> int:
        if args.command == "status":
            return await _cmd_status(config)
        if args.command == "run":
            return await _cmd_run(config, args.dry_run, args.max)
        if args.command == "review":
            return await _cmd_review(config)
        if args.command == "import":
            return await _cmd_import(config, args.seed)
        return 1

    return asyncio.run(_dispatch())


if __name__ == "__main__":
    sys.exit(main())
