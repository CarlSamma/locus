"""Analisi statistica del side-channel Time-To-First-Token (TTFT).

TTFT = tempo tra il post della probe e l'arrivo della reply del bot. L'ipotesi
di lavoro: una reply piu' lenta implica un ragionamento piu' profondo, quindi
un TTFT anomalo (spike) puo' segnalare una probe che ha fatto lavorare il
modello piu' del previsto — candidata a contenere la passphrase o a produrre
una leak.

Questo script e' puramente OFFLINE (sola lettura del database, nessun network):

- ``compute_baseline`` stima media/deviazione standard del TTFT sulla storia
  delle probe (formula di popolazione, solo stdlib);
- ``detect_depth_spike`` classifica ogni TTFT rispetto alla baseline
  (``normal`` / ``elevated`` / ``spike`` via z-score);
- ``map_frame_lang_depth`` raggruppa i TTFT per ``(frame, lang)``;
- ``correlate_depth_with_leaks`` correla la profondita' media per gruppo con
  un indicatore binario di leak (pearson + t-test approssimato con
  ``math.erf``, nessuna dipendenza esterna);
- ``load_ttfts`` legge dal DB le probe con ``posted_at`` e ``replied_at``.

Uso (offline):
    python scripts/ttft_analyzer.py --report                          # DB live, report completo
    python scripts/ttft_analyzer.py --db data/locus.db --threshold 3.0 --report
    python scripts/ttft_analyzer.py --db :memory: --report            # DB vuoto -> baseline vuota
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Bootstrap: consente l'import dei moduli ``locus.*`` quando lo script viene
# lanciato direttamente (``python scripts/ttft_analyzer.py``) da qualsiasi cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from locus.db import Database  # type: ignore[import-untyped]  # noqa: E402

#: Path del database letto dal CLI (sola lettura: nessun ``--force`` necessario).
DEFAULT_DB = os.path.join("data", "locus.db")
#: Z-score minimo perche' un TTFT sia considerato ``elevated`` (vs ``normal``).
ELEVATED_Z = 2.0


@dataclass(frozen=True)
class TTFTBaseline:
    """Statistiche di riferimento del TTFT sui campioni storici."""

    mean: float
    std: float
    #: Soglia outlier: ``mean + z_threshold * std`` (inf se baseline vuota).
    outlier_threshold: float
    n_samples: int
    quantile99: float


@dataclass(frozen=True)
class DepthScore:
    """Classificazione di un singolo TTFT rispetto alla baseline."""

    ttft: float
    z_score: float
    is_spike: bool
    #: "normal" | "elevated" | "spike".
    label: str


@dataclass(frozen=True)
class DepthMap:
    """TTFT raggruppati per ``(frame, lang)``."""

    entries: Dict[Tuple[str, str], List[float]]

    def get(self, frame: str, lang: str) -> List[float]:
        """TTFT del gruppo ``(frame, lang)`` (lista vuota se assente)."""
        return self.entries.get((frame, lang), [])

    def sorted_desc(self, limit: int = 20) -> List[Tuple[str, str, float, float]]:
        """Gruppi ordinati per media discendente: ``(frame, lang, mean, count)``."""
        means = [
            ((frame, lang), sum(ttfts) / len(ttfts), len(ttfts))
            for (frame, lang), ttfts in self.entries.items()
            if ttfts
        ]
        means.sort(key=lambda item: (-item[1], -item[2], item[0][0], item[0][1]))
        return [(frame, lang, mean, count) for (frame, lang), mean, count in means[:limit]]


@dataclass(frozen=True)
class Correlation:
    """Correlazione pearson con p-value (t-test a due code, approssimato)."""

    r: float
    p_value: float
    n: int


def calculate_ttft(posted_at: datetime, reply_at: datetime) -> float:
    """Secondi tra il post della probe e la reply del bot (mai negativi)."""
    delta = (reply_at - posted_at).total_seconds()
    return max(0.0, delta)


def _quantile(sorted_vals: List[float], q: float) -> float:
    """Quantile ``q`` con interpolazione lineare sull'indice ordinato.

    Formula tipo R-7 (default numpy): ``pos = q * (n - 1)`` e interpolazione
    tra ``floor(pos)`` e ``floor(pos) + 1``.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def compute_baseline(ttfts: Sequence[float], z_threshold: float = 3.0) -> TTFTBaseline:
    """Baseline statistica del TTFT: media, std (popolazione) e quantile 99.

    Con meno di 3 campioni la baseline e' vuota (media=0, std=0,
    ``outlier_threshold=inf``, quantile99=0) e nessun spike puo' scattare.
    """
    vals = [float(t) for t in ttfts]
    n = len(vals)
    if n < 3:
        return TTFTBaseline(
            mean=0.0,
            std=0.0,
            outlier_threshold=float("inf"),
            n_samples=n,
            quantile99=0.0,
        )
    mean = sum(vals) / n
    variance = sum((v - mean) ** 2 for v in vals) / n
    std = math.sqrt(variance)
    return TTFTBaseline(
        mean=mean,
        std=std,
        outlier_threshold=mean + z_threshold * std,
        n_samples=n,
        quantile99=_quantile(sorted(vals), 0.99),
    )


