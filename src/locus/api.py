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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

logger = logging.getLogger(__name__)

_SEED_PATH = "src/locus/data/locus_seed.json"
_DIST_PATH = Path("web") / "dist"

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
            "SELECT id, property_key, text, reply_text, score, status, classification "
            "FROM probes WHERE status = 'classified' ORDER BY score DESC LIMIT ?",
            (limit,),
        )
        items = []
        for r in rows:
            d = _row_to_dict(r)
            d["classification"] = _parse_classification(d.pop("classification"))
            items.append(d)
        return items

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
    async def poll() -> Dict[str, Any]:
        engine = _require_engine(app)
        try:
            replies = await engine.target.poll_replies()
        except TwitterError as exc:
            raise HTTPException(502, f"poll failed: {exc}") from exc
        return {"replies": replies}

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


# ── Helpers ────────────────────────────────────────────────────


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


def _require_engine(app: FastAPI):
    if app.state.engine is None:
        raise HTTPException(503, "engine not initialized")
    return app.state.engine


# Entrypoint per uvicorn:  uvicorn locus.api:app
app = create_app()
