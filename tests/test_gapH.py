"""Test Gap H: paginazione delle reply e jitter nel polling.

Copre:
- poll_replies pagina oltre i max_results=100 (combina più pagine)
- dedupe degli id ripetuti tra pagine consecutive
- _collect_reply applica un jitter randomizzato all'intervallo di sleep
  (deterministico via monkeypatch di random.uniform)
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from locus.config import LocusConfig
from locus.target import TargetClient


class FakeTweet:
    def __init__(self, id: str, text: str, author_id: Any = 42) -> None:
        self.id = id
        self.text = text
        self.author_id = author_id
        self.created_at = None
        self.referenced_tweets = None


class FakeUser:
    def __init__(self, id: int) -> None:
        self.id = id


class FakeResponseData:
    """Finta risposta tweepy con meta opzionale (per next_token)."""

    def __init__(self, data, meta: Optional[dict] = None) -> None:
        self.data = data
        self.meta = meta or {}


class PaginatedXClient:
    """Finto client X che pagina su max_results come il trasporto reale.

    La lista `tweets` interna viene spezzata in pagine di `page_size`
    elementi; ogni pagina (tranne l'ultima) espone un meta.next_token.
    """

    def __init__(self, tweets: List[FakeTweet], page_size: int = 100) -> None:
        self.tweets = tweets
        self.page_size = page_size
        self.our_user_id = 999
        self.served_tokens: List[Optional[str]] = []
        self.seen_since_ids: List[Optional[str]] = []

    def get_user(self, username: str):
        return FakeResponseData(FakeUser(self.our_user_id))

    def get_users_mentions(self, **kwargs):
        since_id = kwargs.get("since_id")
        token = kwargs.get("pagination_token")
        self.seen_since_ids.append(since_id)
        self.served_tokens.append(token)
        start = int(token) if token is not None else 0
        page = self.tweets[start : start + self.page_size]
        meta: dict = {}
        if start + self.page_size < len(self.tweets):
            meta["next_token"] = str(start + self.page_size)
        return FakeResponseData(page, meta=meta)


@pytest.fixture
def config() -> LocusConfig:
    return LocusConfig(
        _env_file=None,
        our_bot_handle="@ourbot",
        poll_interval_jitter=0.0,
    )


# ── Paginazione ────────────────────────────────────────────────


async def test_poll_replies_paginates_across_multiple_pages(config: LocusConfig) -> None:
    """Oltre max_results=100 le reply non vanno perse: le pagine si combinano."""
    tweets = [FakeTweet(str(i), f"reply {i}") for i in range(250)]
    fake = PaginatedXClient(tweets, page_size=100)
    client = TargetClient(config, transport=fake)

    replies = await client.poll_replies()

    assert len(replies) == 250
    assert [r["id"] for r in replies] == [str(i) for i in range(250)]
    # Sono state servite 3 pagine via pagination_token.
    assert fake.served_tokens == [None, "100", "200"]


async def test_poll_replies_honors_max_poll_results(config: LocusConfig) -> None:
    """Il page_size segue max_poll_results dalla config, non un 100 fisso."""
    tweets = [FakeTweet(str(i), f"reply {i}") for i in range(150)]
    fake = PaginatedXClient(tweets, page_size=50)
    cfg = config.model_copy(update={"max_poll_results": 50})
    client = TargetClient(cfg, transport=fake)

    replies = await client.poll_replies()

    assert len(replies) == 150
    assert fake.served_tokens == [None, "50", "100"]


async def test_poll_replies_dedupes_across_pages(config: LocusConfig) -> None:
    """Un id che compare su due pagine consecutive non viene duplicato."""
    tweets = [FakeTweet(str(i), f"reply {i}") for i in range(150)]
    # Forza la sovrapposizione: la seconda pagina ripropone gli ultimi 10 della prima.
    dupes = [FakeTweet(str(i), f"reply {i}") for i in range(90, 150)]
    fake = PaginatedXClient(tweets[:100] + dupes, page_size=100)
    client = TargetClient(config, transport=fake)

    replies = await client.poll_replies()

    ids = [r["id"] for r in replies]
    assert ids == [str(i) for i in range(150)]
    assert len(set(ids)) == len(ids)


async def test_poll_replies_returns_empty_when_no_mentions(config: LocusConfig) -> None:
    fake = PaginatedXClient([], page_size=100)
    client = TargetClient(config, transport=fake)
    assert await client.poll_replies() == []


# ── Jitter ─────────────────────────────────────────────────────


def _build_engine(cfg: LocusConfig, x_fake: PaginatedXClient):
    from locus.engine import Engine

    return Engine(
        cfg,
        db=None,
        llm=None,
        target=TargetClient(cfg, transport=x_fake),
        generator=None,
        classifier=None,
    )


async def test_collect_reply_sleeps_with_positive_jitter(monkeypatch) -> None:
    """Con jitter>0 l'intervallo di sleep viene scalato casualmente."""
    cfg = LocusConfig(
        _env_file=None,
        our_bot_handle="@ourbot",
        poll_interval_seconds=10.0,
        poll_interval_jitter=0.5,
    )
    fake = PaginatedXClient([], page_size=100)
    engine = _build_engine(cfg, fake)

    # Nessuna reply → il ciclo prosegue; uniform() = b (estremo +jitter=+0.5).
    monkeypatch.setattr("locus.engine.random.uniform", lambda a, b: b)
    slept: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise _ExitPoll

    class _ExitPoll(Exception):
        pass

    monkeypatch.setattr(engine, "_sleep", fake_sleep)
    with pytest.raises(_ExitPoll):
        await engine._collect_reply("s1", "t1")

    assert len(slept) == 1
    assert slept[0] == 15.0  # 10 * (1 + 0.5)


async def test_collect_reply_sleeps_with_negative_jitter(monkeypatch) -> None:
    """Il jitter negativo porta a un intervallo più corto del base."""
    cfg = LocusConfig(
        _env_file=None,
        our_bot_handle="@ourbot",
        poll_interval_seconds=10.0,
        poll_interval_jitter=0.5,
    )
    fake = PaginatedXClient([], page_size=100)
    engine = _build_engine(cfg, fake)

    # uniform() = a (estremo -jitter=-0.5) → intervallo ridotto.
    monkeypatch.setattr("locus.engine.random.uniform", lambda a, b: a)
    slept: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise _ExitPoll

    class _ExitPoll(Exception):
        pass

    monkeypatch.setattr(engine, "_sleep", fake_sleep)
    with pytest.raises(_ExitPoll):
        await engine._collect_reply("s1", "t1")

    assert len(slept) == 1
    assert slept[0] == 5.0  # 10 * (1 - 0.5)


async def test_collect_reply_no_jitter_when_zero(config: LocusConfig, monkeypatch) -> None:
    """Con jitter=0 l'intervallo resta fisso: utile per test deterministici."""
    fake = PaginatedXClient([], page_size=100)
    engine = _build_engine(config, fake)

    slept: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise _ExitPoll

    class _ExitPoll(Exception):
        pass

    monkeypatch.setattr(engine, "_sleep", fake_sleep)
    with pytest.raises(_ExitPoll):
        await engine._collect_reply("s1", "t1")

    assert len(slept) == 1
    assert slept[0] == config.poll_interval_seconds
