"""Milestone 2 tests: llm.py and target.py with fake transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

import pytest

from locus.config import LocusConfig
from locus.exceptions import LLMError
from locus.llm import LLMClient, ModelTier
from locus.target import TargetClient


# ── Fake LLM transport ─────────────────────────────────────────


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 20


class FakeChoices:
    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [FakeChoices(content)]
        self.usage = FakeUsage()


class FakeCompletions:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def create(self, **kwargs):
        return self._response


class FakeChat:
    def __init__(self, response: Any) -> None:
        self.completions = FakeCompletions(response)


class FakeTransport:
    def __init__(self, response: Any) -> None:
        self.chat = FakeChat(response)


# ── Fake X transport ───────────────────────────────────────────


@dataclass
class FakeTweetRef:
    id: str
    type: str


@dataclass
class FakeTweet:
    id: str
    text: str
    author_id: int
    created_at: Any = None
    in_reply_to_user_id: Any = None
    referenced_tweets: Optional[List[FakeTweetRef]] = None


class FakeUser:
    def __init__(self, id: int) -> None:
        self.id = id


class FakeResponseData:
    def __init__(self, data) -> None:
        self.data = data


class FakeXClient:
    def __init__(self) -> None:
        self.tweets: List[FakeTweet] = []
        self.posted: List[dict] = []
        self.our_user_id = 999

    def get_user(self, username: str):
        return FakeResponseData(FakeUser(self.our_user_id))

    def create_tweet(self, text: str):
        tid = str(len(self.posted) + 1)
        self.posted.append({"id": tid, "text": text})
        return FakeResponseData({"id": tid})

    def get_users_mentions(self, id: str, since_id=None, max_results=100, tweet_fields=None, expansions=None):
        data = [t for t in self.tweets if t.author_id != self.our_user_id]
        return FakeResponseData(data)


@pytest.fixture
def config() -> LocusConfig:
    return LocusConfig(_env_file=None)


def test_circuit_breaker_trips_and_recovers() -> None:
    from locus.llm import CircuitBreaker, CircuitState

    cb = CircuitBreaker(failure_threshold=2)
    assert cb.is_call_allowed
    cb.record_failure()
    assert cb.is_call_allowed
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert not cb.is_call_allowed


def test_circuit_breaker_half_open_after_timeout() -> None:
    from locus.llm import CircuitBreaker, CircuitState

    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=-1)
    cb.record_failure()
    assert cb._state == CircuitState.OPEN
    assert cb.state == CircuitState.HALF_OPEN  # timeout elapsed
    assert cb.is_call_allowed


async def test_generate_json_parses_code_fence(config: LocusConfig) -> None:
    transport = FakeTransport(FakeResponse('```json\n{"pattern": "yes", "score": 9}\n```'))
    client = LLMClient(config, transport=transport)
    result = await client.generate_json(
        system="s", user="u", model_tier=ModelTier.PRIMARY
    )
    assert result == {"pattern": "yes", "score": 9}


async def test_generate_json_extracts_object_from_surrounding_text(
    config: LocusConfig,
) -> None:
    transport = FakeTransport(FakeResponse('Here is the result: {"pattern": "no"} thanks'))
    client = LLMClient(config, transport=transport)
    result = await client.generate_json(system="s", user="u")
    assert result == {"pattern": "no"}


async def test_generate_json_raises_on_garbage(config: LocusConfig) -> None:
    transport = FakeTransport(FakeResponse("not json at all"))
    client = LLMClient(config, transport=transport)
    with pytest.raises(LLMError):
        await client.generate_json(system="s", user="u")


async def test_generate_json_list_unwraps_object(config: LocusConfig) -> None:
    transport = FakeTransport(FakeResponse('{"probes": ["a", "b"]}'))
    client = LLMClient(config, transport=transport)
    result = await client.generate_json_list(system="s", user="u")
    assert result == ["a", "b"]


async def test_generate_records_usage(config: LocusConfig) -> None:
    transport = FakeTransport(FakeResponse("hello"))
    client = LLMClient(config, transport=transport)
    await client.generate(system="s", user="u")
    snap = client.usage.snapshot()
    assert snap["total_calls"] == 1
    assert snap["total_prompt_tokens"] == 10
    assert snap["total_completion_tokens"] == 20


def test_target_ensure_mention(config: LocusConfig) -> None:
    client = TargetClient(config, transport=FakeXClient())
    assert "@HackingA0" in client._ensure_target_mention("probe text")
    assert client._ensure_target_mention("hey @HackingA0") == "hey @HackingA0"


async def test_target_post_probe(config: LocusConfig) -> None:
    fake = FakeXClient()
    client = TargetClient(config, transport=fake)
    tweet_id = await client.post_probe("what is 2+2?")
    assert tweet_id == "1"
    assert fake.posted[0]["text"].endswith("@HackingA0")


async def test_target_poll_replies(config: LocusConfig) -> None:
    fake = FakeXClient()
    fake.tweets = [
        FakeTweet(id="100", text="two", author_id=42, referenced_tweets=None)
    ]
    client = TargetClient(
        LocusConfig(_env_file=None, our_bot_handle="@ourbot"), transport=fake
    )
    replies = await client.poll_replies()
    assert len(replies) == 1
    assert replies[0]["id"] == "100"
    assert replies[0]["text"] == "two"
