"""
api.py — Servidor FastAPI para flipazo.es

Endpoints:
  GET /api/deals?limit=50&offset=0   → lista de deals publicados (JSON)
  GET /api/deals/count               → total de deals en BD
  GET /r/{deal_id}                   → redirect de afiliado con tracking
  GET /health                        → healthcheck

Arranque:
  venv/bin/uvicorn api:app --host 0.0.0.0 --port 8080
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

load_dotenv()

DB_PATH = "flipazo_deals.db"

app = FastAPI(title="Flipazo API", version="1.0.0")

# CORS — permite llamadas desde flipazo.es (Vercel) y localhost (dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://flipazo.es", "https://www.flipazo.es", "http://localhost", "http://127.0.0.1"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── DB helper ─────────────────────────────────────────────────────────────────

def _get_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


# ── Startup: migraciones en caliente ─────────────────────────────────────────

@app.on_event("startup")
def _ensure_vote_columns():
    """Añade columnas votes_up/votes_down si no existen."""
    with _get_db() as con:
        for col_def in ["votes_up INTEGER DEFAULT 0", "votes_down INTEGER DEFAULT 0"]:
            try:
                con.execute(f"ALTER TABLE deals_publicados ADD COLUMN {col_def}")
            except Exception:
                pass  # columna ya existe
        con.commit()


# ── Modelos ───────────────────────────────────────────────────────────────────

class VoteBody(BaseModel):
    direction: str  # "up" | "down"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/deals")
def get_deals(
    limit:  int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0,  ge=0),
    tipo:   Optional[str] = Query(default=None, description="OFERTA | REVENTA"),
    tienda: Optional[str] = Query(default=None),
):
    """
    Devuelve deals publicados ordenados del más reciente al más antiguo.
    Filtra opcionalmente por tipo o tienda.
    """
    where_clauses = []
    params: list = []

    if tipo:
        where_clauses.append("tipo = ?")
        params.append(tipo.upper())
    if tienda:
        where_clauses.append("tienda = ?")
        params.append(tienda)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            rowid,
            deal_id         AS id,
            titulo,
            tienda,
            tipo,
            precio          AS precio_actual,
            precio_original,
            descuento_pct,
            imagen_url,
            url_afiliado    AS url_affiliate,
            precio_wallapop,
            beneficio_neto,
            razonamiento,
            COALESCE(votes_up,   0) AS votes_up,
            COALESCE(votes_down, 0) AS votes_down,
            publicado_en    AS timestamp
        FROM deals_publicados
        {where_sql}
        ORDER BY publicado_en DESC
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]

    with _get_db() as con:
        rows = con.execute(sql, params).fetchall()

    deals = []
    for r in rows:
        d = dict(r)
        # Aseguramos que los campos numéricos nunca sean null
        d["precio_actual"]   = d["precio_actual"]   or 0.0
        d["precio_original"] = d["precio_original"] or 0.0
        d["descuento_pct"]   = d["descuento_pct"]   or 0
        d["precio_wallapop"] = d["precio_wallapop"] or 0.0
        d["beneficio_neto"]  = d["beneficio_neto"]  or 0.0
        d["imagen_url"]      = d["imagen_url"]       or ""
        d["razonamiento"]    = d["razonamiento"]     or ""
        d["votes_up"]        = d["votes_up"]         or 0
        d["votes_down"]      = d["votes_down"]       or 0
        deals.append(d)

    return JSONResponse(content=deals)


@app.get("/api/deals/count")
def get_count():
    with _get_db() as con:
        total = con.execute("SELECT COUNT(*) FROM deals_publicados").fetchone()[0]
    return {"total": total}


@app.post("/api/deals/{deal_id}/vote")
def vote_deal(deal_id: str, body: VoteBody):
    """
    Registra un voto (up/down) en un deal.
    El cliente controla el anti-spam con localStorage.
    Devuelve los contadores actualizados.
    """
    if body.direction not in ("up", "down"):
        return JSONResponse(status_code=400, content={"error": "direction must be 'up' or 'down'"})

    col = "votes_up" if body.direction == "up" else "votes_down"
    with _get_db() as con:
        updated = con.execute(
            f"UPDATE deals_publicados SET {col} = {col} + 1 WHERE deal_id = ?",
            (deal_id,),
        ).rowcount
        if updated == 0:
            return JSONResponse(status_code=404, content={"error": "deal not found"})
        con.commit()
        row = con.execute(
            "SELECT COALESCE(votes_up,0) AS votes_up, COALESCE(votes_down,0) AS votes_down "
            "FROM deals_publicados WHERE deal_id = ?",
            (deal_id,),
        ).fetchone()
    return {"votes_up": row["votes_up"], "votes_down": row["votes_down"]}


@app.get("/r/{deal_id}")
def redirect_afiliado(deal_id: str, request: Request, canal: str = "web"):
    """
    Redirect de afiliado con tracking de clicks.
    Registra el click en BD y redirige a la URL de afiliado.
    """
    with _get_db() as con:
        row = con.execute(
            "SELECT url_afiliado FROM deals_publicados WHERE deal_id = ?",
            (deal_id,)
        ).fetchone()

        if not row or not row["url_afiliado"]:
            return JSONResponse(status_code=404, content={"error": "deal no encontrado"})

        # Registrar click
        con.execute(
            "INSERT INTO clicks (deal_id, canal, ip, ts) VALUES (?, ?, ?, ?)",
            (
                deal_id,
                canal,
                request.client.host if request.client else "unknown",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()

    return RedirectResponse(url=row["url_afiliado"], status_code=302)
