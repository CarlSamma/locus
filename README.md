# Locus

Minimal single-target extraction framework for interacting with LLM bots
(target: `@HackingA0`) via probing conversazionale guidato dall'entropia.

Backend FastAPI + dashboard web (React) oltre a CLI e GUI desktop Tkinter.

Design: [`docs/plans/locus-design.md`](docs/plans/locus-design.md)

## Requisiti

- Windows 10/11
- Python **3.10+** (testato con 3.13) presente nel `PATH`
- Node.js **18+** e npm (solo per build/sviluppo della webapp)
- `git` (opzionale, per l'installazione)

## Installazione (Windows)

Aprire **PowerShell** nella cartella del progetto (`D:\PROGETTI\locus`):

```powershell
# 1) (opzionale) creare e attivare un virtualenv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) installare le dipendenze
pip install -r requirements.txt

# 3) installare il pacchetto in modalità sviluppo
pip install -e .

# 4) (solo per la webapp) installare le dipendenze frontend
cd web
npm install
cd ..

# 5) creare il file .env con le credenziali (X + OpenRouter)
#    copiare .env.example in .env e inserire le chiavi
```

## Credenziali

Le chiavi X/Twitter e OpenRouter vivono nel file `.env` (escluso da git,
mai committare). Il config mappa i nomi legacy `TWITTER_*` /
`OPENROUTER_API_KEY` ai campi `LOCUS_*` via `AliasChoices`, quindi non serve
rinominare nulla. Esempio di `.env`:

```
TWITTER_BEARER_TOKEN=...
TWITTER_CONSUMER_KEY=...
TWITTER_CONSUMER_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_TOKEN_SECRET=...
TWITTER_OAUTH2_CLIENT_ID=...
TWITTER_OAUTH2_CLIENT_SECRET=...
TWITTER_OAUTH2_ACCESS_TOKEN=...
TWITTER_OAUTH2_REFRESH_TOKEN=...
OPENROUTER_API_KEY=...
```

## Lancio da terminale (Windows)

> Tutti i comandi vanno eseguiti dalla root del progetto (`D:\PROGETTI\locus`)
> con il virtualenv attivo (o con Python a sistema).

### 1. Interfaccia grafica (genera probe e posta su X)

```powershell
locus-gui
# oppure
python -m locus.gui
```

La finestra permette di:

- scegliere la **Property** (16 proprietà dal SSOT) e il **Frame** (persona)
- **Generate probe** → genera una sonda via LLM (grok-4.3 / OpenRouter)
- modificare il testo prima dell'invio
- **Post to X** → pubblica il tweet con menzione a `@HackingA0`
- **Poll replies** → legge le risposte del target

### 2. Web app (FastAPI + React)

Il backend FastAPI (`src/locus/api.py`) espone le stesse funzionalità via REST
e serve la dashboard React compilata in `web/dist`.

```powershell
# 1) (una tantum) compilare il frontend → web/dist
cd web
npm install
npm run build
cd ..

# 2) avviare il server (API + SPA sulla stessa porta)
python -m uvicorn locus.api:app --host 127.0.0.1 --port 8000
```

Aprire `http://127.0.0.1:8000`. Documentazione interattiva delle API:
`http://127.0.0.1:8000/api/docs`.

Viste della dashboard:

- **Status** — KPI entropia, progresso Fase 5, tabella stato proprietà
- **Proprietà** — universe delle proprietà con barre di entropia
- **Probe Lab** — genera/posta/interroga sonde + sessioni (dry-run offline)
- **Attack Tree** — la tabella `probes` (l'albero di attacco) espandibile
- **Review** — top sonde per score, approvazione HITL
- **Ledger & Intel** — esiti immutabili e leak raccolti
- **Sessions** — storico sessioni di campagna

In sviluppo (hot reload frontend + proxy verso FastAPI):

```powershell
python -m uvicorn locus.api:app --host 127.0.0.1 --port 8000
# in un altro terminale:
cd web
npm run dev        # http://localhost:5173 (proxy /api → :8000)
```

Endpoint principali: `GET /api/status`, `GET /api/properties`, `GET
/api/frames`, `GET /api/probes`, `GET /api/review`, `GET /api/ledger`, `GET
/api/intel`, `GET /api/sessions`, `POST /api/run` (sessione in background, con
`--dry-run` equivalente via `dry_run: true`), `POST /api/probes/generate`,
`POST /api/probes/post`, `POST /api/probes/poll`.

### 3. Linea di comando

```powershell
# Riepilogo proprietà / entropia / sessioni (carica il SSOT)
locus status

# Importa il SSOT (tutte le sonde passate) nel database
locus import --seed src\locus\data\locus_seed.json

# Replay offline (nessuna chiamata di rete)
locus run --dry-run

# Sessione LIVE (posta davvero su X — da usare con cautela)
locus run --max 5

# Rivedi le sonde classificate
locus review
```

### 4. Test

```powershell
python -m pytest tests\ -v -p no:postgresql
```

## Layout

```
src/locus/
  config.py     # 1 Settings (LOCUS_ env prefix + alias legacy)
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
  trust.py      # confini di fiducia: sanifica contenuto non fidato (reply)
  gui.py        # desktop GUI (generate + post to X)
  cli.py        # HITL entrypoint
  api.py        # backend FastAPI: endpoint REST + mount SPA (web/dist)
  data/         # locus_seed.json — SSOT of all past probes of @HackingA0

web/                  # frontend React + Vite (compilato in web/dist)
  src/
    api.ts            # client tipizzato per /api/*
    App.tsx           # router (7 view) + layout shell
    components/       # Layout (sidebar/header/footer), ui (KPI, barre, chip)
    pages/            # Status, Properties, ProbeLab, AttackTree, Review, Ledger, Sessions
  dist/               # output di build, servito da FastAPI (gitignored)
```

## Status

- [x] Design (`docs/plans/locus-design.md`)
- [x] Milestone 1 — scaffolding + data model
- [x] Milestone 2 — llm.py + target.py
- [x] Milestone 3 — select/probe/classify
- [x] Milestone 4 — engine + cli + memory
- [x] Milestone 5 — test suite + SSOT dry-run
- [x] GUI — generazione probe + posting su X
- [x] Web — backend FastAPI + dashboard React (7 view, dry-run da browser)
