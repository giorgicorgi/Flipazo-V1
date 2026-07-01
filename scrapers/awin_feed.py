"""
scrapers/awin_feed.py — Lector del product feed de AWIN (Create-a-Feed).

La URL completa del feed (con apikey + fids + columnas) se guarda en `.env` como
`AWIN_FEED_URL` — NUNCA en código (contiene la credencial apikey).

Comportamiento por tienda (según lo que trae el feed):
  - Padel Market      → tiene `product_price_old` (precio antes) fiable → se PUBLICA
                        como deal si el descuento ≥ mínimo. `aw_deep_link` ya es el
                        enlace de afiliado, así que se usa directamente.
  - ECI / Zalando /   → el feed NO trae "precio antes". Se REGISTRA su precio diario en
    Deporte Outlet /    price_history y se detectan bajadas ≥X% vs su propio máximo
    Brico Depot         histórico (scrapers/price_drop.py, genérico) → se publican con
                        descuento REAL verificado por nosotros, en cuanto hay ≥7 días de datos.

Caché 23h en memoria, igual que el feed de Tradedoubler.
"""

import csv
import gzip
import io
import os
import sqlite3
from datetime import datetime, timedelta

import collections

import requests
from dotenv import load_dotenv

from scrapers.price_drop import cargar_referencias, evaluar_bajada

load_dotenv()

AWIN_FEED_URL = os.getenv("AWIN_FEED_URL", "")

_CACHE_TTL_H = 23
_cache: list[dict] = []
_last_fetch: datetime | None = None

# merchant_name (tal cual viene en el feed) → nombre de tienda interno de Flipazo
_MERCHANT_MAP = {
    "Padel Market":         "Padel Market",
    "adidas ES":            "Adidas",            # product_price_old == precio (no es "antes") → histórico
    "El Corte Ingles ES":   "ElCorteIngles",
    "BRICO DEPÔT_ES":       "Brico Depot",
    "Privé by Zalando ES":  "Zalando",
    "Deporte Outlet ES":    "Deporte Outlet",
    "Paco Perfumerias ES":  "Paco Perfumerias",  # perfumería, sin "precio antes"
    "BIKILA ES":            "Bikila",            # running/trail, sin "precio antes"
}
# Tiendas con product_price_old fiable → se publican como deals
_PUBLICABLE = {"Padel Market"}
# Tiendas sin precio de referencia usable → solo histórico (registro diario de precio actual)
# para detectar bajadas ≥40% por histórico propio (los feeds no traen "precio antes" real;
# adidas trae product_price_old pero == precio actual, así que tampoco sirve).
_SOLO_HISTORICO = {"ElCorteIngles", "Brico Depot", "Zalando", "Deporte Outlet",
                   "Paco Perfumerias", "Bikila", "Adidas"}
# Suelo de precio para registrar histórico (evita inflar la BD: ECI son ~967k productos).
# A 100€ son ~246k obs/día; subir el suelo (env AWIN_HIST_PRECIO_MIN) reduce volumen.
_HIST_PRECIO_MIN = float(os.getenv("AWIN_HIST_PRECIO_MIN", "100"))
# Conservar histórico AWIN solo N días (acota el tamaño de price_history)
_HIST_DIAS = 45
# Máx. deals detectados por bajada por tienda y pasada (anti-flood en rebajas masivas)
_MAX_DETECT_POR_TIENDA = int(os.getenv("PRICE_DROP_MAX_POR_TIENDA", "40"))


def _to_float(s) -> float:
    try:
        return float(str(s).replace(",", ".").strip())
    except (ValueError, TypeError, AttributeError):
        return 0.0


