#!/usr/bin/env python3
"""
analytics/tracker.py — Servidor de redirección con analytics de clicks.

Ejecutar:  uvicorn analytics.tracker:app --host 0.0.0.0 --port 8080

Endpoints:
  GET /r/{deal_id}?canal=telegram   → registra click y redirige a URL de afiliado
  GET /stats/{deal_id}              → JSON con clicks de un deal
  GET /stats                        → JSON con resumen global
"""

import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "flipazo_deals.db")

app = FastAPI(title="Flipazo Analytics", docs_url=None, redoc_url=None)


# ── Helpers BD ────────────────────────────────────────────────────────────────

def _get_url_afiliado(deal_id: str) -> str | None:
    """Devuelve la URL de afiliado almacenada para el deal_id dado."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT url_afiliado FROM deals_publicados WHERE deal_id = ?",
                (deal_id,),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _registrar_click(deal_id: str, canal: str, ip: str):
    """Inserta un registro en la tabla clicks."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """INSERT INTO clicks (deal_id, canal, ip, ts)
                   VALUES (?, ?, ?, ?)""",
                (deal_id, canal[:32], ip[:64], datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
    except Exception:
        pass  # nunca bloquear la redirección por un fallo de logging


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/r/{deal_id}")
async def redirect_click(deal_id: str, request: Request, canal: str = "directo"):
    """Registra el click y redirige a la URL de afiliado."""
    url = _get_url_afiliado(deal_id)
    if not url:
        raise HTTPException(status_code=404, detail="Deal no encontrado o expirado")

    ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "desconocida")
    _registrar_click(deal_id, canal, ip)

    return RedirectResponse(url=url, status_code=302)


@app.get("/stats/{deal_id}")
async def stats_deal(deal_id: str):
    """Clicks de un deal concreto agrupados por canal."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            # Info del deal
            deal = con.execute(
                "SELECT titulo, tienda, precio, tipo, publicado_en FROM deals_publicados WHERE deal_id = ?",
                (deal_id,),
            ).fetchone()
            if not deal:
                raise HTTPException(status_code=404, detail="Deal no encontrado")

            # Clicks por canal
            rows = con.execute(
                "SELECT canal, COUNT(*) as n FROM clicks WHERE deal_id = ? GROUP BY canal ORDER BY n DESC",
                (deal_id,),
            ).fetchall()

        return {
            "deal_id": deal_id,
            "titulo": deal[0],
            "tienda": deal[1],
            "precio": deal[2],
            "tipo": deal[3],
            "publicado_en": deal[4],
            "clicks_total": sum(r[1] for r in rows),
            "clicks_por_canal": {r[0]: r[1] for r in rows},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats")
async def stats_global():
    """Resumen global: deals con más clicks en las últimas 72h."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(
                """
                SELECT c.deal_id,
                       d.titulo,
                       d.tienda,
                       d.tipo,
                       COUNT(*) AS clicks
                FROM clicks c
                LEFT JOIN deals_publicados d ON c.deal_id = d.deal_id
                WHERE c.ts >= datetime('now', '-72 hours')
                GROUP BY c.deal_id
                ORDER BY clicks DESC
                LIMIT 20
                """
            ).fetchall()

        return {
            "periodo": "últimas 72h",
            "deals": [
                {
                    "deal_id": r[0],
                    "titulo": r[1],
                    "tienda": r[2],
                    "tipo": r[3],
                    "clicks": r[4],
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}
