# AGENTS.md

Locus — single-process Python framework that probes an LLM bot on X (`@HackingA0`)
via entropy-guided conversational probes. Extraction of a secret passphrase through
probe → reply → classify cycles. Windows / PowerShell / Python 3.10+ (3.13 tested).

Agent can use the MCP notebooklm (https://github.com/jacob-bd/gemini-notebook-mcp-cli )
Agent can create new notebooklm; makes autonomous search in the notebooks; add new sources; upload files to notebooklm;

## Setup (Windows)

- Root is `D:\PROGETTI\locus`; everything runs from there.
- venv: `.\.venv\Scripts\Activate.ps1`, then `pip install -r requirements.txt` and `pip install -e .` (src-layout — editable install is required for the `locus` / `locus-gui` console scripts).
- Dev tools (ruff, mypy) are declared in `pyproject.toml` dev extras but are **not** installed by `requirements.txt`. Install with `pip install -e .[dev]` before running lint/typecheck.
- `.env` (gitignored) holds X + OpenRouter secrets. Legacy `TWITTER_*` / `OPENROUTER_API_KEY` names map to `LOCUS_*` fields via `AliasChoices` in `config.py` — do not rename. Never commit `.env`.

## Commands

- Tests (fully offline, 46 tests): `python -m pytest tests\ -v -p no:postgresql`
- CLI: `locus status` (entropy summary), `locus import --seed src\locus\data\locus_seed.json`, `locus review`
- GUI: `locus-gui` or `python -m locus.gui` (Tkinter)

## Safety: live vs offline

- `locus run --dry-run` — offline replay, no network.
- `locus run --max N` — **LIVE**: posts real tweets to X and polls replies. Never run live in a test/dev context; prefer `--dry-run`.

## Architecture (not obvious from filenames)

- Single SQLite `data/locus.db` (WAL, gitignored, created at runtime). The `probes` table IS the attack tree / source of state; `ledger` logs immutable outcomes per property.
- Data-driven: the property universe and full campaign history live in `src/locus/data/locus_seed.json` (16 properties, 120 probes, 2865 intel). Every CLI command and the GUI auto-import this seed before running — treat as read-only SSOT.
- One iteration of `engine.py`: SELECT property by entropy → generate probe via LLM → post → poll reply → CLASSIFY (single LLM call returning `{pattern, boolean, score, leaks}`) → update ledger/property state.
- `llm.py` (OpenRouter gateway: retry + circuit breaker + JSON parsing) and `target.py` (X client) both accept an injectable `transport`. That seam is how tests avoid the network.

## Testing conventions

- Tests must stay fully offline: inject fake transports (`FakeTransport`/`FakeChat` with `.chat.completions.create`) into `LLMClient`/`TargetClient`; never construct the real clients.
- Always `LocusConfig(_env_file=None)` in tests so the local `.env` is not loaded.
- Async tests rely on pytest-asyncio `asyncio_mode = "auto"`; DB fixture uses `:memory:`.
- Tests are grouped by milestone (`test_milestone1.py` … `test_milestone5.py`, `test_gui_config.py`).

## Style / tooling

- Ruff: line-length 120, double-quoted strings, `select = ["E", "F", "W", "I"]` (import sorting enforced). Mypy targets py310.
- Code comments and docs are in Italian (README, `docs/plans/`); commit messages use conventional-style English (`feat:`, `docs:`). Match whichever language the file you touch uses.
