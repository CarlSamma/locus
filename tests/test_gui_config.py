"""GUI/config wiring tests: env aliases and seed-backed pickers."""

from __future__ import annotations

import os

import pytest

from locus.config import LocusConfig
from locus.db import Database
from locus.seed import import_seed, load_seed


def test_env_alias_twitter_bearer(monkeypatch) -> None:
    monkeypatch.setenv("TWITTER_BEARER_TOKEN", "bearer-x")
    cfg = LocusConfig(_env_file=None)
    assert cfg.x_bearer_token is not None
    assert cfg.x_bearer_token.get_secret_value() == "bearer-x"


def test_env_alias_openrouter(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    cfg = LocusConfig(_env_file=None)
    assert cfg.llm_api_key is not None
    assert cfg.llm_api_key.get_secret_value() == "sk-or-x"


def test_env_alias_first_match_wins(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-legacy")
    monkeypatch.setenv("LOCUS_LLM_API_KEY", "sk-or-locus")
    cfg = LocusConfig(_env_file=None)
    # AliasChoices resolves to the first defined var (OPENROUTER_API_KEY)
    assert cfg.llm_api_key.get_secret_value() == "sk-or-legacy"


async def test_gui_picker_data_from_seed() -> None:
    """The GUI loads properties and active frames from the SSOT seed."""
    db = Database()
    await db.initialize(":memory:")
    await import_seed(db, load_seed("src/locus/data/locus_seed.json"))
    props = await db.fetchall("SELECT key FROM properties")
    frames = await db.fetchall("SELECT alias FROM frames WHERE status='active'")
    assert len(props) == 16
    assert len(frames) == 11  # active frames in seed
    await db.close()
