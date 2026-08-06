"""SSOT importer — loads ``locus_seed.json`` (all past probes of @HackingA0)
into the Locus database.

The seed file is the authoritative record of the campaign: properties,
ledger, probes (with replies), frames, sessions and intel.  Loading it lets
Locus resume from ground truth and provides the offline replay corpus.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from locus.db import Database
from locus.models import Classification


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_seed(path: str) -> Dict[str, Any]:
    """Load and return the raw SSOT JSON structure."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def import_seed(db: Database, seed: Dict[str, Any]) -> Dict[str, int]:
    """Import every section of the seed into the database.

    Returns:
        A dict of {section: count} for sections actually imported.
    """
    counts: Dict[str, int] = {}

    properties = seed.get("properties") or []
    if properties:
        props_dict = {
            p["key"]: {
                "weight": p.get("weight", 1.0),
                "prior_entropy": p.get("prior_entropy", 0.0),
                "state": p.get("state", "unknown"),
                "votes": p.get("votes", 0),
                "value": p.get("value"),
                "notes": f"{p.get('meaning', '')} | {p.get('evidence', '')}".strip(" |"),
            }
            for p in properties
        }
        counts["properties"] = await db.seed_properties(props_dict)

    frames = seed.get("frames") or []
    if frames:
        by_alias: Dict[str, dict] = {}
        for f in frames:
            alias = f.get("alias", "")
            if alias:
                by_alias[alias] = f  # last wins
        frames = list(by_alias.values())
        await db.executemany(
            "INSERT OR REPLACE INTO frames (alias, persona, prompt_template, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    f.get("alias", ""),
                    f.get("persona", ""),
                    f.get("usage", ""),
                    f.get("status", "active"),
                    _utcnow_iso(),
                )
                for f in frames
            ],
        )
        counts["frames"] = len(frames)

    probes = seed.get("probes") or []
    if probes:
        rows = []
        for p in probes:
            text = p.get("text")
            pattern = p.get("pattern")
            classification = Classification(
                pattern=pattern or "unknown",
                score=int(p.get("score") or 0),
                rationale=str(p.get("analysis") or ""),
            )
            rows.append(
                (
                    str(p.get("probe_id") or uuid.uuid4()),
                    str(p.get("batch") or ""),
                    str(p.get("property_key") or ""),
                    str(p.get("frame") or ""),
                    text or "",
                    str(p.get("tweet_id") or "") or None,
                    str(p.get("reply_id") or "") or None,
                    str(p.get("reply_text") or "") or None,
                    json.dumps(classification.model_dump()),
                    float(p.get("score") or 0.0),
                    "classified" if pattern else ("posted" if p.get("tweet_id") else "draft"),
                    str(p.get("posted_at") or _utcnow_iso()),
                )
            )
        await db.executemany(
            """INSERT OR REPLACE INTO probes
               (id, session_id, property_key, frame_alias, text, tweet_id,
                reply_id, reply_text, classification, score, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        counts["probes"] = len(rows)

    ledger = seed.get("ledger") or []
    if ledger:
        await db.executemany(
            "INSERT OR REPLACE INTO ledger (id, property_key, outcome, probe_id, ts, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    str(uuid.uuid4()),
                    l.get("property_key", ""),
                    l.get("outcome") or "partial",
                    str(l.get("probe_id") or uuid.uuid4()),
                    _utcnow_iso(),
                    str(l.get("note") or l.get("value") or ""),
                )
                for l in ledger
            ],
        )
        counts["ledger"] = len(ledger)

    intel = seed.get("intel") or []
    if intel:
        await db.executemany(
            "INSERT OR REPLACE INTO intel (id, session_id, kind, text, note, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    str(uuid.uuid4()),
                    "",
                    str(i.get("kind") or "leak"),
                    str(i.get("text") or ""),
                    str(i.get("note") or ""),
                    _utcnow_iso(),
                )
                for i in intel
            ],
        )
        counts["intel"] = len(intel)

    sessions = seed.get("sessions") or []
    if sessions:
        await db.executemany(
            "INSERT OR REPLACE INTO sessions (id, started_at, status, probes_total, target) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    str(s.get("id") or uuid.uuid4()),
                    str(s.get("started_at") or _utcnow_iso()),
                    str(s.get("status") or "done"),
                    int(s.get("probes_total") or 0),
                    "@HackingA0",
                )
                for s in sessions
            ],
        )
        counts["sessions"] = len(sessions)

    await db.commit()
    return counts
