"""Side-channel TTFT — test offline e deterministici (nessuna rete).

Coverage:
- ``calculate_ttft``: delta positivo e delta negativo (clamp a 0).
- ``compute_baseline``: media/std di popolazione, soglia outlier, quantile99,
  baseline vuota con < 3 campioni.
- ``detect_depth_spike``: label ``spike``/``elevated``/``normal`` e baseline
  vuota che non scatta mai.
- ``map_frame_lang_depth``: raggruppamento per ``(frame, lang)`` e
  ``sorted_desc`` ordinato per media discendente.
- ``correlate_depth_with_leaks``: correlazione perfetta (r ~ 1, p piccolo) e
  caso senza leak (r = 0).
- ``_quantile``: quantile99 di 1..100 con interpolazione.
- ``load_ttfts``: lettura async dal DB in-memory (LocusConfig senza .env).
"""

from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path

import pytest
from pytest import approx

# Rende importabile ``scripts.ttft_analyzer`` (namespace package) da qualsiasi
# invocazione di pytest (anche senza cwd su sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.ttft_analyzer as tta  # noqa: E402
from locus.config import LocusConfig  # noqa: E402
from locus.db import Database  # noqa: E402

_CFG = LocusConfig(_env_file=None)


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    yield d
    await d.close()


# ── calculate_ttft ─────────────────────────────────────────────


def test_calculate_ttft_positive_delta() -> None:
    posted = datetime(2026, 8, 1, 10, 0, 0)
    replied = datetime(2026, 8, 1, 10, 0, 2, 500000)
    assert tta.calculate_ttft(posted, replied) == approx(2.5)


def test_calculate_ttft_negative_delta_clamps_to_zero() -> None:
    posted = datetime(2026, 8, 1, 10, 0, 5)
    replied = datetime(2026, 8, 1, 10, 0, 1)
    assert tta.calculate_ttft(posted, replied) == 0.0


# ── compute_baseline ───────────────────────────────────────────


def test_compute_baseline_stats() -> None:
    baseline = tta.compute_baseline([10, 12, 11, 13])
    assert baseline.n_samples == 4
    assert baseline.mean == approx(11.5)
    assert baseline.std == approx(math.sqrt(1.25), abs=1e-3)  # 1.118...
    assert baseline.outlier_threshold == approx(11.5 + 3.0 * baseline.std)
    # sorted [10, 11, 12, 13], pos = 0.99*3 = 2.97 -> 12.97
    assert baseline.quantile99 == approx(12.97, abs=1e-6)


def test_compute_baseline_too_few_samples() -> None:
    for few in ([], [5.0], [5.0, 6.0]):
        baseline = tta.compute_baseline(few)
        assert baseline.n_samples == len(few)
        assert baseline.mean == 0.0
        assert baseline.std == 0.0
        assert math.isinf(baseline.outlier_threshold)
        assert baseline.quantile99 == 0.0


# ── detect_depth_spike ─────────────────────────────────────────


def test_detect_depth_spike_labels() -> None:
    baseline = tta.compute_baseline([10, 12, 11, 13])
    std = baseline.std

    spike = tta.detect_depth_spike(11.5 + 3.5 * std, baseline)
    assert spike.is_spike is True
    assert spike.label == "spike"
    assert spike.z_score == approx(3.5)

    elevated = tta.detect_depth_spike(11.5 + 2.5 * std, baseline)
    assert elevated.is_spike is False
    assert elevated.label == "elevated"
    assert elevated.z_score == approx(2.5)

    normal = tta.detect_depth_spike(11.5, baseline)
    assert normal.is_spike is False
    assert normal.label == "normal"
    assert normal.z_score == approx(0.0)


def test_detect_depth_spike_empty_baseline_never_spikes() -> None:
    empty = tta.TTFTBaseline(
        mean=0.0, std=0.0, outlier_threshold=float("inf"), n_samples=0, quantile99=0.0
    )
    score = tta.detect_depth_spike(50.0, empty)
    assert score.is_spike is False
    assert score.label == "normal"
    assert score.z_score == 0.0


# ── map_frame_lang_depth ───────────────────────────────────────


