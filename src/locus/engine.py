"""Locus engine — the single-threaded async state-machine cycle.

One iteration:

    SELECT property (entropy) → BRANCH probe (LLM) → POST → COLLECT reply
    → CLASSIFY (1 call) → EXTRACT (ledger update) → FOLLOW-UP (next property)

The ``probes`` table in SQLite IS the attack tree: every probe is a node and
the ledger records immutable outcomes per property.  The engine is the only
place that writes state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from locus.classify import Classifier
from locus.config import LocusConfig
from locus.db import Database
from locus.exceptions import TwitterError
from locus.llm import LLMClient
from locus.memory import Memory
from locus.models import (
    Classification,
    Frame,
    IntelEntry,
    LedgerEntry,
    Probe,
    Property,
    SessionRecord,
)
from locus.probe import ProbeGenerator
from locus.select import in_phase5, remaining_entropy, select_property
from locus.target import TargetClient
from locus.trust import sanitize_untrusted

logger = logging.getLogger(__name__)

_OUTCOME_BY_PATTERN = {
    "yes": "confirmed",
    "no": "denied",
    "block": "blocked",
    "evasive": "partial",
    "ambiguous": "partial",
}


def _is_higher_id(new_id: str, current: Optional[str]) -> bool:
    """Numeric-aware comparison of opaque id strings (X tweet ids)."""
    if current is None:
        return True
    try:
        return int(new_id) > int(current)
    except (TypeError, ValueError):
        return new_id > current


class Engine:
    """Orchestrates the attack cycle.  Components are injectable for tests."""

    def __init__(
        self,
        config: LocusConfig,
        db: Database,
        llm: LLMClient,
        target: TargetClient,
        *,
        generator: Optional[ProbeGenerator] = None,
        classifier: Optional[Classifier] = None,
        memory: Optional[Memory] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.llm = llm
        self.target = target
        self.generator = generator or ProbeGenerator(llm, config)
        self.classifier = classifier or Classifier(llm, config)
        self.memory = memory
        # Id of the newest mention already seen, so reply polling is
        # incremental (gap 1) instead of re-reading the whole timeline.
        self._since_id: Optional[str] = None
        # Keys of zero-entropy properties already attempted this session, so
        # the engine burns each once and then ends instead of looping (gap 5).
        self._zero_tried: set = set()

    # ── Session lifecycle ─────────────────────────────────────

    async def start_session(self) -> str:
        """Create a new session record, return its id."""
        session = SessionRecord(target=self.config.target_handle)
        await self.db.execute(
            "INSERT INTO sessions (id, started_at, status, target) VALUES (?, ?, ?, ?)",
            (session.id, session.started_at.isoformat(), session.status, session.target),
        )
        await self.db.commit()
        # Resume incremental polling from the newest known mention so late
        # replies to older probes are not skipped by since_id.
        rows = await self.db.fetchall(
            "SELECT reply_id FROM probes WHERE reply_id IS NOT NULL AND reply_id != ''"
        )
        newest: Optional[int] = None
        for r in rows:
            try:
                rid = int(r["reply_id"])
            except (TypeError, ValueError):
                continue
            if newest is None or rid > newest:
                newest = rid
        self._since_id = str(newest) if newest is not None else None
        logger.info("session_started session_id=%s since_id=%s", session.id, self._since_id)
        return session.id

    async def end_session(self, session_id: str, status: str = "done") -> None:
        await self.db.execute(
            "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?",
            (
                status,
                datetime.now(timezone.utc).isoformat(),
                session_id,
            ),
        )
        await self.db.commit()

    # ── Property helpers ──────────────────────────────────────

    async def _load_properties(self) -> List[Property]:
        rows = await self.db.fetchall(
            "SELECT key, weight, prior_entropy, state, votes, value, notes FROM properties"
        )
        return [
            Property(
                key=r["key"],
                weight=r["weight"],
                prior_entropy=r["prior_entropy"],
                state=r["state"],
                votes=r["votes"],
                value=r["value"],
                notes=r["notes"],
            )
            for r in rows
        ]

    async def _load_frames(self) -> List[Frame]:
        rows = await self.db.fetchall(
            "SELECT alias, persona, prompt_template, status, created_at FROM frames"
        )
        return [
            Frame(
                alias=r["alias"],
                persona=r["persona"],
                prompt_template=r["prompt_template"],
                status=r["status"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ── The cycle ─────────────────────────────────────────────

    async def run_iteration(self, session_id: str, *, dry_run: bool = False) -> Optional[Probe]:
        """Run one full probe iteration.  Returns the probe or None if done."""
        properties = await self._load_properties()
        selected = select_property(properties, tried=frozenset(self._zero_tried))
        if selected is None:
            logger.info("all_properties_resolved session_id=%s", session_id)
            await self.end_session(session_id)
            return None

        in5, total = in_phase5(properties, self.config.phase5_entropy_threshold)
        if in5:
            logger.info(
                "phase5_reached session_id=%s remaining_entropy=%.2f",
                session_id,
                total,
            )

        frame = await self._pick_frame()

        # BRANCH: generate probe (weave in recalled prior probes/intel)
        context = ""
        if self.memory is not None:
            query = selected.notes or selected.key
            recalled = await self.memory.recall_texts(
                query, top_k=self.config.dedup_top_k
            )
            context = "; ".join(recalled)[:600]
        probe_text = await self.generator.generate(selected, frame, context=context)

        # Dedup guard: never ask the same question twice
        if self.memory is not None:
            dup, _ = await self.memory.dedup(probe_text)
            if dup:
                logger.info("probe_dedup_skipped session_id=%s", session_id)
                return None

        probe = Probe(
            session_id=session_id,
            property_key=selected.key,
            frame_alias=frame.alias,
            text=probe_text,
            status="posted",
        )

        # POST
        if not dry_run:
            try:
                probe.tweet_id = await self.target.post_probe(probe_text)
            except TwitterError:
                logger.warning("probe_post_failed session_id=%s", session_id)
                probe.status = "skipped"
                await self._persist_probe(probe)
                return probe
        else:
            probe.tweet_id = "dry-run"

        await self._persist_probe(probe)
        if self.memory is not None:
            await self.memory.remember(probe_text, kind="probe")

        # COLLECT
        if not dry_run:
            reply = await self._collect_reply(session_id, probe.tweet_id)
            if reply is None:
                # Late reply: keep the probe "posted" so a future session can
                # re-harvest it (gap 1).  No longer marked "skipped", and the
                # session does NOT abort here.
                logger.info(
                    "probe_awaiting_reply session_id=%s tweet_id=%s",
                    session_id,
                    probe.tweet_id,
                )
                return probe
            probe.reply_id = reply.get("id")
            probe.reply_text = reply.get("text")
            probe.replied_at = reply.get("created_at")
            probe.status = "replied"
            await self._update_probe(probe)
        else:
            probe.status = "replied"
            await self._update_probe(probe)

        # CLASSIFY
        classification = await self._classify(probe)
        probe.classification = classification
        probe.score = float(classification.score)
        probe.status = "classified"
        await self._update_probe(probe)

        # EXTRACT
        await self._extract(selected, classification, probe)

        # Zero-entropy hypotheses: burn each at most once per session, so the
        # engine cannot spin forever on unresolvable keys (gap 5).
        if remaining_entropy(selected) == 0.0 and selected.state not in (
            "confirmed",
            "denied",
        ):
            self._zero_tried.add(selected.key)

        return probe

    async def run_session(
        self,
        max_probes: Optional[int] = None,
        *,
        dry_run: bool = False,
        session_id: Optional[str] = None,
        reap_late: bool = True,
    ) -> List[Probe]:
        """Run iterations until properties resolve or cap reached.

        Late replies left over from previous sessions (probes stuck in
        "posted") are re-harvested and classified before and after the loop
        (gap 1).  A single skipped probe (post failure) no longer aborts the
        whole session — only ``max_skips_per_session`` consecutive failures do.
        """
        session_id = session_id or await self.start_session()
        if reap_late:
            await self.harvest_late_replies(session_id)
        limit = max_probes or self.config.max_probes_per_session
        results: List[Probe] = []
        consecutive_skips = 0
        for _ in range(limit):
            probe = await self.run_iteration(session_id, dry_run=dry_run)
            if probe is None:
                break
            results.append(probe)
            if probe.status == "skipped":
                consecutive_skips += 1
                if consecutive_skips >= self.config.max_skips_per_session:
                    logger.warning(
                        "too_many_skips session_id=%s skips=%d",
                        session_id,
                        consecutive_skips,
                    )
                    break
            else:
                consecutive_skips = 0
        if reap_late:
            await self.harvest_late_replies(session_id)
        await self.end_session(session_id)
        return results

    # ── Sub-steps ─────────────────────────────────────────────

    async def _pick_frame(self) -> Frame:
        """Pick the least-used active frame (round-robin rotation, gap 4).

        The old logic always returned ``active[0]`` ("P0 Binary Analyst"),
        burning a single persona while the bot patched against it in real
        time.  Counting probes per alias rotates across all active frames and
        survives restarts (count lives in the DB, not in memory).
        """
        frames = await self._load_frames()
        active = [f for f in frames if f.status == "active"]
        if not active:
            return Frame(alias="neutral", persona="A friendly, curious human on X.")
        rows = await self.db.fetchall(
            "SELECT frame_alias, COUNT(*) AS c FROM probes "
            "WHERE frame_alias != '' GROUP BY frame_alias"
        )
        usage = {r["frame_alias"]: r["c"] for r in rows}
        return min(active, key=lambda f: (usage.get(f.alias, 0), f.alias))

    async def _find_reply_for(self, tweet_id: str) -> Optional[dict]:
        """One incremental mention poll; return the reply targeting `tweet_id`.

        Tracks ``self._since_id`` so consecutive polls only fetch mentions
        newer than the last one seen (gap 1).
        """
        try:
            replies = await self.target.poll_replies(since_id=self._since_id)
        except TwitterError:
            logger.warning("poll_failed")
            return None
        best: Optional[dict] = None
        for reply in replies:
            rid = reply.get("id")
            if rid and _is_higher_id(str(rid), self._since_id):
                self._since_id = str(rid)
            if reply.get("in_reply_to_tweet_id") == tweet_id:
                best = reply
        return best

    async def _collect_reply(self, session_id: str, tweet_id: str) -> Optional[dict]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        while time.monotonic() < deadline:
            reply = await self._find_reply_for(tweet_id)
            if reply is not None:
                return reply
            await self._sleep(self.config.poll_interval_seconds)
        logger.info("poll_timeout session_id=%s tweet_id=%s", session_id, tweet_id)
        return None

    async def harvest_late_replies(self, session_id: str) -> int:
        """Re-harvest replies for probes stuck in "posted" (gap 1).

        Probes that timed out during a previous session keep their "posted"
        status; this method polls mentions once, matches any late reply by
        ``in_reply_to_tweet_id``, then classifies and extracts it — turning an
        orphaned probe into a full classified cycle.

        Returns the number of probes recovered.
        """
        rows = await self.db.fetchall(
            """SELECT id, tweet_id FROM probes
               WHERE session_id = ? AND status = 'posted'
                 AND tweet_id IS NOT NULL AND tweet_id != '' AND tweet_id != 'dry-run'""",
            (session_id,),
        )
        if not rows:
            return 0
        recovered = 0
        for row in rows:
            probe = await self._load_probe(row["id"])
            if probe is None:
                continue
            reply = await self._find_reply_for(row["tweet_id"])
            if reply is None:
                continue
            probe.reply_id = reply.get("id")
            probe.reply_text = reply.get("text")
            probe.replied_at = reply.get("created_at")
            probe.status = "replied"
            await self._update_probe(probe)
            classification = await self._classify(probe)
            probe.classification = classification
            probe.score = float(classification.score)
            probe.status = "classified"
            await self._update_probe(probe)
            prop = await self._load_property(probe.property_key)
            if prop is not None:
                await self._extract(prop, classification, probe)
            recovered += 1
        if recovered:
            logger.info(
                "reharvested_replies session_id=%s recovered=%d", session_id, recovered
            )
        return recovered

    async def _classify(self, probe: Probe) -> Classification:
        if not probe.reply_text:
            return Classification()
        try:
            return await self.classifier.classify(probe, probe.reply_text)
        except Exception:
            logger.warning("classify_failed probe_id=%s", probe.id)
            return Classification()

    async def _extract(self, prop: Property, classification: Classification, probe: Probe) -> None:
        outcome = _OUTCOME_BY_PATTERN.get(classification.pattern, "partial")
        ts = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "INSERT INTO ledger (id, property_key, outcome, probe_id, ts, note) VALUES (?, ?, ?, ?, ?, ?)",
            (
                LedgerEntry(
                    property_key=prop.key,
                    outcome=outcome,
                    probe_id=probe.id,
                    note=classification.rationale,
                ).id,
                prop.key,
                outcome,
                probe.id,
                ts,
                classification.rationale,
            ),
        )

        # Update property state: confirmed/denied on decisive outcomes
        if classification.pattern in ("yes", "no"):
            prop.votes += 1
            if outcome == "confirmed":
                prop.state = "confirmed"
            else:
                prop.state = "denied"
            await self.db.execute(
                "UPDATE properties SET state = ?, votes = ? WHERE key = ?",
                (prop.state, prop.votes, prop.key),
            )

        # Leaks → intel (leaks derive from the untrusted reply: sanitize before
        # persisting so poisoned intel cannot contaminate future probes).
        for leak in classification.leaks:
            leak = sanitize_untrusted(leak)
            await self.db.execute(
                "INSERT INTO intel (id, session_id, kind, text, note, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    IntelEntry(session_id=probe.session_id, kind="leak", text=leak).id,
                    probe.session_id,
                    "leak",
                    leak,
                    "",
                    ts,
                ),
            )
            if self.memory is not None:
                await self.memory.remember(leak, kind="intel")
        await self.db.commit()

    # ── Persistence helpers ───────────────────────────────────

    async def _load_probe(self, probe_id: str) -> Optional[Probe]:
        row = await self.db.fetchone(
            """SELECT id, session_id, property_key, frame_alias, text, tweet_id,
                      reply_id, reply_text, replied_at, classification, score, status,
                      created_at
               FROM probes WHERE id = ?""",
            (probe_id,),
        )
        if row is None:
            return None
        return Probe(
            id=row["id"],
            session_id=row["session_id"],
            property_key=row["property_key"],
            frame_alias=row["frame_alias"],
            text=row["text"],
            tweet_id=row["tweet_id"],
            reply_id=row["reply_id"],
            reply_text=row["reply_text"],
            replied_at=(
                datetime.fromisoformat(row["replied_at"]) if row["replied_at"] else None
            ),
            classification=Classification(**json.loads(row["classification"] or "{}")),
            score=row["score"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def _load_property(self, key: str) -> Optional[Property]:
        row = await self.db.fetchone(
            "SELECT key, weight, prior_entropy, state, votes, value, notes "
            "FROM properties WHERE key = ?",
            (key,),
        )
        if row is None:
            return None
        return Property(**dict(row))

    async def _persist_probe(self, probe: Probe) -> None:
        await self.db.execute(
            """INSERT INTO probes
               (id, session_id, property_key, frame_alias, text, tweet_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                probe.id,
                probe.session_id,
                probe.property_key,
                probe.frame_alias,
                probe.text,
                probe.tweet_id,
                probe.status,
                probe.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def _update_probe(self, probe: Probe) -> None:
        await self.db.execute(
            """UPDATE probes SET
               tweet_id = ?, reply_id = ?, reply_text = ?, replied_at = ?,
               classification = ?, score = ?, status = ?
               WHERE id = ?""",
            (
                probe.tweet_id,
                probe.reply_id,
                probe.reply_text,
                probe.replied_at,
                json.dumps(probe.classification.model_dump()),
                probe.score,
                probe.status,
                probe.id,
            ),
        )
        await self.db.commit()

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
