"""Centralized configuration for Locus.

All tunables in one place.  Override via env vars with prefix ``LOCUS_``
or via a ``.env`` file in the project root.
"""

from __future__ import annotations

from typing import Optional

from pydantic import AliasChoices, Field, SecretStr
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
    our_bot_handle: str = Field(
        default="", description="X handle of our posting account (for reply polling)"
    )
    poll_interval_seconds: float = Field(
        default=30.0, description="Seconds between reply polls"
    )
    # Gap H: jitter randomizzato (±%) sull'intervallo di polling per
    # disallineare le richieste dall'API e ridurre burst sincronizzati.
    poll_interval_jitter: float = Field(
        default=0.2, description="Fraction (+/-) of random jitter on the poll interval"
    )
    poll_timeout_seconds: float = Field(
        default=300.0, description="Max seconds to wait for a reply"
    )
    # Gap H: dimensione pagina per il polling delle mention e cap di sicurezza
    # sul numero di pagine seguite (le reply oltre 100 nel batch non si perdono).
    max_poll_results: int = Field(
        default=100, description="Max results per mention poll page"
    )
    max_poll_pages: int = Field(
        default=100, description="Safety cap on pages followed during a single poll"
    )

    # ── LLM ───────────────────────────────────────────────────
    llm_api_key: Optional[SecretStr] = Field(
        default=None,
        description="OpenRouter API key",
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "LOCUS_LLM_API_KEY"),
    )
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
    # ── Costi LLM (USD per milione di token) — Gap G: non più hardcoded in llm.py ──
    llm_input_price_per_m: float = Field(
        default=3.0,  # preserved: value storico hardcoded in TokenUsage
        description="Costo LLM input in USD per milione di token",
    )
    llm_output_price_per_m: float = Field(
        default=15.0,  # preserved: valore storico hardcoded in TokenUsage
        description="Costo LLM output in USD per milione di token",
    )

    # ── X / Twitter ───────────────────────────────────────────
    x_consumer_key: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("TWITTER_CONSUMER_KEY", "LOCUS_X_CONSUMER_KEY"),
    )
    x_consumer_secret: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("TWITTER_CONSUMER_SECRET", "LOCUS_X_CONSUMER_SECRET"),
    )
    x_access_token: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("TWITTER_ACCESS_TOKEN", "LOCUS_X_ACCESS_TOKEN"),
    )
    x_access_token_secret: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices(
            "TWITTER_ACCESS_TOKEN_SECRET", "LOCUS_X_ACCESS_TOKEN_SECRET"
        ),
    )
    x_bearer_token: Optional[SecretStr] = Field(
        default=None,
        validation_alias=AliasChoices("TWITTER_BEARER_TOKEN", "LOCUS_X_BEARER_TOKEN"),
    )

    # ── Memory ────────────────────────────────────────────────
    similarity_threshold: float = Field(
        default=0.5, description="Min cosine similarity for dedup/recall"
    )
    dedup_top_k: int = Field(default=5, description="Past probes considered for dedup")

    # ── Engine / HITL ─────────────────────────────────────────
    max_probes_per_session: int = Field(
        default=50, description="Safety cap per session"
    )
    max_skips_per_session: int = Field(
        default=3,
        description="Consecutive skipped probes allowed before a session aborts "
        "(post failures only — reply timeouts no longer count as skips)",
    )
    phase5_entropy_threshold: float = Field(
        default=3.3, description="Autoregressive extraction trigger"
    )
    # Gap A: gate dello switch a Phase5. INERTA di default (False): con False il
    # motore si limita a loggare ``phase5_reached`` e procede col probing normale,
    # comportando il comportamento attuale. Solo con ``LOCUS_PHASE5_ENABLED=true``
    # (o via .env) l'iterazione viene deviata a P9 Extractor Prime / probe
    # autoregressivi per segmento.
    phase5_enabled: bool = Field(
        default=False,
        description="Switch a Phase5 (Extractor Prime autoregressive) quando in_phase5() e' True",
    )

    # ── Gap J: budget probe adattivo all'entropia residua ─────
    # ``enable_adaptive_probe_cap`` e' un gate INERTO di default (False): con
    # False il motore mantiene il tetto fisso ``max_probes_per_session``
    # (comportamento storico). Solo con ``LOCUS_ENABLE_ADAPTIVE_PROBE_CAP=true``
    # il budget della sessione scala con l'entropia totale residua:
    # ``ceil(total_remaining_entropy / entropy_per_probe)``, clampato tra
    # ``min_probe_cap`` e ``max_probes_per_session`` cosi' una sessione quasi
    # esaurita non spreca iterazioni inutilmente.
    enable_adaptive_probe_cap: bool = Field(
        default=False,
        description="Budget probe per sessione adattivo all'entropia residua (inerto di default)",
    )
    adaptive_entropy_per_probe: float = Field(
        default=0.5,
        description="Entropia (in bit) attesa per ogni probe, usata dal budget adattivo",
    )
    adaptive_min_probe_cap: int = Field(
        default=5,
        description="Pavimento minimo del budget adattivo (mai sotto questo valore)",
    )

    # ── Probe generation (Gap L) ──────────────────────────────
    # Rotazione lingua tra i probe (round-robin). Vuota = nessuna istruzione di
    # lingua → il modello risponde in inglese (comportamento pre-Gap L).
    probe_languages: list[str] = Field(
        default_factory=list,
        description="Lingue alternate per la generazione dei probe (round-robin)",
    )

    model_config = {"env_prefix": "LOCUS_", "env_file": ".env", "extra": "ignore"}
