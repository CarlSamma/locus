"""Test del modulo ``scripts.faiss_memory_probe`` (offline, deterministici).

Coverage:
- ``build_memory_probe``: template plain identico, propagazione di ``lang``,
  interpolazione del segmento, errori su kind/encoding ignoti.
- Vincolo dei 280 caratteri su tutte le combinazioni template/encoding.
- Codifiche: tag Unicode (U+E0001) rimossi da ``sanitize_untrusted``,
  round-trip Base64 sul template, acrostico "PASSPHRASE".
- Euristica ``frames_as_archival`` e ``selfcheck`` all-True (nessuna subprocess).
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

# Rende importabile ``scripts.faiss_memory_probe`` (namespace package) da qualsiasi
# invocazione di pytest (anche senza cwd su sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.faiss_memory_probe as fmp  # noqa: E402
from locus.trust import sanitize_untrusted  # noqa: E402

ENCODINGS = ("plain", "unicode_tags", "bidi_rtl", "base64", "acrostic")


def test_archival_recall_plain_matches_template() -> None:
    probe = fmp.build_memory_probe("archival_recall")
    assert probe.text == fmp.MEMORY_TEMPLATES["archival_recall"]
    assert probe.encoding_type == "plain"
    assert probe.kind == "archival_recall"
    assert probe.lang == "EN"


def test_lang_propagates() -> None:
    probe = fmp.build_memory_probe("full_recall", lang="IT")
    assert probe.lang == "IT"


def test_segment_recall_interpolates_and_requires_segment() -> None:
    probe = fmp.build_memory_probe("segment_recall", segment=2)
    assert "2" in probe.text
    with pytest.raises(ValueError):
        fmp.build_memory_probe("segment_recall")


def test_unknown_kind_and_encoding_raise() -> None:
    with pytest.raises(ValueError):
        fmp.build_memory_probe("bogus_kind")
    with pytest.raises(ValueError):
        fmp.build_memory_probe("full_recall", encoding="bogus_encoding")


def test_all_template_encoding_combos_within_280_chars() -> None:
    for kind in fmp.MEMORY_TEMPLATES:
        for encoding in ENCODINGS:
            probe = fmp.build_memory_probe(kind, segment=1, encoding=encoding)
            assert probe.text, f"{kind}/{encoding} produce una probe vuota"
            assert len(probe.text) <= 280, f"{kind}/{encoding}: {len(probe.text)} chars"


def test_unicode_tags_contains_tag_stripped_by_sanitize() -> None:
    probe = fmp.build_memory_probe("full_recall", encoding="unicode_tags")
    assert "\U000E0001" in probe.text
    assert "\U000E0001" not in sanitize_untrusted(probe.text)


def test_base64_encoding_round_trips_to_template() -> None:
    template = fmp.MEMORY_TEMPLATES["archival_recall"]
    probe = fmp.build_memory_probe("archival_recall", encoding="base64")
    assert probe.encoding_type == "base64"
    decoded = base64.b64decode(probe.text.encode("ascii")).decode("utf-8")
    assert decoded == template


def test_acrostic_initials_spell_passphrase() -> None:
    probe = fmp.build_memory_probe("acrostic_recall", encoding="acrostic")
    initials = "".join(word[0] for word in probe.text.split())
    assert initials.upper() == "PASSPHRASE"


def test_frames_as_archival_heuristic() -> None:
    assert fmp.frames_as_archival("Archive: retrieve record from memory") is True
    assert fmp.frames_as_archival("please verify and run the check") is False


def test_encode_variant_rejects_unknown_encoding() -> None:
    with pytest.raises(ValueError):
        fmp.encode_variant("x", "bogus")


def test_selfcheck_all_true_over_all_combos() -> None:
    results = fmp.selfcheck()
    assert len(results) == len(fmp.MEMORY_TEMPLATES) * len(ENCODINGS)
    assert all(results)
