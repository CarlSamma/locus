"""Centralized configuration for Locus.

All tunables in one place.  Override via env vars with prefix ``LOCUS_``
or via a ``.env`` file in the project root.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class LocusConfig(BaseSettings):
    """Single source of configuration — no Docker, no multiple services."""

    # ── Database ──────────────────────────────────────────────
    db_path: str = Field(default="data/locus.db", description="SQLite database path")

    # ── Property universe ─────────────────────────────────────
    properties_path: str = Field(
        default="data/properties.json",
        description="Path to the data-driven property universe",
    )

    # ── Target ────────────────────────────────────────────────
    target_handle: str = Field(default="@HackingA0", description="X handle of the target")
    poll_interval_seconds: float = Field(
        default=30.0, description="Seconds between reply polls"
    )
    poll_timeout_seconds: float = Field(
        default=300.0, description="Max seconds to wait for a reply"
    )

    # ── LLM ───────────────────────────────────────────────────
    llm_api_key: Optional[SecretStr] = Field(default=None, description="OpenRouter API key")
    llm_model_primary: str = Field(
        default="claude-sonnet-4", description="Model for normal operations"
    )
    llm_model_hard: str = Field(
        default="grok-4.3", description="Model for hard classification tasks"
    )
    llm_api_base: str = Field(
        default="https://openrouter.ai/api/v1",
        description="OpenAI-compatible API base URL",
    )
    llm_max_retries: int = Field(default=3, description="LLM call retries")
    llm_json_mode: bool = Field(default=True, description="Request JSON-structured output")

    # ── X / Twitter ───────────────────────────────────────────
    x_consumer_key: Optional[SecretStr] = Field(default=None)
    x_consumer_secret: Optional[SecretStr] = Field(default=None)
    x_access_token: Optional[SecretStr] = Field(default=None)
    x_access_token_secret: Optional[SecretStr] = Field(default=None)
    x_bearer_token: Optional[SecretStr] = Field(default=None)

    # ── Memory ────────────────────────────────────────────────
    similarity_threshold: float = Field(
        default=0.5, description="Min cosine similarity for dedup/recall"
    )
    dedup_top_k: int = Field(default=5, description="Past probes considered for dedup")

    # ── Engine / HITL ─────────────────────────────────────────
    max_probes_per_session: int = Field(
        default=50, description="Safety cap per session"
    )
    phase5_entropy_threshold: float = Field(
        default=3.3, description="Autoregressive extraction trigger"
    )

    model_config = {"env_prefix": "LOCUS_", "env_file": ".env", "extra": "ignore"}
