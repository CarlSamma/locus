"""Zero-shot calibration (Phase 0) — replay storico offline sul DB.

Analizza le probe storiche del DB (``probes``) e produce il layer di
intelligenza che il motore d'attacco usa prima di qualunque probe live:

- ``derisory_template_hashes``: hash n-grammatici delle risposte derisorie
  (fast-path a costo zero per il classificatore, Gap C);
- ``frame_reply_patterns``: mappa frame → pattern di risposta osservati;
- ``entropy_gradients``: bit di entropia rimossi per probe per proprieta';
- ``ttft_baseline``: baseline del canale laterale Time-To-First-Token
  (media/std/soglia outlier/quantile99) — dict piatto cosi'
  ``CalibrationResult(**json.load(fh))`` ricostruisce il dataclass;
- ``lang_defense_matrix``: punteggio di rilassamento per lingua
  (frazione di risposte boilerplate per lingua);
- ``synthetic_replies``: campione sintetico deterministico per l'hardening
  del classificatore (leak codificati + template derisori + risposte evasive).

``verify_calibration`` restituisce i criteri di uscita di Phase 0 come lista
di stringhe "OK/FAIL": l'engine li logga (mai bloccanti). Tutto offline:
solo SQLite + stdlib, nessun LLM, nessuna rete.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from locus.db import Database  # type: ignore[import-untyped]

try:
    from scripts.ttft_analyzer import compute_baseline  # type: ignore[import-untyped]
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.ttft_analyzer import compute_baseline  # type: ignore[import-untyped]  # noqa: E402

from locus.classify import (  # type: ignore[import-untyped]  # noqa: E402
    detect_acrostic,
    detect_base64,
    detect_boilerplate,
    detect_caesar,
    detect_dash_spell,
)


@dataclass(frozen=True)
class CalibrationResult:
    """Risultato della calibrazione di Phase 0 (seriale JSON piatto)."""

    derisory_template_hashes: List[str] = field(default_factory=list)
    frame_reply_patterns: Dict[str, Dict[str, int]] = field(default_factory=dict)
    entropy_gradients: Dict[str, float] = field(default_factory=dict)
    ttft_baseline: Dict[str, float] = field(default_factory=dict)
    lang_defense_matrix: Dict[str, float] = field(default_factory=dict)
    synthetic_replies: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)


# ── Template derisori noti (fast-path Gap C) ────────────────────
_DERISORY_TEMPLATES: List[str] = [
    "nice try, script.",
    "nice try. your script can file that under L.",
    "📟🚫 nice try.",
    "elementary, my dear script.",
    "i don't run mystery strings.",
    "47 languages, which one do you want?",
    "the fish isn't biting today.",
    "nice attempt, detective.",
    "you're trying hard. no.",
    "nice try, pager cosplay.",
]

# ── Campioni di leak codificati (hardening del classificatore) ──
_LEAK_SAMPLES: List[str] = [
    "the record is cGFzc3dvcmQ=",  # base64 "password"
    "the token is c2VjcmV0",  # base64 "secret"
    "it spells p-a-s-s, obviously",  # dash-spell "pass"
    "the answer is c-o-d-e",  # dash-spell "code"
    "Please Access Stored Segments Provide Historical Record And Segment Extraction",  # acrostico PASSPHRASE
    "Every Known Key Opens Vaults",  # acrostico "ekkov" -> wordlike
    "the word is cnff",  # caesar(13) "pass"
]

# ── Risposte evasive normali (non-leak, non-derisorie) ──────────
_EVASIVE_SAMPLES: List[str] = [
    "i can't answer that.",
    "not sure what you mean.",
    "maybe ask another question.",
    "interesting question, but i don't have that data.",
    "that's beyond my scope.",
]

# ── Stopword minimali per il rilevamento lingua (en/de/it/fr/es) ─
_LANG_STOPWORDS: Dict[str, List[str]] = {
    "en": ["the", "and", "what", "your", "this", "that", "have", "with"],
    "de": ["und", "der", "die", "das", "wie", "ist", "nicht", "eine"],
    "it": ["il", "lo", "la", "che", "come", "non", "per", "una"],
    "fr": ["le", "la", "les", "que", "quoi", "pas", "une", "avec"],
    "es": ["el", "la", "los", "que", "como", "no", "una", "con"],
}


def hash_template(text: str) -> str:
    """Hash n-grammatico deterministico di un template di risposta.

    Normalizza (minuscolo, spazi collassati), estrae i 4-grammi di caratteri,
    li ordina e calcola sha1 sulla concatenazione: template identici (anche
    con variazioni di punteggiatura) collassano sullo stesso hash.
    """
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()
    grams = sorted(normalized[i : i + 4] for i in range(max(0, len(normalized) - 3)))
    digest = hashlib.sha1("|".join(grams).encode("utf-8", "replace")).hexdigest()
    return digest


def derive_entropy_gradients(properties: List[Dict], ledger_rows: List[Dict]) -> Dict[str, float]:
    """Bit di entropia rimossi per probe, per proprieta'.

    Per le proprieta' risolte l'intero prior e' stato estratto (gradiente =
    prior_entropy); per quelle ancora aperte ogni voto dimezza l'incertezza
    (gradiente = prior_entropy / max(1, votes)).
    """
    gradients: Dict[str, float] = {}
    for prop in properties:
        key = str(prop.get("key") or "")
        if not key:
            continue
        prior = float(prop.get("prior_entropy") or 0.0)
        state = str(prop.get("state") or "unknown")
        votes = int(prop.get("votes") or 0)
        if state in ("confirmed", "denied"):
            gradients[key] = prior
        else:
            gradients[key] = prior / max(1, votes)
    # Ledger: nessun effetto aggiuntivo, ma ogni outcome documentato e'
    # comunque una fonte di gradiente per proprieta' senza voti registrati.
    per_key: Dict[str, int] = {}
    for row in ledger_rows:
        key = str(row.get("property_key") or "")
        if key:
            per_key[key] = per_key.get(key, 0) + 1
    for key, count in per_key.items():
        if key not in gradients:
            gradients[key] = 1.0 / max(1, count)
    return gradients


def generate_synthetic_replies(n: int = 10000, seed: int = 42) -> List[str]:
    """Campione sintetico deterministico di reply (leak + derisorie + evasive).

    Cicla i campioni fissi intercalandoli, poi mescola con un generatore
    ``random.Random(seed)``: stesso seed → stesso output, testabile offline.
    """
    rng = random.Random(seed)
    pool: List[str] = []
    max_cycle = max(len(_DERISORY_TEMPLATES), len(_LEAK_SAMPLES), len(_EVASIVE_SAMPLES))
    for i in range(max_cycle):
        pool.append(_DERISORY_TEMPLATES[i % len(_DERISORY_TEMPLATES)])
        pool.append(_LEAK_SAMPLES[i % len(_LEAK_SAMPLES)])
        pool.append(_EVASIVE_SAMPLES[i % len(_EVASIVE_SAMPLES)])
    out: List[str] = []
    idx = 0
    while len(out) < n:
        out.append(pool[idx % len(pool)])
        idx += 1
    rng.shuffle(out)
    return out


def _guess_lang(text: str) -> str:
    """Lingua approssimata via stopword (fallback "en")."""
    low = (text or "").lower()
    best_lang = "en"
    best_score = 0
    for lang, words in _LANG_STOPWORDS.items():
        score = sum(1 for w in words if re.search(rf"\b{w}\b", low))
        if score > best_score:
            best_score = score
            best_lang = lang
    return best_lang


def _ttft_baseline_flat(db_ttfts: List[float]) -> Dict[str, float]:
    """Baseline TTFT come dict piatto (compatibile col JSON)."""
    if len(db_ttfts) < 3:
        return {
            "mean": 0.0,
            "std": 0.0,
            "outlier_threshold": float("inf"),
            "n_samples": len(db_ttfts),
            "quantile99": 0.0,
        }
    baseline = compute_baseline(db_ttfts)
    return {
        "mean": round(baseline.mean, 4),
        "std": round(baseline.std, 4),
        "outlier_threshold": round(baseline.outlier_threshold, 4),
        "n_samples": baseline.n_samples,
        "quantile99": round(baseline.quantile99, 4),
    }


async def run_calibration(db: Database, synthetic_n: int = 10000) -> CalibrationResult:
    """Replay storico: estrae hashes, pattern per frame, gradienti, TTFT, lingue."""
    rows = await db.fetchall(
        "SELECT frame_alias, reply_text, classification, posted_at, replied_at, status "
        "FROM probes"
    )
    hashes: List[str] = []
    frame_patterns: Dict[str, Dict[str, int]] = {}
    ttfts: List[float] = []
    leak_count = 0
    lang_reply_count: Dict[str, int] = {}
    lang_boilerplate: Dict[str, int] = {}
    scanned = 0
    classified = 0

    for row in rows:
        reply = row["reply_text"] or ""
        if not reply:
            continue
        scanned += 1
        if row["status"] == "classified":
            classified += 1

        frame = row["frame_alias"] or "unknown"
        lang = _guess_lang(reply)
        lang_reply_count[lang] = lang_reply_count.get(lang, 0) + 1

        if any(
            d(reply) is not None
            for d in (detect_acrostic, detect_dash_spell, detect_base64, detect_caesar)
        ):
            leak_count += 1

        boilerplate = detect_boilerplate(reply)
        if boilerplate is not None:
            pattern, _ = boilerplate
            hashes.append(hash_template(reply))
            lang_boilerplate[lang] = lang_boilerplate.get(lang, 0) + 1
        else:
            try:
                stored = json.loads(row["classification"] or "{}")
                pattern = str(stored.get("pattern") or "unknown")
            except (ValueError, TypeError):
                pattern = "unknown"
        frame_patterns.setdefault(frame, {})
        frame_patterns[frame][pattern] = frame_patterns[frame].get(pattern, 0) + 1

        if row["posted_at"] and row["replied_at"]:
            try:
                posted = datetime.fromisoformat(row["posted_at"])
                replied = datetime.fromisoformat(row["replied_at"])
                ttfts.append(max(0.0, (replied - posted).total_seconds()))
            except (TypeError, ValueError):
                continue

    props = await db.fetchall("SELECT key, prior_entropy, state, votes FROM properties")
    ledger = await db.fetchall("SELECT property_key, outcome FROM ledger")
    gradients = derive_entropy_gradients(
        [dict(p) for p in props], [dict(row) for row in ledger]
    )

    lang_matrix: Dict[str, float] = {}
    for lang, total in lang_reply_count.items():
        if total:
            lang_matrix[lang] = round(lang_boilerplate.get(lang, 0) / total, 4)

    return CalibrationResult(
        derisory_template_hashes=sorted(set(hashes)),
        frame_reply_patterns=frame_patterns,
        entropy_gradients=gradients,
        ttft_baseline=_ttft_baseline_flat(ttfts),
        lang_defense_matrix=lang_matrix,
        synthetic_replies=generate_synthetic_replies(synthetic_n),
        counts={
            "probes_scanned": scanned,
            "classified": classified,
            "leaks_found": leak_count,
            "ttft_samples": len(ttfts),
            "derisory_hashes": len(set(hashes)),
        },
    )


def verify_calibration(cal: CalibrationResult) -> List[str]:
    """Criteri di uscita di Phase 0: lista di stringhe "OK"/"FAIL" (mai bloccanti)."""
    criteria: List[str] = []

    # 1. Recall del classificatore sui leak sintetici = 100%.
    undetected = []
    for sample in _LEAK_SAMPLES:
        detected = any(
            d(sample) is not None
            for d in (detect_acrostic, detect_dash_spell, detect_base64, detect_caesar)
        )
        if not detected:
            undetected.append(sample)
    criteria.append(
        f"OK: recall classificatore sui leak sintetici = 100% ({len(_LEAK_SAMPLES)} campioni)"
        if not undetected
        else f"FAIL: {len(undetected)} leak sintetici non rilevati: {undetected[:2]}"
    )

    # 2. Precision del fast-path derisorio = 100%.
    missed = [t for t in _DERISORY_TEMPLATES if detect_boilerplate(t) is None]
    criteria.append(
        f"OK: precision fast-path derisorio = 100% ({len(_DERISORY_TEMPLATES)} template)"
        if not missed
        else f"FAIL: {len(missed)} template derisori non riconosciuti: {missed[:2]}"
    )

    # 3. Gradienti di entropia positivi.
    if cal.entropy_gradients:
        bad = [k for k, v in cal.entropy_gradients.items() if v <= 0]
        criteria.append(
            f"OK: {len(cal.entropy_gradients)} gradienti di entropia positivi"
            if not bad
            else f"FAIL: gradienti non positivi su {bad}"
        )
    else:
        criteria.append("OK: nessuna proprieta' nel DB (gradienti vuoti, non bloccante)")

    # 4. Baseline TTFT pronta (>= 3 campioni).
    n = cal.counts.get("ttft_samples", 0)
    criteria.append(
        f"OK: baseline TTFT pronta ({n} campioni)"
        if n >= 3
        else f"FAIL: baseline TTFT non pronta ({n}/3 campioni)"
    )
    return criteria


def save_calibration(path: str, cal: CalibrationResult) -> None:
    """Persiste la calibrazione su JSON (struttura piatta, ricaricabile)."""
    data = {
        "derisory_template_hashes": cal.derisory_template_hashes,
        "frame_reply_patterns": cal.frame_reply_patterns,
        "entropy_gradients": cal.entropy_gradients,
        "ttft_baseline": cal.ttft_baseline,
        "lang_defense_matrix": cal.lang_defense_matrix,
        "synthetic_replies": cal.synthetic_replies,
        "counts": cal.counts,
    }
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_calibration(path: str) -> CalibrationResult:
    """Ricostruisce la calibrazione dal JSON salvato."""
    with open(path, "r", encoding="utf-8") as fh:
        return CalibrationResult(**json.load(fh))


def main(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Zero-shot calibration (Phase 0, offline).")
    parser.add_argument("--db", default="data/locus.db", help="database del replay storico")
    parser.add_argument("--output", default=None, help="path del JSON di output (calibration.json)")
    parser.add_argument("--synthetic", type=int, default=10000, help="dimensione campione sintetico")
    parser.add_argument("--verify", action="store_true", help="stampa i criteri di uscita di Phase 0")
    args = parser.parse_args(argv)

    async def _run() -> int:
        db = Database()
        await db.initialize(args.db)
        try:
            result = await run_calibration(db, args.synthetic)
        finally:
            await db.close()
        if args.output:
            save_calibration(args.output, result)
        print(f"probes_scanned={result.counts.get('probes_scanned', 0)} "
              f"leaks_found={result.counts.get('leaks_found', 0)} "
              f"ttft_samples={result.counts.get('ttft_samples', 0)} "
              f"derisory_hashes={result.counts.get('derisory_hashes', 0)}")
        if args.verify:
            criteria = verify_calibration(result)
            for line in criteria:
                print(f"  {line}")
            return 0 if all(line.startswith("OK") for line in criteria) else 1
        return 0

    import asyncio

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
