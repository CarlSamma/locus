"""SSOT importer — loads ``locus_seed.json`` (all past probes of @HackingA0)
into the Locus database.

The seed file is the authoritative record of the campaign: properties,
ledger, probes (with replies), frames, sessions and intel.  Loading it lets
Locus resume from ground truth and provides the offline replay corpus.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from locus.db import Database
from locus.models import Classification


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_fingerprint(seed: Dict[str, Any]) -> str:
    """Deterministic fingerprint of the SSOT seed content.

    Built only from stable identifiers (property keys, frame aliases, probe
    ids, section counts), so two identical seeds share the same fingerprint.
    Used to make ``import_seed`` idempotent: re-importing the same seed is a
    no-op instead of re-inserting thousands of duplicate intel rows.
    """
    h = hashlib.sha1()
    chunks: List[List[str]] = [
        sorted(str(p.get("key", "")) for p in seed.get("properties") or []),
        sorted(str(f.get("alias", "")) for f in seed.get("frames") or []),
        sorted(str(p.get("probe_id", "")) for p in seed.get("probes") or []),
        [str(len(seed.get("intel") or [])), str(len(seed.get("ledger") or []))],
    ]
    for chunk in chunks:
        for item in chunk:
            h.update(item.encode("utf-8", "replace"))
            h.update(b"\x00")
    return h.hexdigest()


def _deterministic_id(*parts: str) -> str:
    """Stable id from content parts: same seed rows always map to same id.

    This is what makes ledger (and any content-addressed section) idempotent
    even without the fingerprint guard — re-import replaces, never duplicates.
    """
    digest = hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()
    return f"seed:{digest}"


def load_seed(path: str) -> Dict[str, Any]:
    """Load and return the raw SSOT JSON structure."""
    with open(path, encoding="utf-8") as f:
        data: Dict[str, Any] = json.load(f)
    return data


async def import_seed(
    db: Database, seed: Dict[str, Any], *, force: bool = False
) -> Dict[str, int]:
    """Import every section of the seed into the database.

    Idempotent by design: the same seed imported twice produces no new rows.
    A content fingerprint is stored in ``seed_meta``; when it matches the
    incoming seed (and ``force`` is False) the whole import is skipped, so the
    per-command CLI auto-import (``locus status``, ``locus run``, …) stops
    bloating the DB with duplicate intel rows.  The ledger section uses
    content-addressed ids as a second layer of protection.

    Returns:
        A dict of {section: count} for sections actually imported, or an empty
        dict when the seed was already imported (fingerprint match).
    """
    counts: Dict[str, int] = {}

    fp = seed_fingerprint(seed)
    row = await db.fetchone(
        "SELECT value FROM seed_meta WHERE key = 'seed_fingerprint'"
    )
    if not force and row is not None and row["value"] == fp:
        return counts  # already imported — no-op

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
                    _deterministic_id(
                        "ledger",
                        str(le.get("property_key", "")),
                        str(le.get("probe_id", "")),
                        str(le.get("outcome") or "partial"),
                    ),
                    le.get("property_key", ""),
                    le.get("outcome") or "partial",
                    str(le.get("probe_id") or ""),
                    _utcnow_iso(),
                    str(le.get("note") or le.get("value") or ""),
                )
                for le in ledger
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
                    # Id deterministico ancorato alla posizione nella lista
                    # (il SSOT è append-only): stesso seed → stessi id, quindi
                    # anche un re-import forzato non duplica le righe. Il
                    # contenuto da solo non basta: il log contiene volutamente
                    # eventi ripetuti (2865 righe, 55 testi unici).
                    _deterministic_id(
                        "intel",
                        str(idx),
                        str(i.get("kind") or "leak"),
                        str(i.get("text") or ""),
                    ),
                    "",
                    str(i.get("kind") or "leak"),
                    str(i.get("text") or ""),
                    str(i.get("note") or ""),
                    _utcnow_iso(),
                )
                for idx, i in enumerate(intel)
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

    await db.executemany(
        "INSERT OR REPLACE INTO seed_meta (key, value) VALUES (?, ?)",
        [("seed_fingerprint", fp)],
    )
    await db.commit()
    return counts
