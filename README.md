# Locus

Minimal single-target extraction framework for interacting with LLM bots
(target: `@HackingA0`) via probing conversazionale guidato dall'entropia.

Design: [`docs/plans/locus-design.md`](docs/plans/locus-design.md)

## Requisiti

- Windows 10/11
- Python **3.10+** (testato con 3.13) presente nel `PATH`
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

# 4) creare il file .env con le credenziali (X + OpenRouter)
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

### 2. Linea di comando

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

### 3. Test

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
  data/         # locus_seed.json — SSOT of all past probes of @HackingA0
```

## Status

- [x] Design (`docs/plans/locus-design.md`)
- [x] Milestone 1 — scaffolding + data model
- [x] Milestone 2 — llm.py + target.py
- [x] Milestone 3 — select/probe/classify
- [x] Milestone 4 — engine + cli + memory
- [x] Milestone 5 — test suite + SSOT dry-run
- [x] GUI — generazione probe + posting su X
