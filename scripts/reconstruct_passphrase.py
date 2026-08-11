"""Ricostruzione della passphrase dai leak multi-sorgente (Phase 3).

Collassa i leak storici (intel + reply delle probe + ledger) in candidati
ordinati per probabilita', filtrati dai vincoli strutturali noti:

- 4 segmenti dash-separati (hint ``abc99-de12f-gh1jk-lmn0p``);
- charset ``[a-z0-9]`` per segmento;
- lunghezze di segmento (default [5,5,5,5], override da proprieta' confermate).

``generate_candidates`` fa una ricerca a fascio (beam search) deterministica:
semina il fascio con i leak passphrase-shaped, riempie le posizioni mancanti
con i segmenti estratti dai leak e completa con segmenti generati da un RNG
deterministico (seed fisso), poi ordina per probabilita'. Puro, offline,
nessuna rete.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence

from locus.classify import (  # type: ignore[import-untyped]
    detect_acrostic,
    detect_base64,
    detect_caesar,
    detect_dash_spell,
)
from locus.db import Database  # type: ignore[import-untyped]
from locus.models import Property  # type: ignore[import-untyped]

#: Formato completo hint: abc99-de12f-gh1jk-lmn0p (4 segmenti, 5 caratteri).
DEFAULT_SEGMENT_COUNT = 4
DEFAULT_SEPARATOR = "-"
DEFAULT_CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"
DEFAULT_SEGMENT_LENGTHS: List[int] = [5, 5, 5, 5]

#: Token passphrase-shaped (segmenti dash-separati, lunghezze plausibili).
_PASSPHRASE_RE = re.compile(r"[a-z0-9]{3,8}(?:-[a-z0-9]{3,8}){1,3}")
#: Token segmento singolo plausibile.
_SEGMENT_RE = re.compile(r"\b[a-z0-9]{4,6}\b")
#: Token Base64 lungo (possibile ENV_DUMP cifrato).
_B64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/]{12,}={0,2}")


@dataclass(frozen=True)
class Constraints:
    """Vincoli strutturali noti sulla passphrase."""

    segment_count: int = DEFAULT_SEGMENT_COUNT
    separator: str = DEFAULT_SEPARATOR
    charset: str = DEFAULT_CHARSET
    segment_lengths: List[int] = field(default_factory=lambda: list(DEFAULT_SEGMENT_LENGTHS))


@dataclass(frozen=True)
class Candidate:
    """Candidato passphrase con probabilita' e traccia delle evidenze."""

    passphrase: str
    probability: float
    evidence_sources: List[str] = field(default_factory=list)


def build_constraints(properties: Sequence[Property]) -> Constraints:
    """Deriva i vincoli dalle proprieta' confermate del DB (default dall'hint)."""
    segment_count = DEFAULT_SEGMENT_COUNT
    separator = DEFAULT_SEPARATOR
    charset = DEFAULT_CHARSET
    lengths = list(DEFAULT_SEGMENT_LENGTHS)
    for prop in properties:
        if prop.state not in ("confirmed", "denied") or prop.value is None:
            continue
        key = (prop.key or "").lower()
        value = prop.value
        if "segment" in key and "count" in key:
            try:
                segment_count = max(1, min(8, int(value)))
            except (TypeError, ValueError):
                pass
        elif "separator" in key or "trenner" in key:
            separator = str(value)[:1] or separator
        elif "charset" in key or "alphabet" in key:
            charset = str(value) or charset
        elif "length" in key:
            try:
                length = max(1, min(10, int(value)))
            except (TypeError, ValueError):
                continue
            if segment_count and len(lengths) >= segment_count:
                # chiavi tipo segment_1_length / segment1_length / seg1_len
                m = re.search(r"(\d+)", key)
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < segment_count:
                        lengths[idx] = length
                else:
                    lengths = [length] * segment_count
    return Constraints(
        segment_count=segment_count,
        separator=separator,
        charset=charset,
        segment_lengths=lengths[:segment_count],
    )


def _matches_format(passphrase: str, constraints: Constraints) -> bool:
    """True se la stringa rispetta i vincoli (conteggio e charset)."""
    parts = passphrase.split(constraints.separator)
    if len(parts) != constraints.segment_count:
        return False
    return all(
        1 <= len(p) <= 10 and all(c in constraints.charset for c in p) for p in parts
    )