def fetch_awin_productos(
    descuento_minimo: int = 40,
    precio_minimo: float = 25.0,
    precio_maximo: float = 9999.0,
    db_path: str | None = None,
    descuento_minimo_fn=None,
) -> list[dict]:
    """Descarga el feed AWIN, devuelve los deals publicables (Padel Market) y registra
    el histórico de precios de las tiendas sin precio de referencia (ECI/Brico).
    Caché 23h. Devuelve list[dict] compatible con el constructor de Producto."""
    global _cache, _last_fetch

    if not AWIN_FEED_URL:
        return []

    ahora = datetime.now()
    if _last_fetch and (ahora - _last_fetch) < timedelta(hours=_CACHE_TTL_H):
        print(f"   📦 AWIN caché activa: {len(_cache)} deals")
        return _cache

    try:
        print("   📡 AWIN feed (Create-a-Feed)...")
        r = requests.get(AWIN_FEED_URL, stream=True, timeout=180)
        if r.status_code != 200:
            print(f"   ❌ AWIN feed HTTP {r.status_code} — se mantiene caché previa ({len(_cache)})")
            return _cache
        r.raw.decode_content = False  # el cuerpo ES gzip (compression/gzip), no transfer-encoding
        gz = gzip.GzipFile(fileobj=r.raw)
        rdr = csv.DictReader(io.TextIOWrapper(gz, encoding="utf-8", errors="replace"))

        # Referencias de histórico (precio_max sostenido) por producto, para detectar
        # bajadas en las tiendas sin "precio antes" en el feed. 1 query antes de stremear.
        ref_index = cargar_referencias(db_path, sorted(_SOLO_HISTORICO)) if db_path else {}
        detect_cnt: collections.Counter = collections.Counter()

        publicables: list[dict] = []
        obs: list[tuple] = []  # (asin, tienda, precio, precio_ref, fecha) para price_history
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        n = 0
        for row in rdr:
            n += 1
            tienda = _MERCHANT_MAP.get((row.get("merchant_name") or "").strip())
            if not tienda:
                continue
            cur = _to_float(row.get("search_price"))
            if cur <= 0:
                continue

            # ── Tiendas solo-histórico (ECI/Zalando/Deporte/Brico) ───────────────
            # Registrar precio actual + detectar bajada ≥X% vs su propio máximo histórico.
            if tienda in _SOLO_HISTORICO:
                if cur >= _HIST_PRECIO_MIN:
                    pid = ((row.get("merchant_product_id") or row.get("aw_product_id") or "")).strip()[:60]
                    if pid:
                        obs.append((pid, tienda, cur, _to_float(row.get("product_price_old")), fecha_hoy))
                        # Detección de bajada por histórico propio (precio actual = feed de hoy)
                        if detect_cnt[tienda] < _MAX_DETECT_POR_TIENDA:
                            res = evaluar_bajada(ref_index.get((pid, tienda)), cur)
                            if res:
                                titulo = (row.get("product_name") or "").strip()
                                in_stock = (row.get("in_stock") or "").strip().lower() in ("1", "yes", "true", "y")
                                if titulo and in_stock:
                                    publicables.append({
                                        "titulo":          titulo,
                                        "asin":            (row.get("aw_deep_link") or "").strip(),
                                        "precio_actual":   cur,
                                        "precio_original": res[0],   # precio_max histórico
                                        "descuento_pct":   res[1],
                                        "tienda":          tienda,
                                        "imagen_url":      (row.get("merchant_image_url") or row.get("aw_image_url") or "").strip(),
                                    })
                                    detect_cnt[tienda] += 1
                continue

            # ── Tiendas publicables (Padel Market): requieren precio de referencia ──
            if tienda in _PUBLICABLE:
                ref = _to_float(row.get("product_price_old"))
                in_stock = (row.get("in_stock") or "").strip().lower() in ("1", "yes", "true", "y")
                if ref <= cur or not in_stock:
                    continue
                if not (precio_minimo <= cur <= precio_maximo):
                    continue
                desc = int((1 - cur / ref) * 100)
                titulo = (row.get("product_name") or "").strip()
                dmin = descuento_minimo_fn(titulo, cur) if descuento_minimo_fn else descuento_minimo
                if desc < dmin:
                    continue
                publicables.append({
                    "titulo":          titulo,
                    "asin":            (row.get("aw_deep_link") or "").strip(),  # enlace afiliado AWIN
                    "precio_actual":   cur,
                    "precio_original": ref,
                    "descuento_pct":   desc,
                    "tienda":          tienda,
                    "imagen_url":      (row.get("merchant_image_url") or row.get("aw_image_url") or "").strip(),
                })

        # ── Registrar histórico ECI/Brico + podar antiguo ──────────────────────
        if obs and db_path:
            try:
                with sqlite3.connect(db_path) as con:
                    con.executemany(
                        "INSERT OR IGNORE INTO price_history (asin, tienda, precio, precio_original, fecha) "
                        "VALUES (?, ?, ?, ?, ?)",
                        obs,
                    )
                    desde = (datetime.now() - timedelta(days=_HIST_DIAS)).strftime("%Y-%m-%d")
                    _ph = ",".join("?" * len(_SOLO_HISTORICO))
                    con.execute(
                        f"DELETE FROM price_history WHERE tienda IN ({_ph}) AND fecha < ?",
                        (*sorted(_SOLO_HISTORICO), desde),
                    )
                    con.commit()
                print(f"   📈 AWIN histórico ECI/Brico: {len(obs)} observaciones registradas")
            except Exception as e:
                print(f"   ⚠️  AWIN histórico error: {e}")

        _cache = publicables
        _last_fetch = ahora
        n_detect = sum(detect_cnt.values())
        detalle = f" (incl. {n_detect} por bajada histórica: {dict(detect_cnt)})" if n_detect else ""
        print(f"   ✅ AWIN: {len(publicables)} deals publicables{detalle} · {n} filas · {len(obs)} obs histórico")
        return publicables

    except Exception as e:
        print(f"   ❌ AWIN feed error: {e} — se mantiene caché previa ({len(_cache)})")
        return _cache
