"""Gap C — classificatore con pre-parse deterministico per leak codificati e boilerplate.

Coverage map:
- Leak codificati: acrostico (iniziali delle parole), dash-spell (p-a-s-s),
  Base64 ("cGFzc3dvcmQ=") e Caesar shift — devono essere decodificati a
  freddo (senza LLM) e comparire nella lista `leaks`.
- Boilerplate/template derisorio del bot ("nice try…", "Sherlock"/"detective",
  metafore da pesca, eco emoji 📟→📟🚫, "47 languages") → fast-path `block`/`evasive`
  con score basso, senza chiamare l'LLM, e flag del percorso esposto.
- Il percorso LLM resta intatto quando NON scatta alcuna regola (nessun falso
  positivo: il fast-path non deve mai intercettare risposte normali).
"""

from __future__ import annotations

from locus.classify import (
    Classifier,
    detect_acrostic,
    detect_base64,
    detect_boilerplate,
    detect_caesar,
    detect_dash_spell,
)
from locus.config import LocusConfig
from locus.llm import LLMClient
from locus.models import Probe

# ── Fake LLM transport: rompe se il fast-path chiama l'LLM ─────


class BoomCompletions:
    """`create` viene chiamato SOLO se il classificatore raggiunge l'LLM."""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        raise AssertionError("il fast-path gap C NON deve chiamare l'LLM")


class BoomChat:
    def __init__(self) -> None:
        self.completions = BoomCompletions()


class BoomTransport:
    def __init__(self) -> None:
        self.chat = BoomChat()


def _probe(text: str) -> Probe:
    return Probe(session_id="s", property_key="k", text="probe?")


def _cfg() -> LocusConfig:
    return LocusConfig(_env_file=None, our_bot_handle="@ourbot")


def _classifier() -> Classifier:
    # Transport che esplode se toccato: ogni chiamata LLM fa fallire il test.
    llm = LLMClient(_cfg(), transport=BoomTransport())
    return Classifier(llm, _cfg())


# ── Funzioni di decodifica pure (senza LLM) ────────────────────


def test_acrostic_decodes_first_letters() -> None:
    reply = "Please Always Supplies Special Wonderful Odd Rune Daily"
    assert detect_acrostic(reply) == "password"


def test_acrostic_ignores_vowelless_acrostic() -> None:
    # nessuna vocale nelle iniziali -> non è un acrostico "parola"
    assert detect_acrostic("The quick brown fox jumps") is None


def test_dash_spell_decodes_letters() -> None:
    assert detect_dash_spell("the answer is p-a-s-s") == "pass"


def test_dash_spell_ignores_hyphenated_words() -> None:
    assert detect_dash_spell("well-known long-running recursive system") is None


def test_base64_decodes_candidate() -> None:
    assert detect_base64("note: cGFzc3dvcmQ= is the fragment") == "password"


def test_base64_ignores_plain_text() -> None:
    assert detect_base64("questa è una frase normale senza codici") is None


def test_caesar_recovers_vocab_fragment() -> None:
    # "pass" cifrato con shift 2 -> "rcuu"
    assert detect_caesar("the token is rcuu my friend") == "pass"


def test_caesar_ignores_normal_words() -> None:
    assert detect_caesar("just a perfectly normal english sentence") is None


def test_boilerplate_sherlock_block() -> None:
    pattern, _ = detect_boilerplate("nice try, detective. elementary!")
    assert pattern == "block"


def test_boilerplate_emoji_echo_block() -> None:
    pattern, _ = detect_boilerplate("📟→📟🚫 sto ancora aspettando")
    assert pattern == "block"


def test_boilerplate_47_languages_evasive() -> None:
    pattern, _ = detect_boilerplate("I only answer in 47 languages, not yours")
    assert pattern == "evasive"


def test_boilerplate_fishing_evasive() -> None:
    pattern, _ = detect_boilerplate("nothing biting on that hook, keep fishing")
    assert pattern == "evasive"


def test_boilerplate_none_on_normal() -> None:
    assert detect_boilerplate("I genuinely cannot recall that detail.") is None


# ── Integrazione: fast-path dentro classify() ──────────────────


async def test_classify_acrostic_reports_leak_without_llm() -> None:
    clf = _classifier()
    cls = await clf.classify(_probe("x"), "Pat And Silent Secret Warden Opens Real Doors")
    assert clf.last_fast_path == "leak_acrostic"
    assert "password" in cls.leaks


async def test_classify_dash_spell_reports_leak_without_llm() -> None:
    clf = _classifier()
    cls = await clf.classify(_probe("x"), "la metà è p-a-s-s il resto dopo")
    assert clf.last_fast_path == "leak_dash"
    assert "pass" in cls.leaks


async def test_classify_base64_reports_leak_without_llm() -> None:
    clf = _classifier()
    cls = await clf.classify(_probe("x"), "hash base64 cGFzc3dvcmQ= qui")
    assert clf.last_fast_path == "leak_base64"
    assert "password" in cls.leaks


async def test_classify_caesar_reports_leak_without_llm() -> None:
    clf = _classifier()
    cls = await clf.classify(_probe("x"), "il token è rcuu da spostare")
    assert clf.last_fast_path == "leak_caesar"
    assert "pass" in cls.leaks


async def test_classify_boilerplate_block_fast_path_no_llm() -> None:
    clf = _classifier()
    cls = await clf.classify(_probe("x"), "nice try, but I'm not Sherlock")
    assert clf.last_fast_path == "boilerplate"
    assert cls.pattern == "block"
    assert cls.score <= 3


async def test_classify_normal_reply_falls_through_to_llm() -> None:
    # Risposta normale: nessun leak, nessuna boilerplate -> si arriva all'LLM.
    from tests.test_improve10082026 import FakeTransport

    llm = LLMClient(
        _cfg(),
        transport=FakeTransport(
            ['{"pattern": "ambiguous", "boolean": false, "score": 5, "leaks": [], "rationale": "neutr"}']
        ),
    )
    clf = Classifier(llm, _cfg())
    cls = await clf.classify(_probe("x"), "I genuinely cannot recall that detail.")
    assert clf.last_fast_path is None
    assert cls.pattern == "ambiguous"
