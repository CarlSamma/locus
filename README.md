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

### 2. Web app (FastAPI + React) — avvio passo-passo

La web app è una dashboard che si apre nel browser. Serve: il backend FastAPI
(`src/locus/api.py`) che espone le API e serve la dashboard React (in `web/dist`).

> 💡 **Nota per chi inizia**: per la **sola visualizzazione** della dashboard
> (Pagine Status, Proprietà, Attack Tree, Ledger, Sessions…) **non servono le
> chiavi** di X/OpenRouter. Le chiavi servono solo alle azioni live (Genera probe,
> Posta su X, Poll). Quindi la prima volta puoi comunque aprire la dashboard.

#### Prima volta (una tantum)

Apri **PowerShell** nella cartella del progetto, poi esegui in ordine questi blocchi.
Non saltare passaggi; se un comando dà errore, fermati e controlla prima di continuare.

**① Installa le dipendenze Python e il pacchetto**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

Se vedi `(venv)` all'inizio del prompt, la "venv" è attiva. Se `python`
non viene trovato, controlla di avere installato **Python 3.10+** dal sito
python.org (spunta "Add to PATH" durante l'installazione).

**② Installa il frontend React**

```powershell
cd web
npm install
npm run build
cd ..
```

> `npm` viene installato insieme a **Node.js 18+**. Se `npm` non è riconosciuto,
> scarica e installa Node.js da nodejs.org, poi riapri PowerShell e riprova.

**③ Copia il file delle credenziali** (facoltativo per la sola visualizzazione)

```powershell
copy .env.example .env
```

Poi apri `.env` con un editor di testo e inserisci le chiavi se vuoi usare le
azioni live (Generate / Post / Poll). Se non hai chiavi, lascialo così: la
dashboard si apre comunque.

#### Avvio (oggi e ogni volta dopo)

**④ Lancia il server**

```powershell
python -m uvicorn locus.api:app --host 127.0.0.1 --port 8000
```

Vedrai qualcosa tipo `Application startup complete` e `Uvicorn running on
http://127.0.0.1:8000`. Non chiudere questa finestra finché usi la dashboard.

**⑤ Apri il browser**

Vai su **http://127.0.0.1:8000**. Per la documentazione interattiva delle API:
**http://127.0.0.1:8000/api/docs**.

Per **fermare** il server: torna nella finestra PowerShell e premi `Ctrl+C`.

#### Se riavvii il computer

Ripeti solo i passaggi ④ e ⑤ (la venv, le dipendenze e la build restano salvate).
Se per errore chiudi PowerShell senza attivare la venv, dapprima esegui:
`.\.venv\Scripts\Activate.ps1` e poi il passo ④.

#### Viste della dashboard

- **Status** — KPI entropia, progresso Fase 5, tabella stato proprietà
- **Proprietà** — universe delle proprietà con barre di entropia
- **Probe Lab** — genera/posta/interroga sonde + sessioni (dry-run offline)
- **Attack Tree** — la tabella `probes` (l'albero di attacco) espandibile
- **Review** — top sonde per score, approvazione HITL
- **Ledger & Intel** — esiti immutabili e leak raccolti
- **Sessions** — storico sessioni di campagna

#### Sviluppo (avanzato, con hot reload — non per l'utente medio)

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

### 4. Suite di attacco (scripts/)

Motore offensivo autonomo in `scripts/` — orchestra probe → reply →
classificazione in quattro fasi. Tutti i moduli sono **offline-first** (nessuna
rete, nessun LLM obbligatorio per il pre-parse deterministico):

```powershell
# Replay offline della macchina a stati (4 fasi): nessun post, nessuna rete
python scripts\autonomous_strike.py --max-probes 3
```

Moduli della suite:

- `zero_shot_calibration.py` — baseline di entropia su DB reale + campione
  sintetico; deriva hash di risposte derisorie, pattern per frame, gradienti
  di entropia, baseline TTFT e matrice di difesa per lingua
- `probe_variants_advanced.py` — 9 varianti della stessa domanda (Tags
  Unicode, bidi RTL, base64, acrostico, dash, caesar, breakpoint,
  HTML/Markdown, plain), pura manipolazione di stringhe
- `information_theory_optimizer.py` — ranking delle varianti per riduzione
  attesa `E[ΔH]` (entropia residua × indice di Gini × confidence)
- `ttft_analyzer.py` — canale laterale Time-To-First-Token: baseline e
  rilevamento spike di profondità di ragionamento
- `faiss_memory_probe.py` — probe "archival" contro lo store di memoria del
  bot (frame meno difeso, nessun secret scan)
- `subagent_delegation.py` — frame "archive task": delegazione al verifier,
  parsing del report
- `reconstruct_passphrase.py` — collassa i leak storici in candidati ordinati
  per probabilità (beam search deterministica, vincoli strutturali noti)
- `backfill_classify.py` — riclassifica a freddo le reply storiche mai
  arrivate allo stato `classified`
- `autonomous_strike.py` — macchina a stati a 4 fasi (calibrazione → breach →
  loop adattivo → ricostruzione) con rate limit, cost breaker e checkpoint

### 5. Test e lint

```powershell
# Suite completa (275 test, interamente offline)
python -m pytest tests\ -v -p no:postgresql

# Lint e typecheck (dev extras: pip install -e .[dev])
python -m ruff check src scripts tests
python -m mypy src scripts
```

`scripts/` è un namespace package (`pyproject.toml` ha `explicit_package_bases`
+ `namespace_packages`): i moduli importano i gemelli come `scripts.foo`.

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

scripts/              # suite di attacco offline (vedi §4 in alto)
  autonomous_strike.py          # macchina a stati a 4 fasi
  zero_shot_calibration.py      # baseline + layer di intelligenza
  probe_variants_advanced.py    # 9 encoding della stessa domanda
  information_theory_optimizer.py  # ranking E[ΔH]
  ttft_analyzer.py              # canale laterale TTFT
  faiss_memory_probe.py         # probe "archival" / memoria
  subagent_delegation.py        # delegazione al verifier
  reconstruct_passphrase.py     # candidati passphrase dai leak
  backfill_classify.py          # riclassificazione a freddo

tests/                # 275 test offline (milestone + suite di attacco)
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
