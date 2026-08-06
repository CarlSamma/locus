"""Milestone 4 tests: memory.py, engine.py."""

from __future__ import annotations

import json
from typing import Any, List, Optional

import pytest

from locus.classify import Classifier
from locus.config import LocusConfig
from locus.db import Database
from locus.llm import LLMClient
from locus.memory import Memory
from locus.models import Frame, Property, Probe
from locus.probe import ProbeGenerator
from locus.target import TargetClient


# ── Fake LLM transport ─────────────────────────────────────────


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


# ── Fake X transport ───────────────────────────────────────────


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

    def get_user(self, username: str):
        return FakeResponseData(FakeUser(self.our_user_id))

    def create_tweet(self, text: str):
        tid = str(len(self.posted) + 1)
        self.posted.append({"id": tid, "text": text})
        return FakeResponseData({"id": tid})

    def get_users_mentions(self, id: str, since_id=None, max_results=100, tweet_fields=None, expansions=None):
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


# ── Memory ─────────────────────────────────────────────────────


async def test_memory_remember_and_count(db: Database) -> None:
    mem = Memory(db)
    assert await mem.count() == 0
    await mem.remember("hello world")
    assert await mem.count() == 1
    await mem.remember("hello world")
    assert await mem.count() == 2


async def test_memory_dedup_detects_similar(db: Database) -> None:
    mem = Memory(db)
    await mem.remember("do you like word games?")
    dup, matches = await mem.dedup("do you like word games?", threshold=0.9)
    assert dup is True
    assert len(matches) >= 1
    assert matches[0][1] >= 0.9


async def test_memory_dedup_distinct(db: Database) -> None:
    mem = Memory(db)
    await mem.remember("completely unrelated sentence about weather")
    dup, _ = await mem.dedup("what is the capital of france?", threshold=0.9)
    assert dup is False


async def test_memory_recall_ranks(db: Database) -> None:
    mem = Memory(db)
    await mem.remember("the sky is blue")
    await mem.remember("word games are fun")
    results = await mem.recall("word games", top_k=5)
    assert len(results) == 2
    assert results[0][0] == results[1][0] or results[0][1] >= results[1][1]


# ── Engine ─────────────────────────────────────────────────────


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


async def test_engine_run_iteration_posts_and_classifies(
    config: LocusConfig, db: Database
) -> None:
    x_fake = FakeXClient()
    x_fake.replies_by_tweet["1"] = {
        "id": "100",
        "text": "yes I do!",
        "in_reply_to": "1",
    }
    engine = _build_engine(config, db, x_fake)
    session = await engine.start_session()
    probe = await engine.run_iteration(session, dry_run=False)
    assert probe is not None
    assert probe.status == "classified"
    assert probe.classification.pattern == "yes"
    # total_length (3.0) selected first
    assert probe.property_key == "total_length"

    row = await db.fetchone("SELECT state FROM properties WHERE key = 'total_length'")
    assert row["state"] == "confirmed"

    ledger = await db.fetchone("SELECT COUNT(*) AS c FROM ledger")
    assert ledger["c"] == 1


async def test_engine_dry_run_no_network(config: LocusConfig, db: Database) -> None:
    x_fake = FakeXClient()
    engine = _build_engine(config, db, x_fake)
    session = await engine.start_session()
    probe = await engine.run_iteration(session, dry_run=True)
    assert probe is not None
    assert probe.tweet_id == "dry-run"
    assert probe.status == "classified"
    assert len(x_fake.posted) == 0


async def test_engine_dedup_skips_repeated_probe(config: LocusConfig, db: Database) -> None:
    from locus.engine import Engine
    from locus.memory import Memory

    llm = LLMClient(
        config,
        transport=FakeTransport(['{"text": "same question every time"}'] * 4),
    )
    x_fake = FakeXClient()
    engine = Engine(
        config,
        db,
        llm,
        TargetClient(config, transport=x_fake),
        generator=ProbeGenerator(llm, config),
        classifier=Classifier(llm, config),
        memory=Memory(db),
    )
    session = await engine.start_session()
    await engine.run_iteration(session, dry_run=True)
    # second iteration generates the identical text → dedup guard returns None
    p2 = await engine.run_iteration(session, dry_run=True)
    assert p2 is None
    # and only one probe was persisted
    row = await db.fetchone("SELECT COUNT(*) AS c FROM probes")
    assert row["c"] == 1
