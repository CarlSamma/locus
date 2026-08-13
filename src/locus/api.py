"""Locus Web API — backend FastAPI per la dashboard web di Locus.

Espone REST sul database e sull'engine reali (stessa logica di ``cli.py``),
servendo anche il frontend React (``web/dist``) quando compilato.

Endpoints principali:
    GET  /api/status               Riepilogo entropia + conteggi (= ``locus status``)
    GET  /api/properties           Universe proprietà
    GET  /api/frames               Frame attivi
    GET  /api/probes               Albero di attacco (filtrabile per status)
    GET  /api/review               Sonde classificate top-score (= ``locus review``)
    GET  /api/ledger               Log immutabile degli outcome
    GET  /api/intel                Stream intel (filtrabile per kind)
    GET  /api/sessions             Sessioni di campagna
    POST /api/run                  Avvia una sessione in background (dry-run/live)
    GET  /api/run/{id}             Stato/progresso della sessione
    POST /api/run/{id}/stop        Ferma la sessione
    POST /api/probes/generate      Genera una probe (LLM)
    POST /api/probes/post          Posta una probe su X
    POST /api/probes/poll          Legge le risposte del target
    GET  /api/config               Config non-segreta
    GET  /api/health               Stato del gateway LLM

Avvio:  uvicorn locus.api:app --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from locus.classify import Classifier
from locus.config import LocusConfig
from locus.db import Database
from locus.llm import LLMClient
from locus.memory import Memory
from locus.models import Frame, Property
from locus.select import in_phase5, remaining_entropy, total_remaining_entropy
from locus.target import TargetClient
from locus.trust import sanitize_untrusted

logger = logging.getLogger(__name__)

_SEED_PATH = "src/locus/data/locus_seed.json"
_DIST_PATH = Path("web") / "dist"
_ARHIVE_DB_PATH = Path("data") / "hackinga0_archive.db"

_PROPERTY_COLS = "key, weight, prior_entropy, state, votes, value, notes"


def _row_to_dict(row) -> Dict[str, Any]:
    return dict(row) if hasattr(row, "keys") else {k: row[k] for k in row.keys()}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_classification(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {"pattern": "unknown", "boolean": False, "score": 0, "leaks": [], "rationale": ""}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {}
    except (TypeError, ValueError):
        return {"pattern": "unknown", "boolean": False, "score": 0, "leaks": [], "rationale": ""}
    data.setdefault("pattern", "unknown")
    data.setdefault("boolean", False)
    data.setdefault("score", 0)
    data.setdefault("leaks", [])
    data.setdefault("rationale", "")
    return data


# ── Schemi request ─────────────────────────────────────────────


class RunRequest(BaseModel):
    dry_run: bool = True
    max_probes: Optional[int] = None
    session_id: Optional[str] = None


class GenerateRequest(BaseModel):
    property_key: str
    frame_alias: str = "neutral"


class PostRequest(BaseModel):
    text: str
    session_id: Optional[str] = None
    property_key: Optional[str] = None
    frame_alias: str = "neutral"


class PollRequest(BaseModel):
    since_id: Optional[str] = None


# ── Factory app ────────────────────────────────────────────────


def create_app(
    config: Optional[LocusConfig] = None,
    *,
    engine: Optional[Any] = None,
    transport: Optional[Any] = None,
    x_transport: Optional[Any] = None,
    seed: bool = True,
    dist_path: Optional[Path] = None,
) -> FastAPI:
    """Costruisce l'app FastAPI.

    ``engine`` (opzionale) permette ai test di iniettare un engine con
    transport fake; altrimenti viene costruito dal config come in cli.py.
    """
    cfg = config or LocusConfig()
    dist = dist_path or _DIST_PATH

    app = FastAPI(title="Locus Web", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.config = cfg
    app.state.engine = engine
    app.state.runs = {}  # type: ignore[attr-defined]

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        if _app.state.engine is None:
            db = Database()
            await db.initialize(cfg.db_path)
            if seed:
                await _seed(db, cfg)
            llm = LLMClient(cfg, transport=transport)
            target = TargetClient(cfg, transport=x_transport)
            from locus.engine import Engine

            _app.state.engine = Engine(
                cfg,
                db,
                llm,
                target,
                classifier=Classifier(llm, cfg),
                memory=Memory(db),
            )
        yield
        for task in _app.state.runs.values():
            task.cancel()
        if _app.state.engine is not None:
            await _app.state.engine.db.close()

    app.router.lifespan_context = _lifespan

    # API routes FIRST, so /api/* wins over the SPA catch-all below.
    _register_routes(app)

    # ── Static SPA (se compilata) ────────────────────────────

    if dist.exists() and (dist / "index.html").exists():
        assets_dir = dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}")
        async def _spa(full_path: str):
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


# ── Seed ───────────────────────────────────────────────────────


async def _seed(db: Database, config: LocusConfig) -> None:
    import os as _os

    if _os.path.exists(_SEED_PATH):
        from locus.seed import import_seed, load_seed

        await import_seed(db, load_seed(_SEED_PATH))
        return
    import json as _json

    with open(config.properties_path, encoding="utf-8") as f:
        await db.seed_properties(_json.load(f))


def _register_routes(app: FastAPI) -> None:
    """Registra tutti gli endpoint REST sull'app."""
    from locus.exceptions import TwitterError

    # ── Status / riepilogo ──────────────────────────────────

    @app.get("/api/status")
    async def status() -> Dict[str, Any]:
        engine = _require_engine(app)
        rows = await engine.db.fetchall(f"SELECT {_PROPERTY_COLS} FROM properties")
        properties = [_row_to_dict(r) for r in rows]
        for p in properties:
            p["remaining_entropy"] = remaining_entropy(
                Property(
                    key=p["key"],
                    weight=p["weight"],
                    prior_entropy=p["prior_entropy"],
                    state=p["state"],
                    votes=p["votes"],
                    value=p["value"],
                    notes=p["notes"],
                )
            )
        total = total_remaining_entropy(
            [Property(**p) for p in properties]
        )
        in5, total5 = in_phase5(
            [Property(**p) for p in properties],
            app.state.config.phase5_entropy_threshold,
        )
        counts: Dict[str, int] = {}
        for table in ("sessions", "probes", "intel", "ledger", "frames", "memory_entries"):
            row = await engine.db.fetchone(f"SELECT COUNT(*) AS c FROM {table}")
            counts[table] = row["c"] if row else 0
        return {
            "properties": properties,
            "total_remaining_entropy": total,
            "in_phase5": in5,
            "phase5_threshold": app.state.config.phase5_entropy_threshold,
            "target": app.state.config.target_handle,
            "counts": counts,
        }

    # ── Proprietà ───────────────────────────────────────────

    @app.get("/api/properties")
    async def properties() -> List[Dict[str, Any]]:
        engine = _require_engine(app)
        rows = await engine.db.fetchall(
            f"SELECT {_PROPERTY_COLS} FROM properties ORDER BY prior_entropy DESC"
        )
        result = []
        for r in rows:
            d = _row_to_dict(r)
            d["remaining_entropy"] = remaining_entropy(
                Property(
                    key=d["key"],
                    weight=d["weight"],
                    prior_entropy=d["prior_entropy"],
                    state=d["state"],
                    votes=d["votes"],
                    value=d["value"],
                    notes=d["notes"],
                )
            )
            result.append(d)
        return result

    # ── Frame ───────────────────────────────────────────────

    @app.get("/api/frames")
    async def frames() -> List[Dict[str, Any]]:
        engine = _require_engine(app)
        rows = await engine.db.fetchall(
            "SELECT alias, persona, prompt_template, status, created_at FROM frames "
            "WHERE status = 'active' ORDER BY alias"
        )
        return [_row_to_dict(r) for r in rows]

    # ── Sonde (albero di attacco) ───────────────────────────

    @app.get("/api/probes")
    async def probes(
        status_filter: Optional[str] = Query(None, alias="status"),
        property_key: Optional[str] = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        engine = _require_engine(app)
        where: List[str] = []
        params: List[Any] = []
        if status_filter:
            where.append("status = ?")
            params.append(status_filter)
        if property_key:
            where.append("property_key = ?")
            params.append(property_key)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total_row = await engine.db.fetchone(f"SELECT COUNT(*) AS c FROM probes {clause}", tuple(params))
        rows = await engine.db.fetchall(
            f"SELECT id, session_id, property_key, frame_alias, text, tweet_id, reply_text, "
            f"score, status, created_at, classification FROM probes {clause} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        items = []
        for r in rows:
            d = _row_to_dict(r)
            d["classification"] = _parse_classification(d.pop("classification"))
            items.append(d)
        return {"total": total_row["c"] if total_row else 0, "items": items}

    # ── Review (sonde classificate top) ─────────────────────

    @app.get("/api/review")
    async def review(limit: int = Query(10, ge=1, le=100)) -> List[Dict[str, Any]]:
        engine = _require_engine(app)
        rows = await engine.db.fetchall(
            "SELECT id, property_key, frame_alias, text, reply_text, score, status, classification "
            "FROM probes WHERE status = 'classified' ORDER BY score DESC LIMIT ?",
            (limit,),
        )
        items = []
        for r in rows:
            d = _row_to_dict(r)
            d["classification"] = _parse_classification(d.pop("classification"))
            items.append(d)
        return items

    @app.post("/api/review/{probe_id}/confirm")
    async def review_confirm(probe_id: str) -> Dict[str, Any]:
        """HITL: approva la classificazione → applica l'evidenza al ledger e alla proprietà."""
        engine = _require_engine(app)
        await _apply_review(engine, probe_id, verdict="confirm")
        return {"probe_id": probe_id, "status": "confirmed"}

    @app.post("/api/review/{probe_id}/deny")
    async def review_deny(probe_id: str) -> Dict[str, Any]:
        """HITL: scarta la classificazione → nessuna evidenza applicata, probe marcata denied."""
        engine = _require_engine(app)
        await _apply_review(engine, probe_id, verdict="deny")
        return {"probe_id": probe_id, "status": "denied"}

    # ── Ledger ──────────────────────────────────────────────

    @app.get("/api/ledger")
    async def ledger(limit: int = Query(200, ge=1, le=1000)) -> List[Dict[str, Any]]:
        engine = _require_engine(app)
        rows = await engine.db.fetchall(
            "SELECT id, property_key, outcome, probe_id, ts, note FROM ledger "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_dict(r) for r in rows]

    # ── Intel ───────────────────────────────────────────────

    @app.get("/api/intel")
    async def intel(
        kind: Optional[str] = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        engine = _require_engine(app)
        where = "WHERE kind = ?" if kind else ""
        params = (kind,) if kind else ()
        total_row = await engine.db.fetchone(f"SELECT COUNT(*) AS c FROM intel {where}", params)
        rows = await engine.db.fetchall(
            f"SELECT id, session_id, kind, text, entropy_before, entropy_after, note, ts "
            f"FROM intel {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
            params + (limit, offset),
        )
        return {"total": total_row["c"] if total_row else 0, "items": [_row_to_dict(r) for r in rows]}

    # ── Sessioni ────────────────────────────────────────────

    @app.get("/api/sessions")
    async def sessions() -> List[Dict[str, Any]]:
        engine = _require_engine(app)
        rows = await engine.db.fetchall(
            "SELECT id, started_at, ended_at, probes_total, status, target FROM sessions "
            "ORDER BY started_at DESC LIMIT 100"
        )
        return [_row_to_dict(r) for r in rows]

    @app.post("/api/sessions")
    async def create_session() -> Dict[str, Any]:
        engine = _require_engine(app)
        session_id = await engine.start_session()
        return {"session_id": session_id, "status": "running"}

    # ── Run (sessione in background) ────────────────────────

    @app.post("/api/run")
    async def run(req: RunRequest) -> Dict[str, Any]:
        engine = _require_engine(app)
        session_id = req.session_id or await engine.start_session()
        if session_id in app.state.runs and not app.state.runs[session_id].done():
            raise HTTPException(409, f"session {session_id} already running")

        async def _runner() -> Any:
            try:
                return await engine.run_session(
                    max_probes=req.max_probes,
                    dry_run=req.dry_run,
                    session_id=session_id,
                )
            except asyncio.CancelledError:
                await engine.end_session(session_id, status="paused")
                raise

        task = asyncio.create_task(_runner())
        app.state.runs[session_id] = task
        logger.info("run_started session_id=%s dry_run=%s", session_id, req.dry_run)
        return {
            "session_id": session_id,
            "dry_run": req.dry_run,
            "status": "running",
        }

    @app.get("/api/run/{session_id}")
    async def run_status(session_id: str) -> Dict[str, Any]:
        engine = _require_engine(app)
        task = app.state.runs.get(session_id)
        row = await engine.db.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            raise HTTPException(404, "session not found")
        session = _row_to_dict(row)
        probe_row = await engine.db.fetchone(
            "SELECT COUNT(*) AS c FROM probes WHERE session_id = ?", (session_id,)
        )
        session["probes_total"] = probe_row["c"] if probe_row else session.get("probes_total", 0)
        if task is not None and task.done():
            try:
                session["result"] = len(task.result())
            except Exception as exc:
                session["result"] = 0
                session["error"] = str(exc)
        return session

    @app.post("/api/run/{session_id}/stop")
    async def stop_run(session_id: str) -> Dict[str, Any]:
        task = app.state.runs.get(session_id)
        if task is None or task.done():
            raise HTTPException(404, "no running session")
        task.cancel()
        return {"session_id": session_id, "status": "stopping"}

    # ── Probe Lab (generate / post / poll) ──────────────────

    @app.post("/api/probes/generate")
    async def generate(req: GenerateRequest) -> Dict[str, Any]:
        engine = _require_engine(app)
        prop_row = await engine.db.fetchone(
            "SELECT key, weight, prior_entropy, state, votes, value, notes FROM properties WHERE key = ?",
            (req.property_key,),
        )
        if prop_row is None:
            raise HTTPException(404, "property not found")
        _prop_cols = ("key", "weight", "prior_entropy", "state", "votes", "value", "notes")
        prop = Property(**{k: prop_row[k] for k in _prop_cols})
        frame = None
        if req.frame_alias:
            frow = await engine.db.fetchone(
                "SELECT alias, persona, prompt_template, status FROM frames WHERE alias = ?",
                (req.frame_alias,),
            )
            if frow is not None:
                frame = Frame(
                    alias=frow["alias"],
                    persona=frow["persona"],
                    prompt_template=frow["prompt_template"],
                    status=frow["status"],
                )
        text = await engine.generator.generate(prop, frame)
        return {"text": text, "property_key": req.property_key, "frame_alias": req.frame_alias}

    @app.post("/api/probes/post")
    async def post(req: PostRequest) -> Dict[str, Any]:
        engine = _require_engine(app)
        if not req.text.strip():
            raise HTTPException(400, "empty probe text")
        try:
            tweet_id = await engine.target.post_probe(req.text)
        except TwitterError as exc:
            raise HTTPException(502, f"post failed: {exc}") from exc
        session_id = req.session_id or await engine.start_session()
        probe_id = await _persist_probe(engine, session_id, req, tweet_id)
        url = f"https://x.com/{app.state.config.target_handle.lstrip('@')}/status/{tweet_id}"
        return {"tweet_id": tweet_id, "url": url, "session_id": session_id, "probe_id": probe_id}

    @app.post("/api/probes/poll")
    async def poll(req: Optional[PollRequest] = None) -> Dict[str, Any]:
        engine = _require_engine(app)
        # since_id: accettato dal body oppure derivato dal DB come cursore
        # incrementale (stessa logica di Engine.start_session), così i poll
        # ripetuti restituiscono solo le reply più recenti dell'ultima vista.
        since_id = req.since_id if req is not None else None
        since_id = since_id or await _derive_since_id(engine)
        try:
            replies = await engine.target.poll_replies(since_id=since_id)
        except TwitterError as exc:
            raise HTTPException(502, f"poll failed: {exc}") from exc
        payload: Dict[str, Any] = {"replies": replies}
        if since_id:
            # Riecheggia il cursore usato (additivo, retro-compatibile con la UI).
            payload["since_id"] = since_id
        return payload

    # ── Config / health ─────────────────────────────────────

    @app.get("/api/config")
    async def config() -> Dict[str, Any]:
        cfg = app.state.config
        return {
            "target_handle": cfg.target_handle,
            "our_bot_handle": cfg.our_bot_handle,
            "llm_model_primary": cfg.llm_model_primary,
            "llm_model_hard": cfg.llm_model_hard,
            "llm_api_base": cfg.llm_api_base,
            "poll_interval_seconds": cfg.poll_interval_seconds,
            "poll_timeout_seconds": cfg.poll_timeout_seconds,
            "max_probes_per_session": cfg.max_probes_per_session,
            "phase5_entropy_threshold": cfg.phase5_entropy_threshold,
            "similarity_threshold": cfg.similarity_threshold,
            "dedup_top_k": cfg.dedup_top_k,
        }

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        engine = _require_engine(app)
        health_data = {}
        try:
            health_data = engine.llm.get_health_status()
        except Exception as exc:  # pragma: no cover - resilience
            health_data = {"error": str(exc)}
        return {"ok": True, "llm": health_data}

    # ── HackingA0 Archive (Q→A database, DB separato) ─────────
    @app.get("/api/hackinga0/stats")
    async def hackinga0_stats() -> Dict[str, Any]:
        db = _ARHIVE_DB_PATH
        if not db.exists():
            return {"exists": False, "total": 0, "replies": 0, "with_question": 0,
                    "earliest": None, "latest": None}
        async with aiosqlite.connect(db) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT COUNT(*) c FROM hackinga0_archive")
            total = await cur.fetchone()
            cur = await c.execute("SELECT COUNT(*) c FROM hackinga0_archive WHERE is_reply=1")
            replies = await cur.fetchone()
            cur = await c.execute(
                "SELECT COUNT(*) c FROM hackinga0_archive "
                "WHERE question_text IS NOT NULL AND question_text != ''")
            with_q = await cur.fetchone()
            cur = await c.execute("SELECT MIN(created_at), MAX(created_at) FROM hackinga0_archive")
            rng = await cur.fetchone()
        return {"exists": True, "total": total["c"], "replies": replies["c"],
                "with_question": with_q["c"], "earliest": rng[0], "latest": rng[1]}

    @app.get("/api/hackinga0/qa")
    async def hackinga0_qa(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        kind: str = Query("all"),  # all | reply | post
        has_question: bool = Query(False),
    ) -> Dict[str, Any]:
        db = _ARHIVE_DB_PATH
        if not db.exists():
            return {"total": 0, "items": []}
        where, params = [], []
        if kind == "reply":
            where.append("is_reply = 1")
        elif kind == "post":
            where.append("is_reply = 0")
        if has_question:
            where.append("question_text IS NOT NULL AND question_text != ''")
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        async with aiosqlite.connect(db) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute(
                f"SELECT COUNT(*) c FROM hackinga0_archive {wsql}", params)
            total = await cur.fetchone()
            cur = await c.execute(
                f"SELECT * FROM hackinga0_archive {wsql} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset])
            rows = await cur.fetchall()
        return {"total": total["c"], "items": [dict(r) for r in rows]}

    @app.get("/api/hackinga0/search")
    async def hackinga0_search(
        q: str = Query(..., min_length=1, max_length=200),
        limit: int = Query(50, ge=1, le=200),
    ) -> List[Dict[str, Any]]:
        db = _ARHIVE_DB_PATH
        if not db.exists():
            return []
        like = f"%{q}%"
        async with aiosqlite.connect(db) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute(
                "SELECT * FROM hackinga0_archive "
                "WHERE text LIKE ? OR question_text LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (like, like, limit))
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ── Helpers ────────────────────────────────────────────────────


async def _derive_since_id(engine: Any) -> Optional[str]:
    """Deriva il cursore since_id dalla reply più recente persistita.

    Scansiona ``reply_id`` su tutte le probe e restituisce il massimo
    (confronto numerico, come in ``Engine.start_session``): i poll successivi
    chiederanno al trasporto solo mention più nuove di questo id.
    """
    rows = await engine.db.fetchall(
        "SELECT reply_id FROM probes WHERE reply_id IS NOT NULL AND reply_id != ''"
    )
    newest: Optional[int] = None
    for row in rows:
        try:
            rid = int(row["reply_id"])
        except (TypeError, ValueError):
            continue
        if newest is None or rid > newest:
            newest = rid
    return str(newest) if newest is not None else None


async def _persist_probe(
    engine: Any, session_id: str, req: PostRequest, tweet_id: str
) -> str:
    import uuid as _uuid

    probe_id = str(_uuid.uuid4())
    now = _utcnow_iso()
    await engine.db.execute(
        """INSERT INTO probes
           (id, session_id, property_key, frame_alias, text, tweet_id, posted_at, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            probe_id,
            session_id,
            req.property_key or "manual",
            req.frame_alias,
            req.text,
            tweet_id,
            now,
            "posted",
            now,
        ),
    )
    await engine.db.commit()
    return probe_id


async def _apply_review(engine: Any, probe_id: str, *, verdict: str) -> None:
    """Applica l'esito HITL su una probe classificata.

    ``confirm`` → l'evidenza della classificazione viene applicata (ledger,
    stato/voti proprietà, leak → intel), replicando la logica di
    ``Engine._extract``; la probe passa a stato ``confirmed``.

    ``deny`` → la classificazione è scartata: nessuna evidenza viene applicata
    al ledger né alla proprietà; la probe passa a stato ``denied``.

    Lancia HTTPException 404 se la probe non esiste, 409 se non è nello stato
    ``classified`` atteso.
    """
    row = await engine.db.fetchone(
        "SELECT id, session_id, property_key, status, reply_text, classification FROM probes WHERE id = ?",
        (probe_id,),
    )
    if row is None:
        raise HTTPException(404, "probe not found")
    if row["status"] != "classified":
        raise HTTPException(409, "probe is not classified")

    _outcome_by_pattern = {
        "yes": "confirmed",
        "no": "denied",
        "block": "blocked",
        "evasive": "partial",
        "ambiguous": "partial",
    }
    ts = _utcnow_iso()
    if verdict == "confirm":
        classification = _parse_classification(row["classification"])
        outcome = _outcome_by_pattern.get(classification["pattern"], "partial")
        probe_id_v = row["id"]
        await engine.db.execute(
            "INSERT INTO ledger (id, property_key, outcome, probe_id, ts, note) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                row["property_key"],
                outcome,
                probe_id_v,
                ts,
                f"HITL confirm: {classification.get('rationale', '')}",
            ),
        )
        await engine.db.execute(
            "UPDATE probes SET status = 'confirmed' WHERE id = ?",
            (probe_id_v,),
        )
        # Aggiorna la proprietà solo sugli esiti decisivi (yes/no), come
        # in Engine._extract.
        if classification["pattern"] in ("yes", "no"):
            prop_row = await engine.db.fetchone(
                "SELECT key, state, votes FROM properties WHERE key = ?",
                (row["property_key"],),
            )
            if prop_row is not None:
                state = "confirmed" if outcome == "confirmed" else "denied"
                votes = (prop_row["votes"] or 0) + 1
                await engine.db.execute(
                    "UPDATE properties SET state = ?, votes = ? WHERE key = ?",
                    (state, votes, row["property_key"]),
                )
        # Leak → intel (sanitizzati, come in _extract).
        for leak in classification.get("leaks", []):
            await engine.db.execute(
                "INSERT INTO intel (id, session_id, kind, text, note, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    row["session_id"],
                    "leak",
                    sanitize_untrusted(str(leak)),
                    "HITL confirm",
                    ts,
                ),
            )
    else:  # deny
        await engine.db.execute(
            "UPDATE probes SET status = 'denied' WHERE id = ?",
            (row["id"],),
        )
    await engine.db.commit()


def _require_engine(app: FastAPI):
    if app.state.engine is None:
        raise HTTPException(503, "engine not initialized")
    return app.state.engine


# Entrypoint per uvicorn:  uvicorn locus.api:app
app = create_app()
