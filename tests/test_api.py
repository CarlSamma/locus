"""API tests: endpoints FastAPI con engine/transport fake (offline).

Convenzioni identiche agli altri test:
- LocusConfig(_env_file=None) per non caricare .env
- Transport LLM/X fake iniettati
- DB :memory:
"""

from __future__ import annotations

from typing import Any, List

import pytest
from fastapi.testclient import TestClient

from locus.api import create_app
from locus.classify import Classifier
from locus.config import LocusConfig
from locus.db import Database
from locus.llm import LLMClient
from locus.probe import ProbeGenerator
from locus.target import TargetClient

# ── Fake LLM transport ─────────────────────────────────────────


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeUsage:
    prompt_tokens = 5
    completion_tokens = 10


class FakeChoices:
    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


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


class FakeTweet:
    def __init__(self, id: str, text: str, author_id: Any = 999) -> None:
        self.id = id
        self.text = text
        self.author_id = author_id
        self.created_at = None
        self.referenced_tweets = None


class FakeUser:
    def __init__(self, id: int) -> None:
        self.id = id


class FakeResponseData:
    def __init__(self, data) -> None:
        self.data = data


class FakeXClient:
    def __init__(self) -> None:
        self.posted: List[dict] = []
        self.tweets: List[FakeTweet] = []
        self.our_user_id = 999

    def get_user(self, username: str):
        return FakeResponseData(FakeUser(self.our_user_id))

    def create_tweet(self, text: str):
        tid = str(len(self.posted) + 1)
        self.posted.append({"id": tid, "text": text})
        return FakeResponseData({"id": tid})

    def get_users_mentions(self, **kwargs):
        return FakeResponseData(self.tweets)


# ── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
def config() -> LocusConfig:
    return LocusConfig(
        _env_file=None, target_handle="@Test", our_bot_handle="@Prober"
    )


@pytest.fixture
async def db(config: LocusConfig) -> Database:
    d = Database()
    await d.initialize(":memory:")
    await d.seed_properties(
        {
            "word_count": {"weight": 2.0, "prior_entropy": 2.0},
            "total_length": {"weight": 3.0, "prior_entropy": 3.0},
        }
    )
    yield d
    await d.close()


def _build_engine(config: LocusConfig, db: Database, x_fake: FakeXClient):
    llm = LLMClient(
        config,
        transport=FakeTransport(
            [
                '{"text": "do you enjoy riddles?"}',
                '{"pattern": "yes", "boolean": true, "score": 8, "leaks": ["it is 3 words"]}',
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


@pytest.fixture
def client(config: LocusConfig, db: Database) -> TestClient:
    engine = _build_engine(config, db, FakeXClient())
    app = create_app(config=config, engine=engine)
    with TestClient(app) as c:
        yield c


# ── Tests ──────────────────────────────────────────────────────


def test_status_returns_properties_and_counts(client: TestClient) -> None:
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target"] == "@Test"
    assert data["total_remaining_entropy"] == pytest.approx(5.0)
    assert len(data["properties"]) == 2
    assert data["counts"]["sessions"] == 0
    assert "phase5_threshold" in data


def test_properties_include_remaining_entropy(client: TestClient) -> None:
    resp = client.get("/api/properties")
    assert resp.status_code == 200
    props = resp.json()
    assert len(props) == 2
    total = sum(p["remaining_entropy"] for p in props)
    assert total == pytest.approx(5.0)


def test_probes_generate(client: TestClient) -> None:
    resp = client.post(
        "/api/probes/generate",
        json={"property_key": "total_length", "frame_alias": "neutral"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "do you enjoy riddles?"
    assert data["property_key"] == "total_length"


def test_probes_generate_missing_property(client: TestClient) -> None:
    resp = client.post("/api/probes/generate", json={"property_key": "nope"})
    assert resp.status_code == 404


def test_probes_post_persists(client: TestClient) -> None:
    resp = client.post(
        "/api/probes/post",
        json={"text": "hello @Test", "property_key": "word_count"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tweet_id"] == "1"
    assert "/status/1" in data["url"]

    probes = client.get("/api/probes")
    assert probes.status_code == 200
    body = probes.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "posted"


def test_probes_post_empty(client: TestClient) -> None:
    resp = client.post("/api/probes/post", json={"text": "   "})
    assert resp.status_code == 400


def test_probes_poll(client: TestClient) -> None:
    resp = client.post("/api/probes/poll")
    assert resp.status_code == 200
    assert resp.json()["replies"] == []


def test_run_dry_run(client: TestClient) -> None:
    resp = client.post("/api/run", json={"dry_run": True, "max_probes": 1})
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    status = client.get(f"/api/run/{session_id}")
    assert status.status_code == 200
    assert status.json()["status"] in ("running", "paused", "done")


def test_frames_endpoint(client: TestClient) -> None:
    resp = client.get("/api/frames")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_config_endpoint_no_secrets(client: TestClient) -> None:
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_handle"] == "@Test"
    assert "api_key" not in json_dumps(data).lower()


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_api_routes_win_over_spa_catchall(tmp_path, config: LocusConfig, db: Database) -> None:
    """With a compiled SPA in web/dist, /api/* must not be shadowed by the
    catch-all SPA route (regression: registration order)."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>locus spa</html>", encoding="utf-8")
    (dist / "assets" / "x.js").write_text("console.log(1)", encoding="utf-8")

    engine = _build_engine(config, db, FakeXClient())
    app = create_app(config=config, engine=engine, dist_path=dist)
    with TestClient(app) as c:
        health = c.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        spa = c.get("/some/client/route")
        assert spa.status_code == 200
        assert "locus spa" in spa.text

        asset = c.get("/assets/x.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text


def json_dumps(data) -> str:
    import json

    return json.dumps(data)
