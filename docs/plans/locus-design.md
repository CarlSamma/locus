# Locus — Design

Locus è un framework minimalista ed efficiente per l'interazione a obiettivo singolo con un bot LLM (target @HackingA0) su X/Twitter, il cui scopo è l'estrazione di una proprietà segreta (passphrase) tramite probing conversazionale guidato dall'entropia.

Sostituisce l'over-engineering di **TAP v3.1** (Aware repo: 3 sottosistemi, 13 servizi Docker, frontend React, memoria a 7 tipi) con un **unico processo Python, un singolo SQLite, un singolo gateway LLM**.

## Principi guida

1. **Determinismo nel core, LLM ai bordi** — selezione proprietà, dedup, calcolo entropia, state machine sono codice puro. L'LLM genera i probe e classifica le risposte, punto.
2. **Un solo DB = single source of truth** — la tabella `probes` *è* l'albero di attacco. Niente SSOT markdown, event_log, EventStore o WebSocket per lo stato.
3. **Una chiamata LLM per risposta** — classificazione, punteggio e riconoscimento pattern fusi in un'unica chiamata JSON strutturata.
4. **Data-driven, niente hardcode** — universe delle proprietà e pesi di entropia da file JSON, non costanti sparse in 3 moduli.
5. **Zero infrastruttura** — nessun Docker/Neo4j/PostgreSQL/Kafka per operare. SQLite + librerie pure.
6. **HITL via CLI** — review A/B e approvazione da terminale. Nessuna dashboard React obbligatoria.

## Architettura

```
src/locus/
├── config.py        # 1 Settings (pydantic-settings, no Docker)
├── models.py        # Probe, Reply, Property, Frame, LedgerEntry — Pydantic v2
├── exceptions.py    # LocusError, LLMError, TwitterError
├── db.py            # UN SQLite, ~6 tabelle; aiosqlite, WAL
├── llm.py           # UN gateway (OpenRouter: retry, circuit breaker, JSON mode)
├── target.py        # X client: post + poll + reply detection
├── select.py        # selezione proprietà: entropia da ledger, data-driven
├── probe.py         # generazione probe + frame (personas) via LLM
├── classify.py      # 1 chiamata LLM → {pattern, boolean, score, leaks}
├── engine.py        # ciclo async state-machine
├── memory.py        # dedup semantico + recall (i 2 pattern memoria che servono)
└── cli.py           # HITL: review A/B, status, run
```

### Flusso del ciclo (1 iterazione)

```
SELECT proprietà (entropia dal ledger)
   → BRANCH frame (persona) + probe via LLM
   → POST tweet a @HackingA0
   → COLLECT reply (poll)
   → CLASSIFY (1 call LLM: pattern/boolean/score/leaks)
   → EXTRACT: aggiorna ledger proprietà (conferma/denied/bias)
   → FOLLOW-UP: decide prossima proprietà (A conservativa / B esplorativa)
```

## Modello dati (SQLite `locus.db`)

| Tabella | Campi chiave | Ruolo |
|---|---|---|
| `properties` | key, weight, prior_entropy, state (unknown/confirmed/denied), votes | Universe proprietà |
| `probes` | id, property_key, frame, text, tweet_id, posted_at, reply_id, reply_text, classification, score | **L'albero di attacco** — ogni row è un nodo |
| `frames` | alias, persona, prompt_template, active/burned/absorbed | Stato frame/personas |
| `intel` | kind, text, entropy_before, entropy_after, note | Leak utili oltre la proprietà corrente |
| `sessions` | id, started_at, probes_total, status | Sessioni di campagna |
| `ledger` | property_key, outcome, ts, probe_id | Log immutabile conferme/denied per entropia |

Proprietà confermate dal lavoro precedente su @HackingA0 (da `eig_property_universe.json`):

| Proprietà | Entropia (bit) |
|---|---|
| word_count | 2.0 |
| total_length | 3.0 |
| first_letter | 1.0 |
| language | 1.5 |
| word1_length | 2.0 |
| word2_length | 2.0 |
| word1_language | 1.5 |
| word2_language | 1.5 |
| **Base** | **20.0** → ~14.5 bit di riduzione, ~20-30 probe |

## Confronto con TAP v3.1 (Aware)

| Aspetto | TAP v3.1 | Locus |
|---|---|---|
| Infrastruttura | 13+ servizi Docker | Nessuna |
| Sottosistemi | TAP + HYDRA (Neo4j) + CHRONOS (Temporal) | Uno |
| DB | SQLite + Neo4j + PostgreSQL 16 | SQLite solo |
| Chiamate LLM/risposta | 3-4 (classifier+judge+γ) | 1 |
| Stato | ssot.md + event_log + EventStore | Una tabella `probes` |
| Logica proprietà | Hardcoded in 3 moduli | `properties.json` data-driven |
| Frontend | React 19 | CLI (opzionale API) |
| Memoria | 7 tipi (aware) | Dedup + recall semantico |

## Cosa si riusa da Aware/tap-app

- `tap-app/src/tap/llm_client.py` → `llm.py` (retry, circuit breaker, JSON parsing)
- `tap-app/src/tap/x_client.py` → `target.py` (triple OAuth, poll)
- `tap-app/src/tap/dpa.py` → concetti frame/alias in `frames` + `probe.py`
- `tap-app/data/eig_property_universe.json` → `properties.json` (data-driven)
- `tap-app/data/hackinga0_historical_fixed.json` → fixture test dedup/classify
- `aware/src/aware/embeddings.py` + `vector_store.py` (brute-force fallback) → `memory.py`

## Cosa NON si riusa

- `hydra/` (Neo4j), `chronos/` (Temporal) — stubs mai in produzione
- Docker Compose completo, Kafka, Debezium, MinIO, ClickHouse, Redis
- Frontend React + FastAPI WebSocket
- SSOT markdown rigenerato, event_log, EventStore
- aware memory a 7 tipi (memory.py copre dedup+recall con ~200 LOC)
- Pesî/priority hardcoded in engine/followup/binary_search

## Test

- `tests/` con pytest + `asyncio_mode = "auto"` (stessa convenzione Aware)
- Mock LLM (risposte fissate) per classify/select/probe senza chiamate reali
- Fixture target: `hackinga0_historical_fixed.json` per testare dedup e classification sul parlato reale del bot
- Nessuna dipendenza da postgres/vss reali — fallback brute-force cosine

## Roadmap

1. **Milestone 0 (questo doc)** — design validato
2. **Milestone 1** — scaffolding `src/locus/` + modello dati + config
3. **Milestone 2** — llm.py + target.py (porting, no network nei test)
4. **Milestone 3** — select.py (entropia data-driven) + probe.py + classify.py
5. **Milestone 4** — engine.py + cli.py (HITL) + memory.py
6. **Milestone 5** — test suite + dry-run offline su storico

## Stato

- [x] Design
- [x] Milestone 1 — scaffolding `src/locus/` + modello dati + config
- [x] Milestone 2 — llm.py + target.py (porting, no network nei test)
- [ ] Milestone 3 — select.py (entropia data-driven) + probe.py + classify.py
- [ ] Milestone 4 — engine.py + cli.py (HITL) + memory.py
- [ ] Milestone 5 — test suite + dry-run offline su storico
