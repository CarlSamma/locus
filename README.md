# Locus

Minimal single-target extraction framework for interacting with LLM bots
(target: `@HackingA0`) via probing conversazionale guidato dall'entropia.

Design: [`docs/plans/locus-design.md`](docs/plans/locus-design.md)

## Quick start

```bash
python -m pytest tests/ -v -p no:postgresql
```

## Install (editable)

```bash
pip install -e .
```

## Layout

```
src/locus/
  config.py     # 1 Settings (LOCUS_ env prefix)
  models.py     # Pydantic v2 entities
  db.py         # SQLite schema + connection
  ...           # (Milestone 2+: llm, target, select, probe, classify, engine, memory, cli)
```

## Status

- [x] Design (`docs/plans/locus-design.md`)
- [x] Milestone 1 — scaffolding + data model
- [ ] Milestone 2 — llm.py + target.py
- [ ] Milestone 3 — select/probe/classify
- [ ] Milestone 4 — engine + cli + memory
- [ ] Milestone 5 — test suite + dry-run
