"""Locus — minimal single-target extraction framework."""

from __future__ import annotations

__version__ = "0.1.0"

from locus.config import LocusConfig
from locus.engine import Engine
from locus.llm import LLMClient

__all__ = ["LocusConfig", "Engine", "LLMClient", "__version__"]
