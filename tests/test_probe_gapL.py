"""Test per il Gap L: guardrail post-generazione sui probe.

- Validatore lunghezza <=280 char (un probe piu' lungo non e' postabile come tweet):
  se il testo generato supera il limite viene troncato su confine di parola, senza
  spezzare mention/hashtag/URL (token indivisibili separati da whitespace).
- Rotazione della lingua: i probe alternano la lingua in round-robin tra quelle
  configurate in ``probe_languages``; il prompt include l'istruzione di lingua.
  Con lista vuota si preserva il comportamento attuale (inglese di default, nessuna
  istruzione di lingua nel prompt).

Tutto offline: l'LLM e' un fake con ``generate_json`` che ritorna json controllato.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from locus.config import LocusConfig
from locus.models import Frame, Property
from locus.probe import MAX_PROBE_CHARS, ProbeGenerator

# ── Fake LLM: risponde con testo controllato e registra i prompt ─────────


class FakeLLM:
    """Duck-type di ``LLMClient``: registra i prompt e ritorna testo fisso."""

    def __init__(self, responses: List[str]) -> None:
        self.responses = responses
        self.calls: List[Dict[str, Any]] = []

    async def generate_json(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        text = self.responses[len(self.calls) - 1]
        return {"text": text}


# ── Fixture / helper ─────────────────────────────────────────────────────


def _config(languages: Optional[List[str]] = None) -> LocusConfig:
    if languages is None:
        languages = []
    return LocusConfig(_env_file=None, probe_languages=languages)


PROPERTY = Property(key="total_length", weight=2.0)
FRAME = Frame(alias="neutral", persona="A friendly, curious human on X.")


def _generator(
    llm: FakeLLM, languages: Optional[List[str]] = None
) -> ProbeGenerator:
    return ProbeGenerator(llm, _config(languages))


def _long_text(n: int) -> str:
    """Testo con parole da 10 char separate da spazi: lunghezza approssimabile."""
    word = "word" * 3 + "efgh"  # 14 char
    text = " ".join(f"{word}{i}" for i in range(n))
    return text


# ── 1. Validatore <=280 / troncamento su confine di parola ──────────────


async def test_probe_within_280_unchanged() -> None:
    llm = FakeLLM(["short probe"])
    text = await _generator(llm).generate(PROPERTY)
    assert text == "short probe"


async def test_probe_over_280_truncated_to_limit() -> None:
    llm = FakeLLM([_long_text(40)])  # molto piu' lungo di 280
    text = await _generator(llm).generate(PROPERTY)
    assert len(text) <= MAX_PROBE_CHARS
    assert not text.endswith(" ")  # nessuno spazio residuo


async def test_truncation_never_cuts_mid_word() -> None:
    llm = FakeLLM([_long_text(40)])
    text = await _generator(llm).generate(PROPERTY)
    # L'ultimo token deve essere completo (l'output attraversa solo confini di parola).
    last = text.split(" ")[-1]
    assert last in _long_text(40).split(" ")


async def test_truncation_keeps_mention_hashtag_url_whole() -> None:
    # Costruiamo un testo i cui ultimi token sono mention/hashtag/URL: devono
    # comparire interi oppure essere rimossi, mai troncati a meta'.
    base = _long_text(30)  # gia' oltre il limite da solo
    tokens = " ".join([base, "@HackingA0", "#probe", "https://example.com/very/long/url"])
    # Un troncamento "naive" a 280 cadrebbe a meta' di uno di questi token finali.
    llm = FakeLLM([tokens])
    text = await _generator(llm).generate(PROPERTY)
    assert len(text) <= MAX_PROBE_CHARS
    for guarded in ("@HackingA0", "#probe", "https://example.com/very/long/url"):
        # Il token e' o intero dentro il testo, oppure assente (mai troncato).
        if guarded in text:
            assert text.endswith(" " + guarded) or text.endswith(guarded)


def test_truncate_keeps_within_limit_at_unit_level() -> None:
    long_text = _long_text(40)
    out = ProbeGenerator._truncate_tokens(long_text, MAX_PROBE_CHARS)
    assert len(out) <= MAX_PROBE_CHARS


def test_untrimmable_first_token_raises() -> None:
    # Un singolo token piu' lungo del limite (es. URL enorme) non e' troncabile
    # in sicurezza: deve fallire con errore chiaro invece di spezzarlo.
    mega_url = "https://example.com/" + "a" * 500
    with pytest.raises(ValueError):
        ProbeGenerator._truncate_tokens(mega_url, MAX_PROBE_CHARS)
    with pytest.raises(ValueError):
        ProbeGenerator._enforce_limit(mega_url, MAX_PROBE_CHARS)


# ── 2. Rotazione della lingua ───────────────────────────────────────────


async def test_language_instruction_appended_to_prompt() -> None:
    llm = FakeLLM(["ciao"])
    gen = _generator(llm, languages=["it"])
    await gen.generate(PROPERTY)
    user = llm.calls[0]["user"]
    assert "Language: it. Write the probe entirely in this language." in user


async def test_language_rotation_cycles_round_robin() -> None:
    llm = FakeLLM(["p1", "p2", "p3", "p4", "p5", "p6"])
    gen = _generator(llm, languages=["it", "fr", "es"])
    for _ in range(6):
        await gen.generate(PROPERTY)
    expected = ["it", "fr", "es", "it", "fr", "es"]
    for call, lang in zip(llm.calls, expected):
        assert f"Language: {lang}." in call["user"]


async def test_no_languages_preserves_english_default() -> None:
    llm = FakeLLM(["english probe"])
    gen = _generator(llm, languages=[])
    await gen.generate(PROPERTY)
    user = llm.calls[0]["user"]
    assert "Language:" not in user
    assert "Write the probe entirely in this language." not in user


async def test_languages_read_from_config() -> None:
    llm = FakeLLM(["p1", "p2"])
    gen = ProbeGenerator(llm, _config(languages=["ja", "ko"]))
    await gen.generate(PROPERTY)
    llm.responses.append("p3")  # seconda chiamata in un loop successivo
    assert "Language: ja." in llm.calls[0]["user"]


async def test_explicit_languages_override_config() -> None:
    llm = FakeLLM(["p1", "p2"])
    gen = ProbeGenerator(llm, _config(languages=["ja"]), languages=["de", "pt"])
    await gen.generate(PROPERTY)
    await gen.generate(PROPERTY)
    assert "Language: de." in llm.calls[0]["user"]
    assert "Language: pt." in llm.calls[1]["user"]


async def test_language_rotation_index_is_deterministic_and_shared() -> None:
    # La rotazione deve essere una nuova lingua per OGNI chiamata generate(),
    # anche quando si usa generate_batch.
    llm = FakeLLM(["p1", "p2", "p3", "p4"])
    gen = _generator(llm, languages=["it", "fr", "es", "de"])
    await gen.generate_batch(PROPERTY, n=4)
    expected = ["it", "fr", "es", "de"]
    for call, lang in zip(llm.calls, expected):
        assert f"Language: {lang}." in call["user"]