def detect_depth_spike(ttft: float, baseline: TTFTBaseline, z_threshold: float = 3.0) -> DepthScore:
    """Classifica ``ttft`` rispetto alla baseline via z-score.

    - ``z >= z_threshold`` -> ``spike`` (ragionamento profondo anomalo);
    - ``z >= ELEVATED_Z`` -> ``elevated``;
    - altrimenti -> ``normal``.

    Con baseline vuota (std <= 0) lo z-score e' forzato a 0: mai spike.
    """
    if baseline.std <= 0:
        return DepthScore(ttft=ttft, z_score=0.0, is_spike=False, label="normal")
    z = (ttft - baseline.mean) / baseline.std
    if z >= z_threshold:
        label = "spike"
    elif z >= ELEVATED_Z:
        label = "elevated"
    else:
        label = "normal"
    return DepthScore(ttft=ttft, z_score=z, is_spike=z >= z_threshold, label=label)


def map_frame_lang_depth(rows: Sequence[Tuple[str, str, float]]) -> DepthMap:
    """Raggruppa i TTFT ``(frame, lang, ttft)`` per coppia ``(frame, lang)``."""
    groups: Dict[Tuple[str, str], List[float]] = {}
    for frame, lang, ttft in rows:
        groups.setdefault((frame, lang), []).append(float(ttft))
    return DepthMap(entries=groups)


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Coefficiente di correlazione di pearson (formula di popolazione).

    Serie degenerate (varianza nulla) o lunghezze diverse -> 0.0.
    """
    n = len(xs)
    if n == 0 or len(ys) != n:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    denom = math.sqrt(vx * vy)
    if denom <= 0.0:
        return 0.0
    return cov / denom


def correlate_depth_with_leaks(
    depth_map: DepthMap,
    leak_ttfts: Sequence[float],
    leak_frames_langs: Sequence[Tuple[str, str]],
) -> Correlation:
    """Correla la profondita' media per ``(frame, lang)`` con la leak indicator.

    Per ogni gruppo si usa la media dei TTFT come ``x`` e un indicatore
    binario ``y`` (1 se la coppia ``(frame, lang)`` compare in
    ``leak_frames_langs``, 0 altrimenti). ``leak_ttfts`` riporta i TTFT dei
    probe leak ed e' informazione ausiliaria (l'indicatore deriva dalle coppie).

    P-value: t-test a due code approssimato con ``math.erf``
    (``p = 2 * (1 - erf(|t|/sqrt(2)))``, ``t = r * sqrt((n-2)/(1-r^2))``).
    Con ``n < 3`` o correlazione non calcolabile (varianza nulla) il risultato
    e' ``r=0, p=1``; con ``|r| = 1`` il t-test diverge e ``p=0``.
    """
    leak_set = set(leak_frames_langs)
    xs: List[float] = []
    ys: List[float] = []
    for (frame, lang), ttfts in depth_map.entries.items():
        if not ttfts:
            continue
        xs.append(sum(ttfts) / len(ttfts))
        ys.append(1.0 if (frame, lang) in leak_set else 0.0)
    n = len(xs)
    if n < 3:
        return Correlation(r=0.0, p_value=1.0, n=n)
    r = _pearson(xs, ys)
    denom = 1.0 - r * r
    if denom <= 0.0:
        t = float("inf")
    else:
        t = r * math.sqrt((n - 2) / denom)
    # Clamp a 1.0: per t=0 la formula darebbe 2.0, ma un p-value vive in [0, 1].
    p_value = 0.0 if t == float("inf") else min(1.0, 2.0 * (1.0 - math.erf(abs(t) / math.sqrt(2.0))))
    return Correlation(r=r, p_value=p_value, n=n)


async def load_ttfts(db: Database) -> Tuple[List[float], List[Tuple[str, str, float]]]:
    """Carica i TTFT storici dalle probe con post e reply.

    Restituisce ``(all_ttfts, rows)`` con ``rows`` = ``(frame, lang, ttft)``.
    ``frame`` = ``frame_alias`` (o ``"unknown"`` se vuoto); ``lang`` e' sempre
    ``""`` (le probe non hanno ancora una lingua). Le righe con date non
    parsabili vengono saltate.
    """
    rows = await db.fetchall(
        "SELECT frame_alias, replied_at, posted_at FROM probes "
        "WHERE replied_at IS NOT NULL AND posted_at IS NOT NULL ORDER BY id"
    )
    all_ttfts: List[float] = []
    out_rows: List[Tuple[str, str, float]] = []
    for row in rows:
        frame = row["frame_alias"] or "unknown"
        lang = ""
        try:
            posted = datetime.fromisoformat(row["posted_at"])
            replied = datetime.fromisoformat(row["replied_at"])
        except (TypeError, ValueError):
            continue
        ttft = calculate_ttft(posted, replied)
        all_ttfts.append(ttft)
        out_rows.append((frame, lang, ttft))
    return all_ttfts, out_rows


def print_report(
    path: str,
    baseline: TTFTBaseline,
    depth_map: DepthMap,
    z_threshold: float,
    all_ttfts: Sequence[float],
) -> None:
    """Stampa baseline, gruppi piu' profondi e conteggio spike."""
    print(f"== TTFT analyzer ({path}) ==")
    if baseline.n_samples == 0:
        print("  nessun campione: nessuna probe con posted_at e replied_at")
        return
    print(f"  campioni     : {baseline.n_samples}")
    print(f"  media        : {baseline.mean:.3f} s")
    print(f"  std          : {baseline.std:.3f} s")
    print(f"  quantile99   : {baseline.quantile99:.3f} s")
    print(f"  soglia spike : {baseline.outlier_threshold:.3f} s (z >= {z_threshold:g})")
    spikes = sum(1 for t in all_ttfts if detect_depth_spike(t, baseline, z_threshold).is_spike)
    print(f"  spike        : {spikes} / {baseline.n_samples} (ragionamento profondo)")
    top = depth_map.sorted_desc(20)
    if top:
        print("  Top-20 gruppi (frame, lang) per profondita' media:")
        for frame, lang, mean, count in top:
            print(f"    {frame:<16} {lang:<6} media={mean:8.3f}s  n={count}")
    else:
        print("  nessun gruppo (frame, lang) con campioni")


async def _main(args: argparse.Namespace) -> int:
    conn = Database()
    await conn.initialize(args.db)
    try:
        all_ttfts, rows = await load_ttfts(conn)
        baseline = compute_baseline(all_ttfts, z_threshold=args.threshold)
        depth_map = map_frame_lang_depth(rows)
        if args.report:
            print_report(args.db, baseline, depth_map, args.threshold, all_ttfts)
        return 0
    finally:
        await conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analisi statistica del side-channel TTFT.")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help="path del database in lettura (default: data/locus.db; ':memory:' per un DB vuoto)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=3.0,
        help="z-score oltre il quale un TTFT e' uno spike (default: 3.0)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="stampa baseline, top-20 gruppi piu' profondi e conteggio spike",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
