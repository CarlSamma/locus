"""Milestone 1 tests: config, models, database schema."""

from __future__ import annotations

import json

import pytest

from locus.config import LocusConfig
from locus.db import Database
from locus.models import Classification, Probe, Property


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    yield d
    await d.close()


def test_config_defaults() -> None:
    cfg = LocusConfig()
    assert cfg.target_handle == "@HackingA0"
    assert cfg.poll_interval_seconds == 30.0
    assert cfg.phase5_entropy_threshold == 3.3


def test_config_env_prefix() -> None:
    cfg = LocusConfig(_env_file=None, target_handle="@test")
    assert cfg.target_handle == "@test"


def test_property_model() -> None:
    p = Property(key="segment_count", weight=2.0, prior_entropy=2.0)
    assert p.state == "unknown"
    assert p.votes == 0


def test_probe_model_defaults() -> None:
    probe = Probe(session_id="s1", property_key="segment_count", text="hi")
    assert probe.status == "pending"
    assert probe.classification.pattern == "unknown"
    assert probe.score == 0.0


async def test_schema_creates_all_tables(db: Database) -> None:
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    names = {r["name"] for r in rows}
    expected = {
        "properties",
        "probes",
        "frames",
        "intel",
        "sessions",
        "ledger",
        "schema_version",
    }
    assert expected <= names


async def test_schema_version(db: Database) -> None:
    row = await db.fetchone("SELECT version FROM schema_version LIMIT 1")
    assert row["version"] == 1


async def test_seed_properties(db: Database) -> None:
    props = json.load(open("data/properties.json"))
    n = await db.seed_properties(props)
    assert n == 8
    row = await db.fetchone(
        "SELECT weight, state FROM properties WHERE key = 'total_length'"
    )
    assert row["weight"] == 3.0
    assert row["state"] == "unknown"


async def test_seed_properties_idempotent(db: Database) -> None:
    props = json.load(open("data/properties.json"))
    await db.seed_properties(props)
    await db.seed_properties(props)
    row = await db.fetchone("SELECT COUNT(*) AS c FROM properties")
    assert row["c"] == 8