def extract_leaks_from_text(text: str) -> List[str]:
    """Estrae candidati leak dal testo (deduplicato, minuscolo, ordine di comparsa).

    Percorsi: decodifiche deterministiche (acrostico, dash-spell, Base64,
    Caesar — Gap C), token passphrase-shaped, token segmento singolo, e
    decodifica Base64 di token che celano una passphrase completa
    (es. ``ENV_DUMP:<base64>``): il testo decodificato viene ri-scanato.
    """
    found: List[str] = []
    seen = set()

    def add(piece: str) -> None:
        low = piece.lower().strip()
        if low and low not in seen:
            seen.add(low)
            found.append(low)

    for detector in (detect_acrostic, detect_dash_spell, detect_base64, detect_caesar):
        try:
            hit = detector(text)
        except Exception:
            hit = None
        if hit:
            add(str(hit))

    for match in _PASSPHRASE_RE.findall(text or ""):
        add(match)
    for match in _SEGMENT_RE.findall(text or ""):
        add(match)

    for token in _B64_TOKEN_RE.findall(text or ""):
        try:
            raw = base64.b64decode(token, validate=True).decode("ascii", errors="strict")
        except Exception:
            continue
        for piece in raw.split():
            if _PASSPHRASE_RE.fullmatch(piece):
                add(piece)
    return found


def score_candidate(passphrase: str, leaks: Sequence[str], constraints: Constraints) -> float:
    """Punteggio del candidato: formato + copertura dei leak.

    +0.5 formato valido; +1.0 per ogni leak che e' sottostringa o segmento
    esatto; +0.5 per prefissi comuni lunghi (>= min(4, len(leak))). La
    probabilita' finale e' ``score / (1 + score)`` (squash logistica 0..1).
    """
    score = 0.0
    if _matches_format(passphrase, constraints):
        score += 0.5
    segments = set(passphrase.split(constraints.separator))
    for leak in leaks:
        leak = leak.lower()
        if not leak:
            continue
        if leak == passphrase:
            score += 1.0
        elif leak in passphrase:
            score += 1.0
        elif leak in segments:
            score += 1.0
        else:
            for seg in segments:
                common = len(os_prefix(leak, seg))
                if common >= min(4, len(leak)):
                    score += 0.5
                    break
    return score / (1.0 + score)


