"""Entropy-driven property selection — pure, data-driven, no LLM.

Remaining entropy model (binary outcomes): each confirming or denying vote
halves a property's uncertainty.  The selected property is the one with the
highest remaining entropy among unresolved properties.

Phase 5: when the *total* remaining entropy across all unresolved properties
drops below ``phase5_entropy_threshold``, the framework switches to
autoregressive extraction of the full passphrase.
"""

from __future__ import annotations

from typing import FrozenSet, List, Optional, Tuple

from locus.models import Property


def remaining_entropy(prop: Property) -> float:
    """Remaining uncertainty in bits for a property.

    Each vote halves the initial uncertainty; a resolved property has 0.
    """
    if prop.state in ("confirmed", "denied"):
        return 0.0
    return prop.prior_entropy * (0.5 ** prop.votes)


def total_remaining_entropy(properties: List[Property]) -> float:
    """Sum of remaining entropy over all properties."""
    return sum(remaining_entropy(p) for p in properties)


def select_property(
    properties: List[Property], tried: FrozenSet[str] = frozenset()
) -> Optional[Property]:
    """Pick the unresolved property with the highest remaining entropy.

    ``tried`` lists keys already attempted *this session*: zero-entropy
    properties (unresolvable hypotheses like ``length_13_16``) are excluded so
    the engine cannot re-select the same one forever — it burns each at most
    once per session, then the caller ends the session when None is returned.

    Tie-break among equal-entropy candidates is by fewest votes, then by key,
    so the selection is deterministic and rotates instead of always landing on
    the first row (frame/format monotony).

    Returns:
        The selected Property, or None if all properties are resolved or every
        remaining candidate has already been tried this session.
    """
    candidates = [
        p
        for p in properties
        if p.state not in ("confirmed", "denied") and p.key not in tried
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (remaining_entropy(p), -p.votes, p.key))


def in_phase5(
    properties: List[Property], threshold: float = 3.3
) -> Tuple[bool, float]:
    """Return (in_phase5, total_remaining_entropy)."""
    total = total_remaining_entropy(properties)
    return total < threshold, total
