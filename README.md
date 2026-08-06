# Locus

Minimal single-target extraction framework for interacting with LLM bots
(target: `@HackingA0`) via probing conversazionale guidato dall'entropia.

Design: [`docs/plans/locus-design.md`](docs/plans/locus-design.md)

## Quick start

```bash
python -m pytest tests/ -v -p no:postgresql

# CLI (from project root, PYTHONPATH=src)
locus status                 # entropy/state summary (loads SSOT seed)
locus import --seed src/locus/data/locus_seed.json
locus run --dry-run          # offline session replay
locus-gui                    # desktop GUI: generate probes + post to X
```

## Layout

```
src/locus/
  config.py     # 1 Settings (LOCUS_ env prefix)
  models.py     # Pydantic v2 entities
  exceptions.py # LocusError / LLMError / TwitterError
  db.py         # SQLite schema + connection
  llm.py        # unified OpenRouter gateway
  target.py     # X post + poll
  select.py     # entropy-driven property selection
  probe.py      # probe generation via frames
  classify.py   # single-call classification
  engine.py     # async state-machine cycle
  memory.py     # dedup + recall (hash embedder, offline)
  seed.py       # SSOT importer (locus_seed.json)
  gui.py        # desktop GUI (generate + post to X)
  cli.py        # HITL entrypoint
  data/         # locus_seed.json — SSOT of all past probes of @HackingA0
```

## Credentials

X/OpenRouter keys live in `.env` (gitignored). The config maps the legacy
`TWITTER_*` / `OPENROUTER_API_KEY` names to the `LOCUS_*` fields via
`AliasChoices`, so no renaming is needed.

## Status

- [x] Design (`docs/plans/locus-design.md`)
- [x] Milestone 1 — scaffolding + data model
- [x] Milestone 2 — llm.py + target.py
- [x] Milestone 3 — select/probe/classify
- [x] Milestone 4 — engine + cli + memory
- [x] Milestone 5 — test suite + SSOT dry-run
