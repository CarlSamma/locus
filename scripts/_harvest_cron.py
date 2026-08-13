"""Wrapper cron per harvest_hackinga0: esce SILENZIOSO se i crediti X sono
esauriti (402); stampa un riepilogo SOLO quando ha raccolto dati nuovi.

Uso:  python scripts/_harvest_cron.py
Exit 0 sempre. Stdout vuoto = nessuna novità (niente notifica).
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:\PROGETTI\locus")
PY = str(REPO / ".venv" / "Scripts" / "python.exe")
SCRIPT = str(REPO / "scripts" / "harvest_hackinga0.py")


def main() -> int:
    try:
        p = subprocess.run(
            [PY, SCRIPT, "--fetch"],
            capture_output=True, text=True, timeout=1800,
            cwd=str(REPO),
            env={**__import__("os").environ, "PYTHONPATH": ""},
        )
    except subprocess.TimeoutExpired:
        return 0  # silenzioso su timeout
    out = (p.stdout or "") + (p.stderr or "")
    if "credits depleted" in out or "402" in out:
        # niente credito: nessuna notifica
        return 0
    if "raccolti" in out or "[fetch]" in out or "[enrich]" in out:
        rows = [ln for ln in out.splitlines() if "[fetch]" in ln or "[enrich]" in ln or "[stats]" in ln]
        print("[harvest-cron] X API ok, harvest eseguito:")
        print("\n".join(rows[-6:]))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
