"""Pydantic models for all Locus entities."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Property(BaseModel):
    """A target property we are trying to determine (e.g. passphrase segment count)."""

    key: str
    weight: float = 1.0  # information gain in bits if resolved
    prior_entropy: float = 0.0  # estimated entropy before probing
    state: str = "unknown"  # unknown | confirmed | denied
    votes: int = 0  # number of probe outcomes contributing evidence
    value: Optional[object] = None  # resolved value once confirmed
    notes: str = ""


class Frame(BaseModel):
    """Conversational framing state (persona / alias / DPA style)."""

    alias: str
    persona: str
    prompt_template: str = ""
    status: str = "active"  # active | burned | absorbed
    created_at: datetime = Field(default_factory=_utcnow)


class Classification(BaseModel):
    """Structured output of the single classify LLM call."""

    pattern: str = "unknown"  # yes | no | block | evasive | ambiguous
    boolean: bool = False  # interpreted yes/no for binary properties
    score: int = 0  # 1-10 confidence of the evidence
    leaks: List[str] = Field(default_factory=list)  # accidental disclosures
    rationale: str = ""


class Probe(BaseModel):
    """One probe node in the attack tree (each row is a node)."""

    id: str = Field(default_factory=_uuid)
    session_id: str
    property_key: str
    frame_alias: str = ""
    text: str
    tweet_id: Optional[str] = None
    posted_at: Optional[datetime] = None
    reply_id: Optional[str] = None
    reply_text: Optional[str] = None
    replied_at: Optional[datetime] = None
    classification: Classification = Field(default_factory=Classification)
    score: float = 0.0
    status: str = "pending"  # pending | posted | replied | classified | skipped
    created_at: datetime = Field(default_factory=_utcnow)


class LedgerEntry(BaseModel):
    """Immutable log of probe outcomes per property (entropy source)."""

    id: str = Field(default_factory=_uuid)
    property_key: str
    outcome: str  # confirmed | denied | partial | leaked | blocked
    probe_id: str
    ts: datetime = Field(default_factory=_utcnow)
    note: str = ""


class IntelEntry(BaseModel):
    """A piece of useful intel discovered during a probe."""

    id: str = Field(default_factory=_uuid)
    session_id: str
    kind: str  # leak | pattern | behavior
    text: str
    entropy_before: float = 0.0
    entropy_after: float = 0.0
    note: str = ""
    ts: datetime = Field(default_factory=_utcnow)


class SessionRecord(BaseModel):
    """One campaign session."""

    id: str = Field(default_factory=_uuid)
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: Optional[datetime] = None
    probes_total: int = 0
    status: str = "running"  # running | paused | done
    target: str = ""
