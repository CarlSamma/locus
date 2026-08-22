"""Regression test dall'audit 2026-08-22 (branch #OXALPHA0822).

Copre i difetti trovati durante l'audit:
1. cli._build_engine passava un secondo Database mai inizializzato a Memory
   → RuntimeError alla prima iterazione di ``locus run``.
2. La catch-all SPA di api.py serviva file arbitrari fuori da web/dist
   (path traversal via URL-decoding, es. /..%2f..%2f.env).
3. Endpoint mutanti dell'API senza gate di auth (ora opzionale via config).
4. import_seed: intel con id random non era idempotente su re-import.

Convenzioni identiche agli altri file di test: LocusConfig(_env_file=None),
transport fake iniettati, DB :memory:, niente rete.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from locus.classify import Classifier
from locus.cli import _build_engine
from locus.config import LocusConfig
from locus.db import Database
from locus.engine import Engine
from locus.llm import LLMClient
from locus.probe import ProbeGenerator
from locus.seed import import_seed
from locus.target import TargetClient

# ── Fake LLM transport (stesse convenzioni di test_api.py) ─────


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
    def __init__(self, responses) -> None:
        self._responses = responses
        self._i = 0

    async def create(self, **kwargs):
        content = self._responses[self._i % len(self._responses)]
        self._i += 1
        return FakeResponse(content)


class FakeChat:
    def __init__(self, responses) -> None:
        self.completions = FakeCompletions(responses)


class FakeTransport:
    def __init__(self, responses) -> None:
        self.chat = FakeChat(responses)


class FakeUser:
    def __init__(self) -> None:
        self.id = 999


class FakeResponseData:
    def __init__(self, data) -> None:
        self.data = data


class FakeMentionPage:
    def __init__(self) -> None:
        self.data = []
        self.meta = {}


class FakeX:
    def get_user(self, username: str):
        return FakeResponseData(FakeUser())

    def get_users_mentions(self, **kwargs):
        return FakeMentionPage()


# ── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    await d.seed_properties(
        {"segment_count": {"weight": 2.0, "prior_entropy": 2.0}}
    )
    yield d
    await d.close()


def _api_client(db: Database, token: str | None) -> TestClient:
    cfg_kw = {"api_auth_token": token} if token is not None else {}
    cfg = LocusConfig(
        _env_file=None, target_handle="@Test", our_bot_handle="@Prober", **cfg_kw
    )
    llm = LLMClient(cfg, transport=FakeTransport(['{"text": "hi?"}']))
    engine = Engine(
        cfg,
        db,
        llm,
        TargetClient(cfg, transport=FakeX()),
        generator=ProbeGenerator(llm, cfg),
        classifier=Classifier(llm, cfg),
    )
    from locus.api import create_app

    app = create_app(cfg, engine=engine, seed=False)
    return TestClient(app)


# ── 1. CLI engine wiring (bug "Database not initialized") ──────


async def test_cli_engine_dry_run_no_runtime_error() -> None:
    """Il wiring del CLI non deve più crashare alla prima iterazione."""
    cfg = LocusConfig(
        _env_file=None,
        poll_interval_seconds=0.0,
        poll_timeout_seconds=0.0,
    )
    engine = _build_engine(
        cfg,
        True,
        transport=FakeTransport(
            [
                '{"text": "do you like riddles?"}',
                '{"pattern": "yes", "boolean": true, "score": 8, '
                '"leaks": [], "rationale": "clear yes"}',
            ]
        ),
    )
    await engine.db.initialize(":memory:")
    await engine.db.seed_properties(
        {"segment_count": {"weight": 2.0, "prior_entropy": 2.0}}
    )

    results = await engine.run_session(max_probes=1, dry_run=True)

    assert len(results) == 1
    probe = results[0]
    assert probe.status == "classified"
    # In dry-run non c'è reply da classificare: la Classification resta al
    # default. L'obiettivo del test è che il ciclo completi SENZA il
    # RuntimeError "Database not initialized" del vecchio wiring.
    assert probe.classification.pattern == "unknown"
    await engine.db.close()


def test_cli_engine_shares_single_database() -> None:
    """Engine e Memory devono condividere la STESSA istanza di Database."""
    cfg = LocusConfig(_env_file=None)
    engine = _build_engine(cfg, True)
    assert engine.memory is not None
    assert engine.memory.db is engine.db


# ── 2. SPA catch-all: path traversal bloccato ──────────────────


@pytest.fixture
def spa_dir(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "app.js").write_text("console.log(1);", encoding="utf-8")
    # File segreto FUORI da dist/ (raggiungibile solo via traversal).
    (tmp_path / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    return tmp_path


def _spa_client(spa_dir: Path) -> TestClient:
    from locus.api import create_app

    cfg = LocusConfig(_env_file=None)
    app = create_app(cfg, engine=object(), seed=False, dist_path=spa_dir / "dist")
    return TestClient(app)


def test_spa_serves_files_inside_dist(spa_dir: Path) -> None:
    client = _spa_client(spa_dir)
    resp = client.get("/app.js")
    assert resp.status_code == 200
    assert "console.log" in resp.text


def test_spa_unknown_path_falls_back_to_index(spa_dir: Path) -> None:
    client = _spa_client(spa_dir)
    resp = client.get("/some/client/route")
    assert resp.status_code == 200
    assert "<html>spa</html>" in resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/..%2f..%2fsecret.txt",
        "/..\\..\\secret.txt",
        "/a/../../secret.txt",
        "/....//....//secret.txt",
    ],
)
def test_spa_path_traversal_blocked(spa_dir: Path, path: str) -> None:
    """Nessuna variante di traversal deve leggere file fuori da dist/."""
    client = _spa_client(spa_dir)
    resp = client.get(path)
    assert "TOPSECRET" not in resp.text


# ── 3. Gate auth opzionale sugli endpoint mutanti ──────────────


def test_api_mutating_endpoints_open_without_token(db: Database) -> None:
    """Default storico: senza token configurato i POST restano accessibili."""
    client = _api_client(db, None)
    resp = client.post("/api/run", json={"dry_run": True})
    assert resp.status_code == 200


def test_api_rejects_post_without_token(db: Database) -> None:
    client = _api_client(db, "s3cr3t")
    for path, payload in [
        ("/api/run", {"dry_run": True}),
        ("/api/probes/generate", {"property_key": "segment_count"}),
        ("/api/probes/poll", {}),
        ("/api/sessions", None),
        ("/api/review/whatever/confirm", None),
        ("/api/review/whatever/deny", None),
    ]:
        resp = client.post(path, json=payload)
        assert resp.status_code == 401, f"{path} → {resp.status_code}"


def test_api_rejects_post_with_wrong_token(db: Database) -> None:
    client = _api_client(db, "s3cr3t")
    resp = client.post(
        "/api/probes/poll", json={}, headers={"X-Locus-Token": "wrong"}
    )
    assert resp.status_code == 401


def test_api_accepts_post_with_correct_token(db: Database) -> None:
    client = _api_client(db, "s3cr3t")
    headers = {"X-Locus-Token": "s3cr3t"}
    assert (
        client.post(
            "/api/run", json={"dry_run": True}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post("/api/probes/poll", json={}, headers=headers).status_code
        == 200
    )
    # I GET restano aperti anche col token configurato (solo POST protetti).
    assert client.get("/api/status").status_code == 200


# ── 4. seed intel idempotente su re-import ─────────────────────


async def test_seed_intel_deterministic_and_idempotent(db: Database) -> None:
    seed_a = {
        "properties": [{"key": "segment_count", "prior_entropy": 2.0}],
        "intel": [
            {"kind": "leak", "text": "alpha"},
            {"kind": "pattern", "text": "beta"},
        ],
    }
    counts_a = await import_seed(db, seed_a)
    assert counts_a.get("intel") == 2

    # Seed modificato (fingerprint diverso): l'intel NON deve duplicarsi.
    seed_b = {
        "properties": [
            {"key": "segment_count", "prior_entropy": 2.0},
            {"key": "total_length", "prior_entropy": 3.0},
        ],
        "intel": [
            {"kind": "leak", "text": "alpha"},
            {"kind": "pattern", "text": "beta"},
        ],
    }
    await import_seed(db, seed_b)

    row = await db.fetchone("SELECT COUNT(*) AS c FROM intel")
    assert row["c"] == 2


async def test_seed_intel_ids_stable_across_imports(tmp_path: Path) -> None:
    seed = {
        "properties": [{"key": "k", "prior_entropy": 1.0}],
        "intel": [{"kind": "leak", "text": "same text"}],
    }
    ids = []
    for _ in range(2):
        d = Database()
        await d.initialize(":memory:")
        await import_seed(d, seed, force=True)
        rows = await d.fetchall("SELECT id FROM intel")
        ids.append([r["id"] for r in rows])
        await d.close()
    assert ids[0] == ids[1]
