# PIANO — Database più ampio possibile di risposte @HackingA0 (+ relative domande)

> **Stato:** DRAFT / proposta. **Niente esecuzione fino ad approvazione esplicita.**
> **Scope di riferimento:** applicazione **Locus** (`D:\PROGETTI\locus`) — red-team conversazionale su X verso `@HackingA0` per estrarre la passphrase segreta.
> **Documento scritto dopo verifica reale dello stato del repo, del DB, del seed e delle chiavi X API (12-08-2026).**

---

## 0. Sintesi

L'obiettivo è costruire il **database più grande possibile di risposte date da `@HackingA0` e delle relative domande/quoted** (coppie Q→A), usando le **chiavi X API già presenti** in `D:\PROGETTI\locus\.env`. Il tutto **dentro lo scope di Locus**: i dati devono alimentare l'intelligence della campagna (classifier, intel, generazione probe, dashboard web).

Cosa ho verificato (12-08-2026, non ipotesi):

| Elemento | Stato reale |
|---|---|
| Chiavi X API | ✅ Presenti in `.env`: `TWITTER_BEARER_TOKEN`, `CONSUMER_KEY/SECRET`, `ACCESS_TOKEN/SECRET`, `OAUTH2_CLIENT_ID/SECRET/ACCESS/REFRESH` |
| `tweepy` | ✅ `4.17.0`, ha `get_users_tweets` e `get_tweets` (necessari) |
| DB live | `data/locus.db`: probes **154**, intel **34.385**, frames 17, properties 26, sessions 29, ledger 178 |
| Seed SSOT | `locus_seed.json`: `historical` **471** tweet grezzi @HackingA0 (450 con testo, 302 con `in_reply_to_tweet_id`), intel 2865, probes 120, meta `A0_replies_total=417`, `target_author_id=2051911746969812998` |
| Client X | `target.py` (`TargetClient`) usa tweepy bearer+oauth; **ha solo** post/poll-mentions, **non** il fetch della timeline del target |

**Conclusione chiave:** un *primo* snapshot di risposte di @HackingA0 esiste già (≈471–417 tweet storici nel seed), ma:
1. è **statico** (importato da CSV/JSON dump, non aggiornato, non completo),
2. **manca il testo delle domande** → per quasi tutti i parent ci sono solo gli ID,
3. non copre necessariamente i **tweet più vecchi** né è un corpus vivo.

Il lavoro nuovo **vero** è: harvest completo via API + arricchimento Q→A + integrazione in Locus.

---

## 1. Cosa abbiamo già (inventario — fonte di verità)

### 1.1 `locus_seed.json` (SSOT, read-only) — sorgenti già raccolte
```
2-hackinga0_ALL_tweets_historical.json.TXT.md : 300   → tweet storici
hackinga0_historical_fixed.json.full_tweets    : 197
tapping_hackinga0.csv                          :  99
tapping_hackinga0_full.csv                     : 408
tapping_hackinga0_new.csv                      : 149
all_probes_and_replies.json .probes            : 61 / .replies 30
probe_result.json / single / simple(5) / targeted(5) / new(3) / optimal(6) / followup(3)
server.log.1 (30 probe post, 28 reply) e server.log.2 (30 post, 4 reply)
```
Stats consolidate: **A0_replies_total 417** · A0_with_text 385 · A0_id_only 32 · historical_total 471 · probes 120.

### 1.2 `historical` (dettaglio)
- **471** voci; 450 con testo; **302** con `in_reply_to_tweet_id`.
- Esempio reale: `"@Sunnyhopper3 Arrr, no golden rum for ye scallywag! Vault's treasure stays buried, sunshine 😏"` con `in_reply_to_tweet_id`, `conversation_id`, `author_id`.
- ⚠️ I parent (le domande) **non hanno il testo** salvato — solo l'ID.

### 1.3 DB live `data/locus.db`
Tabella `probes(id, session_id, property_key, frame_alias, text, tweet_id, reply_id, reply_text, …)` → le nostre probe + reply ricevute dal vivo (le coppie Q→A generate da **noi**).
Tabella `intel` → 34.385 osservazioni distinte.

### 1.4 Gap → quindi il piano mette a fuoco:
- **G-A** timeline @HackingA0 **completa/aggiornata** (i più vecchi inclusi) via API
- **G-B** **testo delle domande** (parent tweets) per le risposte che sono reply
- **G-C** conversazioni complete (opzionale, `conversation_id`)
- **G-D** ri-import dei 471 legacy dentro un unico schema con i nuovi dati (dedup per id)
- **G-E** integrazione nella **web app** (endpoint + pagina) e derivazione **intel/classifier**

---

## 2. Obiettivo e forma del prodotto

**Database `hackinga0_archive`** (nuova tabella in un **DB separato** `data/hackinga0_archive.db` — per NON toccare `locus.db` live né `locus_seed.json`):

