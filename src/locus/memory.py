"""Lightweight memory — semantic dedup + recall.

Only the two memory patterns Locus actually needs (vs. aware's 7 types):
1. Dedup: avoid re-asking the same question as an earlier probe.
2. Recall: surface the most relevant past probes/intel when generating a new one.

Embeddings default to a deterministic n-gram hashing embedder (no model
required) so the framework runs offline; a real embedder can be injected for
production.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol, Tuple

from locus.db import Database
from locus.trust import sanitize_untrusted


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vec_to_blob(vec: List[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> List[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class Embedder(Protocol):
    """Protocol for embedding services."""

    dimension: int

    async def encode(self, text: str) -> List[float]: ...


_WORD_RE = re.compile(r"[a-z0-9]+")


class NgramHashEmbedder:
    """Deterministic bag-of-ngrams embedder (offline / tests).

    Feature hashing over character n-grams (2..4) plus lowercase word tokens,
    with L2 normalization.  Two texts sharing lexical/char-level structure get
    a meaningful cosine similarity (identical text → 1.0), unlike a plain
    SHA-256 hash which scatters near-duplicates to orthogonal vectors.
    """

    def __init__(self, dim: int = 256, n_min: int = 2, n_max: int = 4) -> None:
        self._dim = dim
        self._n_min = n_min
        self._n_max = n_max

    @property
    def dimension(self) -> int:
        return self._dim

    def _features(self, text: str) -> List[str]:
        low = text.lower()
        feats: List[str] = []
        # character n-grams (characterize structure / typos / obfuscation)
        for n in range(self._n_min, self._n_max + 1):
            for i in range(len(low) - n + 1):
                feats.append(f"c{n}:{low[i : i + n]}")
        # word tokens (semantic overlap)
        for tok in _WORD_RE.findall(low):
            feats.append(f"w:{tok}")
        return feats

    @staticmethod
    def _hash_feature(feature: str, dim: int) -> int:
        digest = hashlib.md5(feature.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "little") % dim

    async def encode(self, text: str) -> List[float]:
        vec = [0.0] * self._dim
        for feat in self._features(text):
            idx = self._hash_feature(feat, self._dim)
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            return vec
        return [x / norm for x in vec]


class HashEmbedder:
    """Legacy SHA-256 embedder — kept for API compatibility.

    Deprecated: SHA-256 hashes are orthogonally random, so cosine similarity
    between them is pure noise.  Use :class:`NgramHashEmbedder` (the default).
    """

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    @property
    def dimension(self) -> int:
        return self._dim

    async def encode(self, text: str) -> List[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [b / 255.0 for b in h]
        return (vec * (self._dim // len(vec) + 1))[: self._dim]


class Memory:
    """Semantic dedup + recall over past probes stored in SQLite."""

    def __init__(self, db: Database, embedder: Optional[Any] = None) -> None:
        self.db = db
        self.embedder = embedder or NgramHashEmbedder()

    async def remember(self, text: str, kind: str = "probe") -> str:
        """Store a probe/intel text with its embedding. Returns the id.

        Content is sanitized on write: any text derived from a target reply
        (kind="intel", leaks, recalled context) is untrusted and may smuggle
        hidden instructions.  Sanitization is deterministic (string ops only)
        and idempotent, so trusted probe text is left unchanged.
        """
        entry_id = str(uuid.uuid4())
        text = sanitize_untrusted(text)
        vec = await self.embedder.encode(text)
        await self.db.execute(
            "INSERT INTO memory_entries (id, content, embedding, kind, ts) VALUES (?, ?, ?, ?, ?)",
            (entry_id, text, _vec_to_blob(vec), kind, _utcnow()),
        )
        await self.db.commit()
        return entry_id

    async def dedup(
        self, text: str, threshold: float = 0.9, top_k: int = 5
    ) -> Tuple[bool, List[Tuple[str, float]]]:
        """Return (is_duplicate, matches) — True if an existing probe is
        semantically near-identical to `text`."""
        matches = await self.recall(text, top_k=top_k)
        dupes = [(mid, sim) for mid, sim in matches if sim >= threshold]
        return (bool(dupes), dupes)

    async def recall(
        self, query: str, top_k: int = 5, threshold: float = 0.0, kind: str = "probe"
    ) -> List[Tuple[str, float]]:
        """Return [(memory_id, similarity), …] sorted by relevance."""
        return [(mid, sim) for mid, _, sim in await self._score(query, top_k, threshold, kind)]

    async def recall_texts(
        self, query: str, top_k: int = 5, threshold: float = 0.0, kind: str = "probe"
    ) -> List[str]:
        """Return the content of the most relevant entries sorted by relevance."""
        return [content for _, content, _ in await self._score(query, top_k, threshold, kind)]

    async def _score(
        self, query: str, top_k: int, threshold: float, kind: str
    ) -> List[Tuple[str, str, float]]:
        """Return [(id, content, similarity), …] sorted by relevance."""
        query_vec = await self.embedder.encode(query)
        rows = await self.db.fetchall(
            "SELECT id, content, embedding FROM memory_entries WHERE kind = ?",
            (kind,),
        )
        scored: List[Tuple[str, str, float]] = []
        for row in rows:
            stored = _blob_to_vec(row["embedding"])
            sim = _cosine_similarity(query_vec, stored)
            if sim >= threshold:
                scored.append((row["id"], row["content"], sim))
        scored.sort(key=lambda x: x[2], reverse=True)
        return scored[:top_k]

    async def count(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS c FROM memory_entries")
        return row["c"] if row else 0

    async def export(self) -> dict:
        rows = await self.db.fetchall("SELECT id, content, kind, ts FROM memory_entries")
        return {
            "entries": [
                {"id": r["id"], "content": r["content"], "kind": r["kind"], "ts": r["ts"]}
                for r in rows
            ]
        }
