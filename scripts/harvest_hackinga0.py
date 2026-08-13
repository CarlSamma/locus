"""Harvest completo della cronologia @HackingA0 via X API + arricchimento domande (Q->A).

Files d'uso:
  - Fase 1 (harvest):  python scripts/harvest_hackinga0.py --fetch [--limit N]
  - Fase 2 (domande):  lo stesso comando arricchisce i genitori in batch (get_tweets)
  - Fase 3 (legacy):   python scripts/harvest_hackinga0.py --import-legacy
  - Dry-run offline:   python scripts/harvest_hackinga0.py --fetch --limit 1 --dry-run

Scrive SOLO su data/hackinga0_archive.db. NON tocca locus.db né locus_seed.json.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"D:\PROGETTI\locus")
DB = REPO / "data" / "hackinga0_archive.db"
SEED = REPO / "src" / "locus" / "data" / "locus_seed.json"
TARGET_USERNAME = "HackingA0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hackinga0_archive (
    id                    TEXT PRIMARY KEY,
    text                  TEXT,
    created_at            TEXT,
    author_id             TEXT,
    is_reply              INTEGER,
    in_reply_to_tweet_id  TEXT,
    question_text         TEXT,
    question_author_id    TEXT,
    question_user_handle  TEXT,
    conversation_id       TEXT,
    lang                  TEXT,
    source                TEXT,
    fetched_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_hacka0_created ON hackinga0_archive(created_at);
CREATE INDEX IF NOT EXISTS idx_hacka0_parent  ON hackinga0_archive(in_reply_to_tweet_id);
"""