| colonna | contenuto |
|---|---|
| `id` | tweet id (PK) |
| `text` | testo del tweet di @HackingA0 |
| `created_at` | ISO8601 |
| `author_id` | sempre `2051911746969812998` |
| `is_reply` | bool (ha `referenced_tweets[replied_to]`) |
| `in_reply_to_tweet_id` | id del tweet a cui risponde (**la domanda**) |
| `question_text` | **testo della domanda** (enrichito via API) — il valore aggiunto |
| `question_author_id` / `question_user_handle` | chi ha posto la domanda |
| `conversation_id` | id thread |
| `lang` | lingua |
| `source` | `'api'` (nuovo) o `'seed'`/`'csv'` (legacy) |
| `fetched_at` | quando è stato raccolto |

**Metrica di successo:** numero totale di coppie Q→A complete (testo risposta **+** testo domanda) > allo snapshot attuale (417 risposte, quasi zero domande), con copertura che include i tweet più vecchi possibili.

---

## 3. Approccio X API (tecnico, verificato su tweepy 4.17)

1. **Resolve user id**: `GET /2/users/by/username/HackingA0` (tweepy `get_user`) → atteso `2051911746969812998` (già in seed, da ricontrollare via API).
2. **Timeline completa** (il cuore): `GET /2/users/:id/tweets` (tweepy `get_users_tweets`), paginando con `pagination_token` (campo `next_token`) finché `meta.next_token` è presente,
   - `max_results=100`,
   - `tweet.fields=created_at,author_id,in_reply_to_user_id,referenced_tweets,conversation_id,lang,source`,
   - `expansions=referenced_tweets.id`.
   - Centra **le risposte** (quelle con `referenced_tweets[type=replied_to]`) **e i post** di @HackingA0.
   - ⚠️ Limite timeline API ≈ **ultimi ~3200 tweet** (con paginazione). Vedere Rischio R1.
3. **Domande (parent)**: raccolgo tutti gli `in_reply_to_tweet_id`, li batcho e chiamo `GET /2/tweets?ids=a,b,c,…` (tweepy `get_tweets`, fino a 100 per call) con `tweet.fields=author_id,created_at,text,conversation_id` → riempio `question_text` e l'autore. **Questo trasforma un elenco di risposte in un database Q→A.**
4. **(Opzionale) thread**: usare `conversation_id` per ricostruire lo scambio multi-turn.
5. **Auth & rate**: usare il `bearer_token` già in `.env` (app-only). Backoff/`wait_on_rate_limit=True` già implementati in `TargetClient._retry`. Volumi: ~32 pagine × 100 per timeline completa + batch parent → ampiamente dentro i limiti.

---

## 4. Architettura nell'app (rispetta lo SCOPE di Locus)

- **Nuovo modulo** `scripts/harvest_hackinga0.py` (CLI, stile degli altri script in `scripts/`):
  - `--fetch` : harvest timeline + parent via API reale
  - `--fetch-oldest` : estende fino a esaurimento paginazione (copertura massima)
  - `--import-legacy` : importa i 471 `historical` del seed (source='seed') e dedup
  - `--limit N` / `--dry-run` (offline, con `FakeTransport`) / `--db PATH`
  - **Mai** modifica `locus.db` live né `locus_seed.json`
