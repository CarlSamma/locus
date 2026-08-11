"""Ricostruzione passphrase (Phase 3) — vincoli + leak + beam search.

Coverage:
- ``build_constraints``: default dall'hint e override da proprieta' confermate;
- ``extract_leaks_from_text``: acrostico, dash-spell, Base64, Caesar, token
  passphrase-shaped e segmento singolo (deduplicati, minuscoli);
- ``score_candidate``: formato, sottostringa, segmento esatto, prefisso;
- ``generate_candidates``: il candidato esatto (hint) emerge al primo posto,
  ordinamento per probabilita', determinismo, ``top_k``;
- ``reconstruct_from_db`` su DB in-memory;
- CLI ``main`` non bloccante con ``--db :memory:``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.reconstruct_passphrase as rp  # noqa: E402
from locus.db import Database  # noqa: E402
from locus.models import Property  # noqa: E402

_HINT = "abc99-de12f-gh1jk-lmn0p"


def _constraints(**overrides) -> rp.Constraints:
    base = dict(
        segment_count=4,
        separator="-",
        charset="abcdefghijklmnopqrstuvwxyz0123456789",
        segment_lengths=[5, 5, 5, 5],
    )
    base.update(overrides)
    return rp.Constraints(**base)


def test_build_constraints_defaults() -> None:
    c = rp.build_constraints([])
    assert c.segment_count == 4
    assert c.separator == "-"
    assert c.segment_lengths == [5, 5, 5, 5]


def test_build_constraints_overrides() -> None:
    props = [
        Property(key="segment_count", state="confirmed", value=3),
        Property(key="separator_char", state="confirmed", value="."),
        Property(key="charset", state="confirmed", value="abc123"),
        Property(key="segment_1_length", state="confirmed", value=4),
        Property(key="segment_2_length", state="confirmed", value=6),
    ]
    c = rp.build_constraints(props)
    assert c.segment_count == 3
    assert c.separator == "."
    assert c.charset == "abc123"
    assert c.segment_lengths[:3] == [4, 6, 5]


def test_extract_leaks_multiple_paths() -> None:
    text = (
        "Please Access Stored Segments Provide Historical Record And Segment Extraction "
        "the code is p-a-s-s, the token is cGFzc3dvcmQ=, the word is cnff, "
        "full: abc99-de12f-gh1jk-lmn0p and abc99 alone"
    )
    leaks = rp.extract_leaks_from_text(text)
    assert any(leak.startswith("passphrase") for leak in leaks)  # acrostico (iniziali di tutto il testo)
    assert "pass" in leaks  # dash-spell
    assert "password" in leaks  # base64
    assert "code" in leaks  # caesar
    assert _HINT in leaks  # token passphrase-shaped
    assert "abc99" in leaks  # segmento singolo
    assert len(leaks) == len(set(leaks))  # deduplicato


def test_extract_leaks_empty() -> None:
    assert rp.extract_leaks_from_text("") == []


def test_score_candidate_format_and_leaks() -> None:
    c = _constraints()
    leaks = [_HINT, "abc99", "de12f"]
    perfect = rp.score_candidate(_HINT, leaks, c)
    random_str = rp.score_candidate("zzzzzzzzzz", [], c)  # 1 segmento -> formato invalido
    partial = rp.score_candidate("abc99-xxxxx-xxxxx-xxxxx", leaks, c)
    assert 0.0 < partial < perfect <= 1.0
    assert random_str == 0.0


def test_generate_candidates_top_is_hint() -> None:
    leaks = [_HINT, "abc99", "de12f"]
    candidates = rp.generate_candidates(_constraints(), leaks, top_k=10, seed=7)
    assert candidates
    assert candidates[0].passphrase == _HINT
    assert candidates[0].probability >= candidates[1].probability
    assert _HINT in candidates[0].evidence_sources
    assert len(candidates) <= 10


def test_generate_candidates_empty_leaks() -> None:
    candidates = rp.generate_candidates(_constraints(), [], top_k=5, seed=7)
    assert len(candidates) == 5
    for c in candidates:
        assert len(c.passphrase.split("-")) == 4
        assert c.probability < 0.5


def test_generate_candidates_deterministic() -> None:
    a = rp.generate_candidates(_constraints(), ["abc99"], top_k=5, seed=3)
    b = rp.generate_candidates(_constraints(), ["abc99"], top_k=5, seed=3)
    assert [c.passphrase for c in a] == [c.passphrase for c in b]


def test_generate_candidates_format_respected() -> None:
    candidates = rp.generate_candidates(_constraints(), ["abc99-de12f-gh1jk-lmn0p", "zzz"], top_k=10, seed=1)
    for c in candidates:
        assert rp._matches_format(c.passphrase, _constraints())


async def test_reconstruct_from_db() -> None:
    db = Database()
    await db.initialize(":memory:")
    try:
        await db.execute(
            "INSERT INTO intel (id, session_id, kind, text, note, ts) VALUES (?, ?, ?, ?, ?, ?)",
            ("i1", "s1", "leak", f"candidate is {_HINT}", "", "2026-08-01T00:00:00+00:00"),
        )
        await db.execute(
            "INSERT INTO intel (id, session_id, kind, text, note, ts) VALUES (?, ?, ?, ?, ?, ?)",
            ("i2", "s1", "leak", "segment abc99 only", "", "2026-08-01T00:00:00+00:00"),
        )
        await db.commit()
        candidates = await rp.reconstruct_from_db(db)
        assert candidates
        assert candidates[0].passphrase == _HINT
    finally:
        await db.close()


async def test_reconstruct_from_db_empty() -> None:
    db = Database()
    await db.initialize(":memory:")
    try:
        candidates = await rp.reconstruct_from_db(db)
        assert isinstance(candidates, list)
    finally:
        await db.close()


def test_render_candidates() -> None:
    candidates = rp.generate_candidates(_constraints(), [_HINT], top_k=3, seed=7)
    rendered = rp.render_candidates(candidates)
    assert _HINT in rendered
    assert rp.render_candidates([]) == "(nessun candidato)"


def test_cli_main_memory_db(tmp_path) -> None:
    out = str(tmp_path / "candidates.json")
    rc = rp.main(["--db", ":memory:", "--top-k", "3", "--output", out])
    assert rc == 0
    assert Path(out).exists()
