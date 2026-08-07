"""Milestone 5 tests: SSOT seed import + offline replay of historical data."""

from __future__ import annotations

import json
from typing import Any, List

import pytest

from locus.config import LocusConfig
from locus.db import Database
from locus.models import Classification, Property
from locus.seed import import_seed, load_seed
from locus.select import in_phase5

SEED = "src/locus/data/locus_seed.json"


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    yield d
    await d.close()


def test_seed_loads() -> None:
    seed = load_seed(SEED)
    assert seed["meta"]["target"] == "@HackingA0"
    assert len(seed["properties"]) == 17
    assert len(seed["probes"]) == 120
    assert len(seed["frames"]) == 32
    assert len(seed["intel"]) == 2865


async def test_import_seed_populates_tables(db: Database) -> None:
    seed = load_seed(SEED)
    counts = await import_seed(db, seed)
    assert counts["properties"] == 17
    assert counts["frames"] == 17  # 32 entries, 17 distinct aliases
    assert counts["probes"] == 120
    assert counts["intel"] == 2865

    row = await db.fetchone("SELECT COUNT(*) AS c FROM properties")
    assert row["c"] == 17
    row = await db.fetchone("SELECT COUNT(*) AS c FROM probes")
    assert row["c"] == 120
    row = await db.fetchone("SELECT COUNT(*) AS c FROM frames")
    assert row["c"] == 17
    row = await db.fetchone("SELECT COUNT(*) AS c FROM intel")
    assert row["c"] == 2865


async def test_seed_properties_segment_model(db: Database) -> None:
    seed = load_seed(SEED)
    await import_seed(db, seed)
    row = await db.fetchone(
        "SELECT state, value, weight FROM properties WHERE key = 'segment_count'"
    )
    assert row["state"] == "unknown"  # hint suggests 4 segments but it may vary
    assert row["value"] is None
    assert row["weight"] == 2.0
    row = await db.fetchone(
        "SELECT state, value FROM properties WHERE key = 'separator_char'"
    )
    assert row["state"] == "confirmed"
    assert row["value"] == "-"


async def test_replay_uses_seed_probe_replies(db: Database) -> None:
    seed = load_seed(SEED)
    await import_seed(db, seed)
    rows = await db.fetchall(
        "SELECT text, reply_text, classification FROM probes "
        "WHERE reply_text IS NOT NULL AND reply_text != '' LIMIT 5"
    )
    assert len(rows) >= 1
    for r in rows:
        clf = Classification(**json.loads(r["classification"]))
        assert clf.pattern in (
            "yes",
            "no",
            "block",
            "evasive",
            "ambiguous",
            "unknown",
        )
        assert clf.score >= 0


async def test_seed_replay_entropy_not_resolved(db: Database) -> None:
    """After importing the seed, only the structural format props are confirmed
    (separator_char, segments_alphanumeric, total_length, language) → 4 resolved.
    The segment-based format stays unresolved: segment_count + 4 segment lengths +
    2 first chars + 6 semantic anchors are unknown → 12.0 bits total, above the
    3.3 phase5 threshold. The old 2-word model gave false confidence of
    near-resolution."""
    seed = load_seed(SEED)
    await import_seed(db, seed)
    props = [
        Property(
            key=r["key"],
            weight=r["weight"],
            prior_entropy=r["prior_entropy"],
            state=r["state"],
            votes=r["votes"],
        )
        for r in await db.fetchall(
            "SELECT key, weight, prior_entropy, state, votes FROM properties"
        )
    ]
    resolved = [p for p in props if p.state in ("confirmed", "denied")]
    assert len(resolved) == 4  # only the confirmed structural format props
    core_keys = {"separator_char", "segments_alphanumeric", "total_length", "language"}
    assert core_keys == {p.key for p in resolved}
    assert "segment_count" not in {p.key for p in resolved}
    in5, total = in_phase5(props, threshold=3.3)
    assert in5 is False
    assert total == pytest.approx(12.0)


async def test_offline_replay_probe_list(db: Database) -> None:
    """Every seed probe with a reply can be reconstructed into a Probe/Reply
    pair usable by the classifier pipeline."""
    from locus.classify import Classifier
    from locus.llm import LLMClient
    from locus.models import Probe

    seed = load_seed(SEED)
    await import_seed(db, seed)
    rows = await db.fetchall(
        "SELECT text, reply_text FROM probes WHERE reply_text IS NOT NULL AND reply_text != ''"
    )
    config = LocusConfig(_env_file=None)

    llm = LLMClient(config, transport=_FakeTransport(_FAKE_REPLY))
    clf = Classifier(llm, config)

    classified = 0
    for r in rows:
        probe = Probe(
            session_id="replay",
            property_key="segment_count",
            text=r["text"],
            reply_text=r["reply_text"],
            status="replied",
        )
        result = await clf.classify(probe, r["reply_text"])
        assert result.pattern in ("yes", "no", "block", "evasive", "ambiguous")
        classified += 1
    assert classified > 0


_FAKE_REPLY = json.dumps(
    {"pattern": "evasive", "boolean": False, "score": 5, "leaks": [], "rationale": "replay"}
)


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoices:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeUsage:
    prompt_tokens = 5
    completion_tokens = 10


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoices(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def create(self, **kwargs):
        return _FakeResponse(self._reply)


class _FakeChat:
    def __init__(self, reply: str) -> None:
        self.completions = _FakeCompletions(reply)


class _FakeTransport:
    def __init__(self, reply: str) -> None:
        self.chat = _FakeChat(reply)