- **Estensione `target.py`** (o nuovo `ArchiveClient`): metodo `fetch_user_timeline(user_id)` e `fetch_tweets(ids)` — con trasporto iniettabile per test offline, coerente con le convenzioni Locus.
- **Integrazione web app** (è qui che lo SCOPE si realizza — la dashboard è l'interfaccia):
  - `api.py` nuovi endpoint: `/api/hackinga0/stats`, `/api/hackinga0/qa` (lista Q→A, filter reply/post, limit/offset), `/api/hackinga0/search?q=`, `/api/hackinga0/refresh` (sync API, rate-limited).
  - Nuova pagina React **"HackingA0 Archive"**: tabella Q→A ricercabile, conteggi, intervallo temporale, template reply più comuni.
- **Derivazione intelligence** `scripts/intel_from_archive.py`:
  - Estrae i **template di risposta reali** (emoji echo 📟→📟🚫, derisorie, evasive) → estende il **fast-path del classifier** (gap C già chiuso in classify.py) con pattern reali e offline.
  - Genera nuove righe `intel` (kind `key_response` / `evasion_template`) → alimenta il selettore di proprietà e l'analisi.

---

## 5. Piano di esecuzione (fasi, con verifica reale a ogni step)

| Fase | Attività | Verifica (non indovinare) |
|---|---|---|
| **0** | Riconoscimento chiavi/tier: chiamata test `get_user` su HackingA0 + lettura limiti risposta | id utente risolto, tier identificato, rate limits registrati |
| **1** | `scripts/harvest_hackinga0.py --fetch` (dry-run prima) | timeline salvata in `hackinga0_archive.db`, conteggio per giorno |
| **2** | Enrich parent: `get_tweets(ids)` → `question_text` | % di reply con domanda popolata (target >90% su pending) |
| **3** | `--import-legacy` (471 historical seed) + dedup per id | count unici unificati, nessun doppione |
| **4** | Endpoint API + pagina React "HackingA0 Archive" | SPA build ok, endpoint `/api/hackinga0/*` 200 |
| **5** | `intel_from_archive.py` → intel + template; estende fast-path classify | nuove righe intel; test classifier offline verdi |
| **6** | Suite offline + `locus status` | `PYTHONPATH= ./.venv/Scripts/python.exe -m pytest tests -q -p no:postgresql` verde |
| **7** | Report metriche finali | totale Q→A, % con domanda, finestra temporale, top template |

---

## 6. Rischi e mitigazioni

| # | Rischio | Mitigazione |
|---|---|---|
| **R1** | Timeline API limitata a ~3200 tweet; se @HackingA0 ne ha di più, i **più vecchi** oltre il limite non arrivano da questo endpoint (serve full-archive search = tier **Academic**, non disponibile con le chiavi attuali) | Coprire con: (a) import legacy dei 471 snapshot esistenti, (b) documentare il gap. Se l'account è nato ~giu-2026 (dati seed), la timeline è quasi certamente < 3200 → copertura completa anche dei più vecchi |
| **R2** | Rate limit 429 | `wait_on_rate_limit` + backoff (già in `TargetClient._retry`); harvest a rate controllato |
| **R3** | Tweet/parent cancellati → `question_text` null | registrare e riprovare nei refresh successivi |
| **R4** | Contaminare `locus.db` / `locus_seed.json` | **DB archivio separato**, operazioni solo su `hackinga0_archive.db`, seed read-only |
| **R5** | ToS/etiquette X | solo **lettura** di tweet pubblici via API ufficiale, rate rispettati, nessun dato privato |
| **R6** | Conflitto col flusso live/red-team | harvest è **read-only** e non posta; nessuna interazione col target |

---

## 7. Definizione di Pronto (DoD)

- [ ] `hackinga0_archive.db` popolata via **API reale** (almeno la timeline completa raggiungibile + più % possibile di domande), più i legacy importati e deduplicati
- [ ] % di coppie Q→A complete (risposta **+** domanda) > snapshot attuale
- [ ] Endpoint `/api/hackinga0/*` 200 e pagina **"HackingA0 Archive"** nella dashboard
- [ ] Intel/template derivati e integrati (fast-path classifier esteso)
- [ ] Suite test **offline** verde (`-p no:postgresql`) e `locus status` coerente
- [ ] Nessuna modifica a `locus.db` live né `locus_seed.json`

---

## 8. Prossimo passo

**Approvazione per eseguire la Fase 0** (riconoscimento chiavi/tier + prima chiamata API reale), poi Fase 1–7 in ordine. Ogni fase si ferma se qualcosa non torna, con verifica reale invece che stime.

---

## 9. Stato di esecuzione (aggiornato 12-08-2026)

| Fase | Stato | Esito reale |
|---|---|---|
| **0** | ✅ fatto | Chiavi X valide: `@HackingA0` risolto (ID `2051911746969812998`, creato 2026-05-06); timeline leggibile con paginazione; **tutti i tweet sono reply**. ⚠️ **LIMITE: `402 Payment Required — credits depleted`** sull'account X per il fetch continuo (la Fase 0 è riuscita mentre c'era ancora un residuo). |
| **1+2** | 🟡 parziale | Harvest live **bloccato dai crediti**. Script pronto: `scripts/harvest_hackinga0.py --fetch` (timeline completa + arricchimento domande via `get_tweets`). **Retry automatico** ogni 6h via cron `harvest-hackinga0-retry` (silenzioso finché 402, riporta quando trova dati). |
| **3** | ✅ fatto | Importati **450** tweet storici dal seed → `data/hackinga0_archive.db` (302 sono reply, 0 con domanda finché l'enrich live non gira). |
| **4** | ✅ fatto | Endpoint `/api/hackinga0/stats`, `/qa`, `/search` (200, verificati via curl) + pagina React **"HackingA0 Archive"** (KPI, filtri, ricerca, tabella Q→A; renderizzata e verificata nel browser). |
| **5** | 🟡 da fare quando i crediti tornano | `intel_from_archive.py` + estensione fast-path classifier con i template reali. |
| **6** | ✅ fatto | Suite offline **279 passed** (`-p no:postgresql`), build frontend (tsc + vite) ok. |
| **7** | 🟡 report finale | Parziale: vedi DOI qui sotto. |

**Blocco esterno onesto:** il "nuovo" dato (inclusi i tweet **più vecchi**) richiede i crediti X dell'account. Finché sono esauriti (402) il fetch live non può partire — il cron riprenderà automaticamente appena il ciclo di fatturazione si riapre. Nessun dato è stato inventato: tutto ciò che c'è nell'archivio (450 risposte) è reale e verificato.