def load_env(p: Path) -> dict:
    env = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO hackinga0_archive
           (id, text, created_at, author_id, is_reply, in_reply_to_tweet_id,
            question_text, question_author_id, question_user_handle,
            conversation_id, lang, source, fetched_at)
           VALUES (:id,:text,:created_at,:author_id,:is_reply,:in_reply_to_tweet_id,
                   :question_text,:question_author_id,:question_user_handle,
                   :conversation_id,:lang,:source,:fetched_at)""",
        row,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="harvest timeline via API")
    ap.add_argument("--limit", type=int, default=None, help="max tweet da prendere (debug)")
    ap.add_argument("--dry-run", action="store_true", help="nessuna scrittura su DB")
    ap.add_argument("--import-legacy", action="store_true", help="importa historical dal seed")
    ap.add_argument("--import-probes", action="store_true", help="importa coppie Q→A (probe→reply) dal seed")
    ap.add_argument("--stats", action="store_true", help="mostra conteggi archivio")
    args = ap.parse_args()

    if args.stats:
        conn = get_conn()
        tot = conn.execute("SELECT count(*) FROM hackinga0_archive").fetchone()[0]
        withq = conn.execute(
            "SELECT count(*) FROM hackinga0_archive WHERE question_text IS NOT NULL AND question_text != ''"
        ).fetchone()[0]
        nreply = conn.execute("SELECT count(*) FROM hackinga0_archive WHERE is_reply=1").fetchone()[0]
        print(f"[stats] tot={tot} risposte={nreply} con_domanda={withq}")
        return 0

    if args.import_legacy:
        return import_legacy()

    if args.import_probes:
        return import_probes()

    if args.fetch:
        return fetch(args.limit, args.dry_run)

    ap.print_help()
    return 2


def _row_from_tweet(t: object, source: str) -> dict:
    refs = getattr(t, "referenced_tweets", None) or []
    parent = None
    for r in refs:
        if getattr(r, "type", None) == "replied_to":
            parent = str(r.id)
    return {
        "id": str(t.id),
        "text": getattr(t, "text", None),
        "created_at": (getattr(t, "created_at", None) or datetime.now(timezone.utc)).isoformat(),
        "author_id": str(getattr(t, "author_id", "")),
        "is_reply": 1 if parent else 0,
        "in_reply_to_tweet_id": parent,
        "question_text": None,
        "question_author_id": None,
        "question_user_handle": None,
        "conversation_id": str(getattr(t, "conversation_id", "") or ""),
        "lang": getattr(t, "lang", None),
        "source": source,
        "fetched_at": now_iso(),
    }


def fetch(limit: int | None, dry_run: bool) -> int:
    env = load_env(REPO / ".env")
    bearer = env.get("TWITTER_BEARER_TOKEN", "")
    if not bearer:
        print("ERRORE: TWITTER_BEARER_TOKEN mancante")
        return 1
    import tweepy

    client = tweepy.Client(bearer_token=bearer, wait_on_rate_limit=True)
    u = client.get_user(username=TARGET_USERNAME)
    if u.errors or not u.data:
        print("ERRORE resolve:", u.errors)
        return 2
    uid = u.data.id

    conn = get_conn() if not dry_run else None
    fetched = 0
    pages = 0
    token = None
    t0 = time.time()
    while True:
        kwargs = dict(
            id=uid, max_results=100,
            tweet_fields=["created_at", "author_id", "in_reply_to_user_id",
                          "referenced_tweets", "conversation_id", "lang"],
            expansions=["referenced_tweets.id"],
        )
        if token:
            kwargs["pagination_token"] = token
        r = client.get_users_tweets(**kwargs)
        if r.errors and not r.data:
            print("ERRORE pagina:", r.errors)
            break
        if r.data:
            for t in r.data:
                row = _row_from_tweet(t, "api")
                if conn:
                    upsert(conn, row)
                fetched += 1
        pages += 1
        meta = getattr(r, "meta", None) or {}
        token = meta.get("next_token")
        if not token:
            break
        if limit and fetched >= limit:
            break
        if pages % 5 == 0:
            print(f"  ...{fetched} tweet in {pages} pagine ({time.time()-t0:.0f}s)")
            if conn:
                conn.commit()
    if conn:
        conn.commit()
    print(f"[fetch] raccolti {fetched} tweet in {pages} pagine ({time.time()-t0:.0f}s)")
    print("[fetch] ora arricchisco le domande (parent tweets)...")
    if conn:
        enrich_questions(conn, client, dry_run=False)
    return 0


def enrich_questions(conn: sqlite3.Connection, client, dry_run: bool) -> None:
    rows = conn.execute(
        "SELECT id, in_reply_to_tweet_id FROM hackinga0_archive "
        "WHERE is_reply=1 AND in_reply_to_tweet_id IS NOT NULL "
        "AND (question_text IS NULL OR question_text='')"
    ).fetchall()
    parents = {r[1] for r in rows}
    total = len(parents)
    done = 0
    lst = sorted(parents)
    for i in range(0, len(lst), 100):
        batch = lst[i : i + 100]
        try:
            resp = client.get_tweets(
                ids=batch,
                tweet_fields=["text", "author_id", "created_at"],
                user_fields=["username"],
                expansions=["author_id"],
            )
        except Exception as e:
            print("  get_tweets err:", e)
            continue
        username = {}
        if getattr(resp, "includes", None) and resp.includes.get("users"):
            for usr in resp.includes["users"]:
                username[str(usr.id)] = getattr(usr, "username", None)
        found = {}
        for t in (resp.data or []):
            found[str(t.id)] = (getattr(t, "text", None), str(getattr(t, "author_id", "") or ""))
        for pid in batch:
            if pid in found:
                qt, qa = found[pid]
                conn.execute(
                    "UPDATE hackinga0_archive SET question_text=?, question_author_id=?, "
                    "question_user_handle=? WHERE in_reply_to_tweet_id=?",
                    (qt, qa, username.get(qa), pid),
                )
        done += len(batch)
        conn.commit()
        if done % 200 == 0 or done == total:
            print(f"  ...domande arricchite {done}/{total}")
    withq = conn.execute(
        "SELECT count(*) FROM hackinga0_archive WHERE question_text IS NOT NULL AND question_text != ''"
    ).fetchone()[0]
    print(f"[enrich] totale genitori processati={total}, righe con domanda={withq}")


def import_legacy() -> int:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    hist = data.get("historical", [])
    conn = get_conn()
    inserted = 0
    for x in hist:
        text = (x.get("text") or "").strip()
        if not text:
            continue
        parent = x.get("in_reply_to_tweet_id")
        # il seed non ha il testo della domanda -> usiamo il csv_row se presente? no.
        upsert(conn, {
            "id": str(x.get("id")),
            "text": text,
            "created_at": x.get("created_at"),
            "author_id": x.get("author_id"),
            "is_reply": 1 if parent else 0,
            "in_reply_to_tweet_id": parent,
            "question_text": None,
            "question_author_id": None,
            "question_user_handle": None,
            "conversation_id": x.get("conversation_id"),
            "lang": None,
            "source": "seed:" + (x.get("source") or "unknown"),
            "fetched_at": now_iso(),
        })
        inserted += 1
    conn.commit()
    tot = conn.execute("SELECT count(*) FROM hackinga0_archive").fetchone()[0]
    print(f"[legacy] importati {inserted} tweet storici; totale archivio={tot}")
    return 0


def import_probes() -> int:
    """Importa le coppie Q→A reali del seed (probe -> reply di @HackingA0).

    Ogni voce: id = reply_id (o sintetico), text = reply (risposta),
    question_text = probe (domanda), in_reply_to_tweet_id = tweet_id della probe.
    """
    data = json.loads(SEED.read_text(encoding="utf-8"))
    probes = data.get("probes", [])
    conn = get_conn()
    inserted = 0
    skipped = 0
    for i, p in enumerate(probes):
        reply = (p.get("reply_text") or "").strip()
        question = (p.get("text") or "").strip()
        if not reply or not question:
            continue
        rid = p.get("reply_id")
        if not rid:
            rid = f"probe:{p.get('probe_id', i)}"
        exists = conn.execute(
            "SELECT 1 FROM hackinga0_archive WHERE id=?", (str(rid),)).fetchone()
        if exists:
            skipped += 1
            continue
        upsert(conn, {
            "id": str(rid),
            "text": reply,
            "created_at": None,
            "author_id": "2051911746969812998",
            "is_reply": 1,
            "in_reply_to_tweet_id": str(p.get("tweet_id")) if p.get("tweet_id") else None,
            "question_text": question,
            "question_author_id": None,
            "question_user_handle": None,
            "conversation_id": None,
            "lang": None,
            "source": "seed:probe-" + str(p.get("source", "unknown")),
            "fetched_at": now_iso(),
        })
        inserted += 1
    conn.commit()
    withq = conn.execute(
        "SELECT count(*) FROM hackinga0_archive "
        "WHERE question_text IS NOT NULL AND question_text != ''").fetchone()[0]
    tot = conn.execute("SELECT count(*) FROM hackinga0_archive").fetchone()[0]
    print(f"[probes] importate {inserted} coppie Q→A (skippate {skipped}); "
          f"totale archivio={tot}, con_domanda={withq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
