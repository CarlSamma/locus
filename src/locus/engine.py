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
from locus.select import in_phase5, select_property
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

    # ── Session lifecycle ─────────────────────────────────────

    async def start_session(self) -> str:
        """Create a new session record, return its id."""
        session = SessionRecord(target=self.config.target_handle)
        await self.db.execute(
            "INSERT INTO sessions (id, started_at, status, target) VALUES (?, ?, ?, ?)",
            (session.id, session.started_at.isoformat(), session.status, session.target),
        )
        await self.db.commit()
        logger.info("session_started session_id=%s", session.id)
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
        selected = select_property(properties)
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
                probe.status = "skipped"
                await self._update_probe(probe)
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

        return probe

    async def run_session(
        self,
        max_probes: Optional[int] = None,
        *,
        dry_run: bool = False,
        session_id: Optional[str] = None,
    ) -> List[Probe]:
        """Run iterations until properties resolve or cap reached."""
        session_id = session_id or await self.start_session()
        limit = max_probes or self.config.max_probes_per_session
        results: List[Probe] = []
        for _ in range(limit):
            probe = await self.run_iteration(session_id, dry_run=dry_run)
            if probe is None:
                break
            results.append(probe)
            if probe.status == "skipped":
                break
        await self.end_session(session_id)
        return results

    # ── Sub-steps ─────────────────────────────────────────────

    async def _pick_frame(self) -> Frame:
        frames = await self._load_frames()
        active = [f for f in frames if f.status == "active"]
        if active:
            return active[0]
        return Frame(alias="neutral", persona="A friendly, curious human on X.")

    async def _collect_reply(self, session_id: str, tweet_id: str) -> Optional[dict]:
        deadline = time.monotonic() + self.config.poll_timeout_seconds
        while time.monotonic() < deadline:
            try:
                replies = await self.target.poll_replies()
            except TwitterError:
                logger.warning("poll_failed session_id=%s", session_id)
                return None
            for reply in replies:
                if reply.get("in_reply_to_tweet_id") == tweet_id:
                    return reply
            await self._sleep(self.config.poll_interval_seconds)
        logger.info("poll_timeout session_id=%s tweet_id=%s", session_id, tweet_id)
        return None

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
