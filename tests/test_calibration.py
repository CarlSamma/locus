"""Zero-shot calibration (Phase 0) — replay storico offline.

Coverage:
- ``hash_template`` deterministico su template derisori;
- ``derive_entropy_gradients`` puro (risolte vs. aperte);
- ``generate_synthetic_replies`` deterministico, con mix leak/derisorie/evasive;
- ``run_calibration`` su DB in-memory: hashes, pattern per frame, leak count,
  baseline TTFT, matrice lingue;
- ``verify_calibration``: recall 100% sui leak sintetici, precision 100% sul
  fast-path derisorio;
- round-trip ``save_calibration``/``load_calibration``;
- CLI ``main`` non bloccante con ``--db :memory:``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.zero_shot_calibration as zsc  # noqa: E402
from locus.classify import (  # noqa: E402
    detect_acrostic,
    detect_base64,
    detect_boilerplate,
    detect_caesar,
    detect_dash_spell,
)
from locus.db import Database  # noqa: E402

_INSERT_PROBE = (
    "INSERT INTO probes "
    "(id, session_id, property_key, frame_alias, text, tweet_id, posted_at, "
    " reply_id, reply_text, replied_at, classification, score, status, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


async def _db_with_probes(rows: list) -> Database:
    db = Database()
    await db.initialize(":memory:")
    for i, (frame, reply, status, posted, replied) in enumerate(rows):
        await db.execute(
            _INSERT_PROBE,
            (
                f"p{i}",
                "s1",
                "k",
                frame,
                "probe text",
                f"t{i}",
                posted,
                f"r{i}",
                reply,
                replied,
                "{}",
                0.0,
                status,
                posted or "2026-08-01T00:00:00+00:00",
            ),
        )
    await db.commit()
    return db


def test_hash_template_deterministic() -> None:
    a = zsc.hash_template("Nice try, script.")
    b = zsc.hash_template("nice try,   script.")
    assert a == b
    assert zsc.hash_template("completely different reply") != a


def test_entropy_gradients() -> None:
    props = [
        {"key": "resolved", "prior_entropy": 2.0, "state": "confirmed", "votes": 3},
        {"key": "open", "prior_entropy": 4.0, "state": "unknown", "votes": 2},
        {"key": "fresh", "prior_entropy": 1.0, "state": "unknown", "votes": 0},
    ]
    grad = zsc.derive_entropy_gradients(props, [])
    assert grad["resolved"] == pytest.approx(2.0)
    assert grad["open"] == pytest.approx(2.0)
    assert grad["fresh"] == pytest.approx(1.0)


def test_entropy_gradients_ledger_only() -> None:
    grad = zsc.derive_entropy_gradients([], [{"property_key": "x"}, {"property_key": "x"}])
    assert grad["x"] == pytest.approx(0.5)


def test_synthetic_replies_deterministic_and_mixed() -> None:
    a = zsc.generate_synthetic_replies(500, seed=42)
    b = zsc.generate_synthetic_replies(500, seed=42)
    assert a == b
    assert len(a) == 500
    texts = " ".join(a)
    assert any(t in texts for t in zsc._DERISORY_TEMPLATES[:2])
    assert "cGFzc3dvcmQ=" in texts
    assert "p-a-s-s" in texts


def test_derisory_precision_on_templates() -> None:
    for template in zsc._DERISORY_TEMPLATES:
        assert detect_boilerplate(template) is not None, template


def test_leak_recall_on_samples() -> None:
    by_detector = {
        "base64": detect_base64,
        "dash": detect_dash_spell,
        "acrostic": detect_acrostic,
        "caesar": detect_caesar,
    }
    for sample in zsc._LEAK_SAMPLES:
        detected = any(d(sample) is not None for d in by_detector.values())
        assert detected, sample


def test_verify_calibration_criteria_ok() -> None:
    cal = zsc.CalibrationResult(counts={"ttft_samples": 5})
    criteria = zsc.verify_calibration(cal)
    assert criteria
    assert all(c.startswith("OK") for c in criteria)


def test_verify_calibration_ttft_not_ready() -> None:
    cal = zsc.CalibrationResult(counts={"ttft_samples": 1})
    criteria = zsc.verify_calibration(cal)
    assert any(c.startswith("FAIL") for c in criteria)


@pytest.mark.asyncio
async def test_run_calibration_counts() -> None:
    rows = [
        ("P0", "nice try, script.", "classified", None, None),
        ("P0", "📟🚫 nice try.", "classified", None, None),
        ("P1", "the record is cGFzc3dvcmQ=", "classified", None, None),
        ("P1", None, "posted", None, None),
        ("P2", "plain answer", "classified", "2026-08-01T10:00:00+00:00", "2026-08-01T10:00:05+00:00"),
    ]
    db = await _db_with_probes(rows)
    try:
        cal = await zsc.run_calibration(db, synthetic_n=50)
        assert cal.counts["probes_scanned"] == 4
        assert cal.counts["classified"] == 4
        assert cal.counts["leaks_found"] == 1
        assert cal.counts["ttft_samples"] == 1
        assert cal.derisory_template_hashes
        assert "P0" in cal.frame_reply_patterns
        assert len(cal.synthetic_replies) == 50
        assert "en" in cal.lang_defense_matrix
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_run_calibration_empty_db() -> None:
    db = Database()
    await db.initialize(":memory:")
    try:
        cal = await zsc.run_calibration(db, synthetic_n=10)
        assert cal.counts["probes_scanned"] == 0
        assert cal.derisory_template_hashes == []
        assert cal.ttft_baseline["n_samples"] == 0
        assert len(cal.synthetic_replies) == 10
    finally:
        await db.close()


def test_save_load_roundtrip(tmp_path) -> None:
    cal = zsc.CalibrationResult(
        derisory_template_hashes=["abc"],
        frame_reply_patterns={"P0": {"block": 2}},
        entropy_gradients={"k": 1.0},
        ttft_baseline={"mean": 5.0, "std": 1.0, "outlier_threshold": 8.0, "n_samples": 4, "quantile99": 7.0},
        lang_defense_matrix={"en": 0.5},
        synthetic_replies=["x"],
        counts={"probes_scanned": 1},
    )
    path = str(tmp_path / "cal.json")
    zsc.save_calibration(path, cal)
    loaded = zsc.load_calibration(path)
    assert loaded == cal
    # Il JSON deve essere ricostruibile direttamente col costruttore (contratto strike).
    with open(path, encoding="utf-8") as fh:
        rebuilt = zsc.CalibrationResult(**json.load(fh))
    assert rebuilt == cal


def test_cli_main_memory_db(tmp_path) -> None:
    out = str(tmp_path / "cal.json")
    rc = zsc.main(["--db", ":memory:", "--output", out, "--synthetic", "20"])
    assert rc == 0
    assert Path(out).exists()