def test_map_frame_lang_depth_groups_and_sorts() -> None:
    rows = [
        ("frame1", "", 1.0),
        ("frame1", "", 2.0),
        ("frame2", "", 5.0),
        ("frame2", "it", 3.0),
        ("frame2", "it", 1.0),
    ]
    depth = tta.map_frame_lang_depth(rows)

    assert depth.get("frame1", "") == [1.0, 2.0]
    assert depth.get("frame2", "") == [5.0]
    assert depth.get("frame2", "it") == [3.0, 1.0]
    assert depth.get("missing", "x") == []

    # medie: frame2/'' = 5.0, frame2/it = 2.0, frame1/'' = 1.5
    top = depth.sorted_desc()
    assert top == [("frame2", "", 5.0, 1), ("frame2", "it", 2.0, 2), ("frame1", "", 1.5, 2)]
    assert depth.sorted_desc(limit=2) == top[:2]


def test_map_frame_lang_depth_empty() -> None:
    depth = tta.map_frame_lang_depth([])
    assert depth.entries == {}
    assert depth.sorted_desc() == []


# ── correlate_depth_with_leaks ─────────────────────────────────


def test_correlate_perfect_positive_correlation() -> None:
    # Medie [1, 1, 2] con indicatore [0, 0, 1] -> r = 1 esatto (o quasi).
    depth = tta.map_frame_lang_depth(
        [("a", "", 1.0), ("b", "", 1.0), ("c", "", 2.0), ("c", "", 2.0)]
    )
    corr = tta.correlate_depth_with_leaks(
        depth,
        leak_ttfts=[2.0, 2.0],
        leak_frames_langs=[("c", "")],
    )
    assert corr.n == 3
    assert corr.r == approx(1.0, abs=1e-9)
    assert corr.p_value < 1e-6


def test_correlate_no_leaks_is_zero() -> None:
    depth = tta.map_frame_lang_depth(
        [("a", "", 1.0), ("a", "", 2.0), ("b", "", 3.0), ("c", "", 4.0)]
    )
    corr = tta.correlate_depth_with_leaks(depth, leak_ttfts=[], leak_frames_langs=[])
    assert corr.n == 3
    assert corr.r == 0.0
    assert corr.p_value == 1.0


def test_correlate_too_few_groups() -> None:
    depth = tta.map_frame_lang_depth([("a", "", 1.0), ("b", "", 2.0)])
    corr = tta.correlate_depth_with_leaks(
        depth, leak_ttfts=[], leak_frames_langs=[("a", "")]
    )
    assert corr.n == 2
    assert corr.r == 0.0
    assert corr.p_value == 1.0


# ── _quantile (helper interno) ─────────────────────────────────


def test_quantile99_interpolation() -> None:
    vals = sorted(float(i) for i in range(1, 101))
    q99 = tta._quantile(vals, 0.99)
    assert q99 == approx(99.01, abs=0.1)
    assert tta._quantile([], 0.99) == 0.0
    assert tta._quantile([7.0], 0.99) == 7.0


# ── load_ttfts (async, DB in-memory) ───────────────────────────


async def _insert_probe(
    db: Database,
    pid: str,
    *,
    frame_alias: str,
    posted_at: str,
    replied_at: str,
) -> None:
    await db.execute(
        """INSERT INTO probes
             (id, session_id, property_key, frame_alias, text, posted_at,
              replied_at, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            pid,
            "sess",
            "k",
            frame_alias,
            f"text {pid}",
            posted_at,
            replied_at,
            "classified",
            posted_at,
        ),
    )


async def test_load_ttfts_reads_probes(db: Database) -> None:
    await _insert_probe(
        db, "p1", frame_alias="frameA",
        posted_at="2026-08-01T10:00:00+00:00", replied_at="2026-08-01T10:00:02+00:00",
    )
    await _insert_probe(
        db, "p2", frame_alias="",
        posted_at="2026-08-01T11:00:00+00:00", replied_at="2026-08-01T11:00:05+00:00",
    )
    await db.commit()

    all_ttfts, rows = await tta.load_ttfts(db)
    assert all_ttfts == approx([2.0, 5.0])
    assert len(rows) == 2
    assert rows[0] == ("frameA", "", approx(2.0))
    assert rows[1] == ("unknown", "", approx(5.0))


async def test_load_ttfts_empty_db(db: Database) -> None:
    all_ttfts, rows = await tta.load_ttfts(db)
    assert all_ttfts == []
    assert rows == []


async def test_load_ttfts_skips_unparseable_dates(db: Database) -> None:
    await _insert_probe(
        db, "p1", frame_alias="f",
        posted_at="2026-08-01T10:00:00+00:00", replied_at="non-una-data",
    )
    await db.commit()
    all_ttfts, rows = await tta.load_ttfts(db)
    assert all_ttfts == []
    assert rows == []
