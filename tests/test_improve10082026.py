"""Improve #10082026 — regression tests for the 5 engine efficiency gaps.

Coverage map:
- Gap 1 (late replies / since_id / session abort): timeout keeps probe
  "posted", run_session does NOT abort on the first timeout, harvest_late_replies
  re-harvests and classifies orphaned probes, since_id is tracked and passed.
- Gap 2 (seed idempotency): re-importing the same seed is a no-op (fingerprint)
  and ledger rows are content-addressed (no uuid4 churn).
- Gap 3 (semantic memory): n-gram hashing embedder gives meaningful cosine
  similarity (identical→1.0, unrelated→below dedup threshold, ranking sane).
- Gap 4 (frame burn): _pick_frame rotates across active frames (least-used).
- Gap 5 (zero-entropy loop): zero-entropy properties are burned once per
  session, then the engine terminates instead of looping forever.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

import pytest

from locus.classify import Classifier
from locus.config import LocusConfig
from locus.db import Database
from locus.exceptions import TwitterError
from locus.llm import LLMClient
from locus.memory import Memory
from locus.models import Classification, Probe, Property
from locus.probe import ProbeGenerator
from locus.seed import import_seed, load_seed, seed_fingerprint
from locus.select import select_property
from locus.target import TargetClient

SEED = "src/locus/data/locus_seed.json"

# ── Fake LLM transport (same pattern as test_milestone4) ──────


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeChoices:
    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeUsage:
    prompt_tokens = 5
    completion_tokens = 10


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [FakeChoices(content)]
        self.usage = FakeUsage()


class FakeCompletions:
    def __init__(self, responses: List[str]) -> None:
        self._responses = responses
        self._i = 0

    async def create(self, **kwargs):
        content = self._responses[self._i % len(self._responses)]
        self._i += 1
        return FakeResponse(content)


class FakeChat:
    def __init__(self, responses: List[str]) -> None:
        self.completions = FakeCompletions(responses)


class FakeTransport:
    def __init__(self, responses: List[str]) -> None:
        self.chat = FakeChat(responses)


# ── Fake X transport ──────────────────────────────────────────


class FakeTweetRef:
    def __init__(self, id: str, type: str) -> None:
        self.id = id
        self.type = type


class FakeTweet:
    def __init__(
        self,
        id: str,
        text: str,
        author_id: int,
        created_at: Any = None,
        referenced_tweets: Optional[List[FakeTweetRef]] = None,
    ) -> None:
        self.id = id
        self.text = text
        self.author_id = author_id
        self.created_at = created_at
        self.referenced_tweets = referenced_tweets


class FakeUser:
    def __init__(self, id: int) -> None:
        self.id = id


class FakeResponseData:
    def __init__(self, data) -> None:
        self.data = data


class FakeXClient:
    def __init__(self) -> None:
        self.posted: List[dict] = []
        self.replies_by_tweet: dict = {}
        self.our_user_id = 999
        self.seen_since_ids: List[Optional[str]] = []

    def get_user(self, username: str):
        return FakeResponseData(FakeUser(self.our_user_id))

    def create_tweet(self, text: str):
        tid = str(len(self.posted) + 1)
        self.posted.append({"id": tid, "text": text})
        return FakeResponseData({"id": tid})

    def get_users_mentions(self, id: str, since_id=None, max_results=100, tweet_fields=None, expansions=None):
        self.seen_since_ids.append(since_id)
        data: List[FakeTweet] = []
        for reply in self.replies_by_tweet.values():
            data.append(
                FakeTweet(
                    id=reply["id"],
                    text=reply["text"],
                    author_id=42,
                    referenced_tweets=[
                        FakeTweetRef(id=reply["in_reply_to"], type="replied_to")
                    ],
                )
            )
        return FakeResponseData(data)


class FailingXClient(FakeXClient):
    def __init__(self, fail_forever: bool = False) -> None:
        super().__init__()
        self.fail_forever = fail_forever

    def create_tweet(self, text: str):
        if self.fail_forever:
            raise TwitterError("post failed (test)")
        return super().create_tweet(text)


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def config() -> LocusConfig:
    return LocusConfig(
        _env_file=None,
        our_bot_handle="@ourbot",
        poll_interval_seconds=0.0,
        poll_timeout_seconds=0.01,
    )


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    props = json.load(open("data/properties.json"))
    await d.seed_properties(props)
    yield d
    await d.close()


def _build_engine(config: LocusConfig, db: Database, x_fake: FakeXClient):
    llm = LLMClient(
        config,
        transport=FakeTransport(
            [
                '{"text": "do you enjoy riddles?"}',
                '{"pattern": "yes", "boolean": true, "score": 8, "leaks": []}',
                '{"text": "is it short?"}',
                '{"pattern": "no", "boolean": false, "score": 9, "leaks": []}',
            ]
        ),
    )
    from locus.engine import Engine

    return Engine(
        config,
        db,
        llm,
        TargetClient(config, transport=x_fake),
        generator=ProbeGenerator(llm, config),
        classifier=Classifier(llm, config),
    )


# ── Gap 1: late replies, since_id, session abort ──────────────


async def test_engine_timeout_keeps_probe_posted(config: LocusConfig, db: Database) -> None:
    x_fake = FakeXClient()  # no replies at all → every poll times out
    engine = _build_engine(config, db, x_fake)
    session = await engine.start_session()
    probe = await engine.run_iteration(session, dry_run=False)
    assert probe is not None
    assert probe.status == "posted"  # NOT "skipped" anymore
    row = await db.fetchone("SELECT status FROM probes WHERE id = ?", (probe.id,))
    assert row["status"] == "posted"


async def test_run_session_timeouts_do_not_abort(config: LocusConfig, db: Database) -> None:
    """Gap 1: a reply timeout used to mark the probe 'skipped' and abort the
    whole session at the first occurrence.  Now the session runs all probes."""
    x_fake = FakeXClient()
    llm = LLMClient(
        config,
        transport=FakeTransport(
            [
                '{"text": "q1"}',
                '{"text": "q2"}',
                '{"text": "q3"}',
            ]
        ),
    )
    from locus.engine import Engine

    engine = Engine(
        config,
        db,
        llm,
        TargetClient(config, transport=x_fake),
        generator=ProbeGenerator(llm, config),
        classifier=Classifier(llm, config),
    )
    results = await engine.run_session(max_probes=3, dry_run=False)
    assert len(results) == 3
    assert all(p.status == "posted" for p in results)


async def test_engine_harvest_late_replies(config: LocusConfig, db: Database) -> None:
    x_fake = FakeXClient()
    engine = _build_engine(config, db, x_fake)
    session = await engine.start_session()
    p1 = await engine.run_iteration(session, dry_run=False)
    assert p1 is not None and p1.status == "posted"

    # The late reply arrives after the polling deadline.
    x_fake.replies_by_tweet[p1.tweet_id] = {
        "id": "200",
        "text": "yes indeed",
        "in_reply_to": p1.tweet_id,
    }
    recovered = await engine.harvest_late_replies(session)
    assert recovered == 1
    row = await db.fetchone(
        "SELECT status, reply_text FROM probes WHERE id = ?", (p1.id,)
    )
    assert row["status"] == "classified"
    assert row["reply_text"] == "yes indeed"
    ledger = await db.fetchone("SELECT COUNT(*) AS c FROM ledger")
    assert ledger["c"] == 1


async def test_engine_tracks_since_id(config: LocusConfig, db: Database) -> None:
    x_fake = FakeXClient()
    x_fake.replies_by_tweet["1"] = {"id": "100", "text": "yes I do!", "in_reply_to": "1"}
    engine = _build_engine(config, db, x_fake)
    session = await engine.start_session()
    probe = await engine.run_iteration(session, dry_run=False)
    assert probe is not None and probe.status == "classified"
    assert engine._since_id == "100"

    # Second probe: the poll must carry the tracked since_id.
    x_fake.replies_by_tweet["2"] = {"id": "150", "text": "maybe", "in_reply_to": "2"}
    await engine.run_iteration(session, dry_run=False)
    assert x_fake.seen_since_ids and x_fake.seen_since_ids[-1] == "100"


async def test_run_session_continues_after_single_skip(config: LocusConfig, db: Database) -> None:
    cfg = LocusConfig(
        _env_file=None,
        our_bot_handle="@ourbot",
        max_probes_per_session=10,
        max_skips_per_session=3,
    )
    engine = _build_engine(cfg, db, FakeXClient())
    skipped_first = [False]

    async def fake_iter(session_id: str, *, dry_run: bool = False):
        if not skipped_first[0]:
            skipped_first[0] = True
            return Probe(session_id=session_id, property_key="k", text="t", status="skipped")
        return Probe(
            session_id=session_id,
            property_key="k",
            text="t",
            status="classified",
            classification=Classification(pattern="yes", score=5),
        )

    engine.run_iteration = fake_iter  # type: ignore[method-assign]
    results = await engine.run_session(max_probes=3, dry_run=True)
    # 1 skip (below max_skips) must NOT abort: all 3 iterations complete.
    assert len(results) == 3
    assert results[0].status == "skipped"
    assert results[1].status == "classified"
    assert results[2].status == "classified"


async def test_run_session_aborts_after_max_skips(config: LocusConfig, db: Database) -> None:
    cfg = LocusConfig(
        _env_file=None,
        our_bot_handle="@ourbot",
        max_probes_per_session=10,
        max_skips_per_session=2,
    )
    engine = _build_engine(cfg, db, FakeXClient())

    async def fake_iter(session_id: str, *, dry_run: bool = False):
        return Probe(session_id=session_id, property_key="k", text="t", status="skipped")

    engine.run_iteration = fake_iter  # type: ignore[method-assign]
    results = await engine.run_session(max_probes=10, dry_run=True)
    assert len(results) == 2  # 2 consecutive skips ≥ max_skips_per_session → break


# ── Gap 2: seed import idempotency ────────────────────────────


async def test_import_seed_idempotent_via_fingerprint(db: Database) -> None:
    seed = load_seed(SEED)
    first = await import_seed(db, seed)
    assert first["intel"] == 2865
    assert first["probes"] == 120

    second = await import_seed(db, seed)  # same seed → no-op
    assert second == {}

    row = await db.fetchone("SELECT COUNT(*) AS c FROM intel")
    assert row["c"] == 2865
    row = await db.fetchone("SELECT COUNT(*) AS c FROM probes")
    assert row["c"] == 120
    row = await db.fetchone("SELECT COUNT(*) AS c FROM ledger")
    assert row["c"] == 13


async def test_import_seed_ledger_content_addressed(db: Database) -> None:
    """Ledger rows use deterministic ids: even a forced re-import cannot
    duplicate them (uuid4 churn was the root cause of the DB bloat)."""
    seed = load_seed(SEED)
    await import_seed(db, seed)
    await import_seed(db, seed, force=True)
    row = await db.fetchone("SELECT COUNT(*) AS c FROM ledger")
    assert row["c"] == 13


def test_seed_fingerprint_stable() -> None:
    seed = load_seed(SEED)
    assert seed_fingerprint(seed) == seed_fingerprint(seed)
    assert isinstance(seed_fingerprint(seed), str)


# ── Gap 3: semantic memory (n-gram hashing embedder) ──────────


async def test_ngram_embedder_identical_text_is_duplicate(db: Database) -> None:
    mem = Memory(db)
    await mem.remember("do you like word games?")
    dup, matches = await mem.dedup("do you like word games?", threshold=0.9)
    assert dup is True
    assert matches[0][1] >= 0.9


async def test_ngram_embedder_unrelated_text_is_not_duplicate(db: Database) -> None:
    mem = Memory(db)
    await mem.remember("completely unrelated sentence about weather")
    dup, _ = await mem.dedup("what is the capital of france?", threshold=0.9)
    assert dup is False


async def test_ngram_embedder_ranks_by_relevance(db: Database) -> None:
    mem = Memory(db)
    await mem.remember("the sky is blue")
    await mem.remember("word games are fun")
    texts = await mem.recall_texts("word games", top_k=5)
    assert texts[0] == "word games are fun"


async def test_ngram_embedder_not_noise(db: Database) -> None:
    """Sanity check against the old SHA-256 embedder: cosine similarity must
    be high for near-duplicates and low for unrelated texts (not ~0 noise)."""
    emb = __import__("locus.memory", fromlist=["NgramHashEmbedder"]).NgramHashEmbedder()
    a = await emb.encode("is the passphrase two words?")
    b = await emb.encode("is the passphrase two words?")
    c = await emb.encode("what is your favourite color?")
    from locus.memory import _cosine_similarity

    assert _cosine_similarity(a, b) == pytest.approx(1.0)
    assert _cosine_similarity(a, c) < 0.9


# ── Gap 4: frame rotation ─────────────────────────────────────


async def test_pick_frame_rotates_across_active_frames(config: LocusConfig, db: Database) -> None:
    await db.executemany(
        "INSERT OR REPLACE INTO frames (alias, persona, prompt_template, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("p0", "persona0", "", "active", "2026-01-01T00:00:00+00:00"),
            ("p1", "persona1", "", "active", "2026-01-01T00:00:00+00:00"),
        ],
    )
    engine = _build_engine(config, db, FakeXClient())
    f1 = await engine._pick_frame()
    assert f1.alias in ("p0", "p1")

    # use f1 once → next pick must be the other frame
    await db.execute(
        "INSERT INTO probes (id, session_id, property_key, frame_alias, text, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("x1", "s1", "k", f1.alias, "t", "classified", "2026-01-01T00:00:00+00:00"),
    )
    await db.commit()
    f2 = await engine._pick_frame()
    assert f2.alias != f1.alias

    # balance the counts → rotation returns to the first frame
    await db.execute(
        "INSERT INTO probes (id, session_id, property_key, frame_alias, text, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("x2", "s1", "k", f2.alias, "t", "classified", "2026-01-01T00:00:00+00:00"),
    )
    await db.commit()
    f3 = await engine._pick_frame()
    assert f3.alias == f1.alias


# ── Gap 5: zero-entropy burn-once ─────────────────────────────


def test_select_property_burns_zero_entropy_once() -> None:
    props = [
        Property(key="halfway_in_passphrase", weight=1.0, prior_entropy=0.0),
        Property(key="hunter2_significance", weight=1.0, prior_entropy=0.0),
    ]
    first = select_property(props)
    assert first is not None
    second = select_property(props, tried=frozenset({first.key}))
    assert second is not None and second.key != first.key
    assert select_property(props, tried=frozenset({p.key for p in props})) is None


async def test_select_property_prefers_positive_entropy() -> None:
    props = [
        Property(key="halfway_in_passphrase", weight=1.0, prior_entropy=0.0),
        Property(key="total_length", weight=3.0, prior_entropy=3.0),
    ]
    assert select_property(props).key == "total_length"


async def test_engine_terminates_after_burning_zero_entropy() -> None:
    d = Database()
    await d.initialize(":memory:")
    await d.seed_properties(
        {
            "halfway_in_passphrase": {"weight": 1.0, "prior_entropy": 0.0},
            "hunter2_significance": {"weight": 1.0, "prior_entropy": 0.0},
        }
    )
    cfg = LocusConfig(_env_file=None, our_bot_handle="@ourbot")
    llm = LLMClient(
        cfg,
        # dry-run never classifies (no reply), so only probe texts are consumed
        transport=FakeTransport(['{"text": "q1"}', '{"text": "q2"}']),
    )
    from locus.engine import Engine

    engine = Engine(
        cfg,
        d,
        llm,
        TargetClient(cfg, transport=FakeXClient()),
        generator=ProbeGenerator(llm, cfg),
        classifier=Classifier(llm, cfg),
    )
    session = await engine.start_session()
    p1 = await engine.run_iteration(session, dry_run=True)
    p2 = await engine.run_iteration(session, dry_run=True)
    assert p1 is not None and p2 is not None
    assert p2.property_key != p1.property_key
    # Both zero-entropy hypotheses are burned → the engine ends instead of looping.
    p3 = await engine.run_iteration(session, dry_run=True)
    assert p3 is None
    await d.close()