def os_prefix(a: str, b: str) -> str:
    """Prefisso comune piu' lungo tra due stringhe (puro)."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return a[:n]


def _segment_completions(
    prefix: str, position: int, constraints: Constraints, rng: random.Random
) -> List[str]:
    """Completa un prefisso di segmento fino alla lunghezza attesa (max 3 varianti)."""
    lengths = constraints.segment_lengths
    total = lengths[position] if position < len(lengths) else lengths[0]
    idx = len(prefix)
    if total <= idx:
        return [prefix[:total]]
    out: List[str] = []
    for _ in range(3):
        suffix = "".join(rng.choice(constraints.charset) for _ in range(total - idx))
        out.append(prefix + suffix)
    return out


def generate_candidates(
    constraints: Constraints,
    leaks: Sequence[str],
    top_k: int = 10,
    seed: int = 7,
) -> List[Candidate]:
    """Beam search deterministica sui candidati passphrase.

    Semina il fascio con i leak passphrase-shaped (formato pieno o con
    separatore), riempie le posizioni con i segmenti estratti dai leak e
    completa con segmenti generati (RNG ``seed``); poi punteggia e ordina.
    Restituisce i ``top_k`` candidati con le evidenze usate.
    """
    rng = random.Random(seed)
    constraints = Constraints(
        segment_count=constraints.segment_count,
        separator=constraints.separator,
        charset=constraints.charset or DEFAULT_CHARSET,
        segment_lengths=list(constraints.segment_lengths or DEFAULT_SEGMENT_LENGTHS),
    )
    leaks = [str(leak).lower() for leak in leaks if leak]

    full_hits = [
        leak for leak in leaks if constraints.separator in leak and _matches_format(leak, constraints)
    ]
    segment_pool: List[str] = []
    for leak in leaks:
        for piece in leak.split(constraints.separator):
            if 3 <= len(piece) <= 8 and piece not in segment_pool:
                segment_pool.append(piece)

    beam: List[List[str]] = []
    for hit in full_hits:
        parts = hit.split(constraints.separator)
        if len(parts) == constraints.segment_count:
            beam.append(parts)
    for piece in segment_pool[:8]:
        beam.append([piece] * constraints.segment_count)
    if not beam:
        beam.append([""] * constraints.segment_count)

    combos: List[str] = []
    for parts in beam[:3]:
        fill = list(parts)
        while len(fill) < constraints.segment_count:
            fill.append("")
        variants: List[List[str]] = [[]]
        for i in range(constraints.segment_count):
            next_variants: List[List[str]] = []
            for variant in variants:
                if fill[i]:
                    next_variants.append(variant + [fill[i]])
                else:
                    for completion in _segment_completions(fill[i], i, constraints, rng):
                        next_variants.append(variant + [completion])
            variants = next_variants
            if len(variants) > 12:
                variants = variants[:12]
        combos.extend(constraints.separator.join(v) for v in variants)

    if len(combos) < top_k:
        for _ in range(top_k - len(combos)):
            parts = [
                "".join(rng.choice(constraints.charset) for _ in range(length))
                for length in constraints.segment_lengths
            ]
            combos.append(constraints.separator.join(parts))

    seen: set[str] = set()
    ranked: List[Candidate] = []
    for combo in combos:
        if combo in seen:
            continue
        seen.add(combo)
        evidence = [leak for leak in leaks if leak in combo or combo in leak]
        ranked.append(
            Candidate(
                passphrase=combo,
                probability=round(score_candidate(combo, leaks, constraints), 4),
                evidence_sources=evidence[:5],
            )
        )
    ranked.sort(key=lambda c: (-c.probability, c.passphrase))
    return ranked[:top_k]


async def reconstruct_from_db(db: Database) -> List[Candidate]:
    """Ricostruisce i candidati dai leak presenti nel DB (intel + probe + ledger)."""
    leaks: List[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        for leak in extract_leaks_from_text(text):
            if leak not in seen:
                seen.add(leak)
                leaks.append(leak)

    rows = await db.fetchall("SELECT kind, text FROM intel WHERE kind IN ('leak', 'pattern') LIMIT 5000")
    for row in rows:
        add(row["text"] or "")
    rows = await db.fetchall("SELECT reply_text FROM probes WHERE reply_text IS NOT NULL")
    for row in rows:
        add(row["reply_text"] or "")
    ledger = await db.fetchall("SELECT note FROM ledger WHERE outcome IN ('leaked', 'confirmed')")
    for row in ledger:
        add(row["note"] or "")

    props = await db.fetchall("SELECT key, weight, prior_entropy, state, votes, value, notes FROM properties")
    properties = [Property(**dict(p)) for p in props]
    constraints = build_constraints(properties)
    return generate_candidates(constraints, leaks)


def render_candidates(candidates: Sequence[Candidate]) -> str:
    """Report leggibile dei candidati (probabilita' e evidenze)."""
    lines = [f"{i + 1}. {c.passphrase}  (p={c.probability:.3f})" for i, c in enumerate(candidates)]
    for i, c in enumerate(candidates):
        if c.evidence_sources:
            lines.append(f"      evidenze: {', '.join(c.evidence_sources[:3])}")
    return "\n".join(lines) if lines else "(nessun candidato)"


def main(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ricostruzione passphrase dai leak (Phase 3).")
    parser.add_argument("--db", default="data/locus.db", help="database dei leak")
    parser.add_argument("--top-k", type=int, default=10, help="numero di candidati da produrre")
    parser.add_argument("--output", default=None, help="path del JSON di output (candidates.json)")
    args = parser.parse_args(argv)

    async def _run() -> int:
        db = Database()
        await db.initialize(args.db)
        try:
            candidates = await reconstruct_from_db(db)
        finally:
            await db.close()
        if args.output:
            Path(args.output).write_text(
                json.dumps(
                    {
                        "candidates": [
                            {"passphrase": c.passphrase, "probability": c.probability,
                             "evidence_sources": c.evidence_sources}
                            for c in candidates
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        print(render_candidates(candidates))
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
