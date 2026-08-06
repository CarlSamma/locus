"""Locus exception hierarchy."""

from __future__ import annotations

from typing import Any, Optional


class LocusError(Exception):
    """Base exception for all Locus errors."""


class LLMError(LocusError):
    """Raised when LLM generation or parsing fails."""

    def __init__(
        self,
        message: str,
        *,
        model: Optional[str] = None,
        original: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.original = original


class TwitterError(LocusError):
    """Raised when X/Twitter API calls fail."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        original: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.original = original
