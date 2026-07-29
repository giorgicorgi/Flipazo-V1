#!/usr/bin/env python3
"""
telegram_para_ti.py — Resumen diario "Para ti" por Telegram.

Para cada usuario que haya vinculado su cuenta con el bot (tabla `user_prefs`,
`tg_chat_id` no vacío y `tg_enabled = 1`), busca los deals publicados desde el
último envío que encajan con sus preferencias y le manda un único mensaje.

Se ejecuta por cron una vez al día:
    30 8 * * *  /home/flipazo/app/venv/bin/python /home/flipazo/app/scripts/telegram_para_ti.py >> /home/flipazo/app/paratí.log 2>&1

Filtra por categoría, tienda y rango de precio. Las SUBcategorías (TV, Audio,
Running…) son un refinamiento solo de la web: sus regex viven en index.html y
duplicarlas aquí se desincronizaría, así que el resumen se queda en el nivel de
categoría — más ancho, nunca más estrecho de lo que el usuario pidió.

Idempotente: se apoya en `tg_last_sent`, así que relanzarlo el mismo día no
reenvía lo ya mandado.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_PATH        = os.getenv("DB_PATH", os.path.join(BASE_DIR, "flipazo_deals.db"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
SITE           = "https://www.flipazo.es"
MAX_DEALS      = 6        # tope por mensaje: un resumen, no un muro
VENTANA_HORAS  = 36       # margen sobre 24h para no perder deals si falla un día
DRY_RUN        = "--dry-run" in sys.argv


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def tg_send(chat_id: str, texto: str) -> bool:
    if DRY_RUN:
        log(f"  [dry-run] → chat {chat_id}\n{texto}\n")
        return True
    if not TELEGRAM_TOKEN:
        log("  ⚠️  TELEGRAM_TOKEN no configurado")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": texto,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            log(f"  ⚠️  Telegram {r.status_code}: {r.text[:160]}")
        return r.ok
    except Exception as e:
        log(f"  ⚠️  Error enviando: {e}")
        return False


def _json(txt, fallback):
    try:
        v = json.loads(txt or "")
        return v if isinstance(v, type(fallback)) else fallback
    except Exception:
        return fallback


def encaja(deal: sqlite3.Row, cats: list, stores: list, pmin: float, pmax) -> bool:
    if cats and (deal["categoria"] or "") not in cats:
        return False
    if stores and (deal["tienda"] or "") not in stores:
        return False
    p = deal["precio"] or 0
    if pmin and p < pmin:
        return False
    if pmax is not None and p > pmax:
        return False
    return True


def fmt_precio(v) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def construir_mensaje(nombre: str, deals: list) -> str:
    saludo = f"👋 Buenos días{', ' + nombre if nombre else ''}"
    lineas = [f"{saludo} — esto es lo tuyo de hoy:\n"]
    for d in deals:
        pct  = d["descuento_pct"] or 0
        prec = fmt_precio(d["precio"] or 0)
        orig = f" <s>{fmt_precio(d['precio_original'])}</s>" if (d["precio_original"] or 0) > (d["precio"] or 0) else ""
        url  = f"{SITE.replace('www.', '')}/r/{d['deal_id']}?canal=telegram_parati"
        titulo = (d["titulo"] or "")[:80]
        lineas.append(f"🔥 <b>−{pct}%</b> · {titulo}\n<b>{prec}</b>{orig} · {d['tienda']}\n<a href=\"{url}\">Ver oferta</a>\n")
    lineas.append(f"\n⚙️ Ajusta tus intereses en {SITE}  ·  /stop para dejar de recibirlos")
    return "\n".join(lineas)


def main() -> int:
    if not os.path.exists(DB_PATH):
        log(f"❌ No existe la base de datos: {DB_PATH}")
        return 1

    ahora  = datetime.now(timezone.utc)
    con    = db()

    usuarios = con.execute("""
        SELECT p.*, COALESCE(u.name, '') AS nombre
        FROM user_prefs p LEFT JOIN users u ON u.id = p.user_id
        WHERE p.tg_chat_id != '' AND COALESCE(p.tg_enabled, 1) = 1
    """).fetchall()

    if not usuarios:
        log("Sin usuarios vinculados a Telegram — nada que enviar.")
        return 0

    log(f"{len(usuarios)} usuario(s) con Telegram vinculado")
    enviados = 0

    for u in usuarios:
        # Desde el último envío, con un tope de ventana para no arrastrar historia
        desde = u["tg_last_sent"] or ""
        limite = (ahora - timedelta(hours=VENTANA_HORAS)).isoformat()
        if not desde or desde < limite:
            desde = limite

        candidatos = con.execute("""
            SELECT deal_id, titulo, tienda, precio, precio_original, descuento_pct,
                   COALESCE(categoria, '') AS categoria, publicado_en
            FROM deals_publicados
            WHERE COALESCE(expirado, 0) = 0 AND publicado_en > ?
            ORDER BY descuento_pct DESC, publicado_en DESC
            LIMIT 500
        """, (desde,)).fetchall()

        cats   = _json(u["cats"], [])
        stores = _json(u["stores"], [])
        pmin   = u["precio_min"] or 0
        pmax   = u["precio_max"]

        match = [d for d in candidatos if encaja(d, cats, stores, pmin, pmax)][:MAX_DEALS]
        if not match:
            log(f"  · usuario {u['user_id'][:8]}… sin coincidencias nuevas")
            continue

        if tg_send(u["tg_chat_id"], construir_mensaje(u["nombre"], match)):
            enviados += 1
            if not DRY_RUN:
                con.execute("UPDATE user_prefs SET tg_last_sent = ? WHERE user_id = ?",
                            (ahora.isoformat(), u["user_id"]))
                con.commit()
            log(f"  ✅ usuario {u['user_id'][:8]}… → {len(match)} deals")
        time.sleep(0.4)   # cortesía con el rate limit de Telegram

    log(f"Resumen diario enviado a {enviados}/{len(usuarios)} usuario(s)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
