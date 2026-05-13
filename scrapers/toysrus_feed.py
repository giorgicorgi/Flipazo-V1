"""
scrapers/toysrus_feed.py — Feed Tradedoubler ToysRus ES con historial de precios propio.

ToysRus (fid=21529) no incluye precio de referencia/tachado en el feed. Este módulo
construye su propio historial en SQLite: cada vez que se descarga el feed (1 vez/día,
caché 23h) guarda el precio de cada producto (EAN). Cuando un EAN acumula ≥ MIN_DIAS_DATOS
días de historial podemos detectar bajadas reales respecto al máximo de los últimos 30 días.

Feed: JSON paginado — products.json;page=N;pageSize=500;fid=21529?token=...
Clave de producto: EAN (identifiers.ean)
URL afiliado: productUrl ya incluye tracking TD con publisher 3481714

Tablas propias en la BD (no interfieren con deals_publicados):
  toysrus_precios   — (ean, fecha, precio)
  toysrus_productos — (ean, nombre, marca, imagen_url, url_afiliado, ultima_vez)
"""

import os
import sqlite3
import threading
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

TRADEDOUBLER_TOKEN = os.getenv("TRADEDOUBLER_TOKEN", "")
DB_PATH            = os.getenv("DB_PATH", "flipazo_deals.db")

_FID            = "21529"
_API_BASE       = "https://api.tradedoubler.com/1.0/products.json"
_PAGE_SIZE      = 500

_DESCUENTO_MIN    = 40
_PRECIO_MIN       = 25.0
_PRECIO_MAX       = 800.0
_PRECIO_TRACK_MAX = 1500.0   # más amplio para registrar historial de juguetes caros
_DIAS_HISTORIAL   = 30
_MIN_DIAS_DATOS   = 7        # días distintos requeridos para publicar deals

_lock:       threading.Lock = threading.Lock()
_last_fetch: datetime | None = None
_cache:      list[dict] = []


# ── Helpers ────────────────────────────────────────────────────────────────

def _parse_precio(valor) -> float:
    if valor is None:
        return 0.0
    try:
        return float(str(valor).replace(",", ".").strip())
    except Exception:
        return 0.0


# ── SQLite: tablas propias de ToysRus ─────────────────────────────────────

def _init_tablas(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS toysrus_precios (
            ean    TEXT NOT NULL,
            fecha  TEXT NOT NULL,
            precio REAL NOT NULL,
            PRIMARY KEY (ean, fecha)
        );
        CREATE TABLE IF NOT EXISTS toysrus_productos (
            ean          TEXT PRIMARY KEY,
            nombre       TEXT,
            marca        TEXT,
            imagen_url   TEXT,
            url_afiliado TEXT,
            ultima_vez   TEXT
        );
    """)
    con.commit()


# ── Descarga paginada ──────────────────────────────────────────────────────

def _fetch_all_products() -> list[dict]:
    """Pagina el feed completo de ToysRus en bloques de _PAGE_SIZE."""
    if not TRADEDOUBLER_TOKEN:
        return []
    all_items: list[dict] = []
    page = 1
    while True:
        url = f"{_API_BASE};page={page};pageSize={_PAGE_SIZE};fid={_FID}?token={TRADEDOUBLER_TOKEN}"
        try:
            r = requests.get(url, timeout=90)
            r.raise_for_status()
            batch = r.json().get("products", [])
        except Exception as e:
            print(f"   ⚠️  ToysRus feed pág {page}: {e}")
            break
        if not batch:
            break
        all_items.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        page += 1
    return all_items


# ── Parse y normalización ─────────────────────────────────────────────────

def _parsear_feed(raw: list[dict]) -> list[dict]:
    """
    Devuelve UN dict por EAN (in-stock, precio en rango de tracking).
    Ignora out-of-stock para no registrar precios fantasma.
    """
    result:    list[dict] = []
    seen_eans: set[str]   = set()

    for item in raw:
        try:
            titulo = (item.get("name") or "").strip()
            if not titulo or len(titulo) < 8:
                continue

            offers = item.get("offers") or []
            if not offers:
                continue
            offer = offers[0]

            av = (offer.get("availability") or "").lower()
            if av not in ("in stock", "available", "en stock"):
                continue

            ph     = offer.get("priceHistory") or []
            precio = _parse_precio((ph[0].get("price") or {}).get("value") if ph else None)
            if precio <= 0 or precio > _PRECIO_TRACK_MAX:
                continue

            ean = ((item.get("identifiers") or {}).get("ean") or "").strip()
            if not ean:
                ean = titulo[:60].lower()   # fallback si no hay EAN

            if ean in seen_eans:
                continue
            seen_eans.add(ean)

            imagen = ((item.get("productImage") or {}).get("url") or "").strip()

            result.append({
                "ean":       ean,
                "nombre":    titulo,
                "marca":     (item.get("brand") or "").strip(),
                "precio":    precio,
                "imagen_url": imagen,
                "url":       offer.get("productUrl", ""),
            })
        except Exception:
            continue

    return result


# ── Persistencia ──────────────────────────────────────────────────────────

def _registrar_precios(productos: list[dict]) -> None:
    """Guarda precio de HOY para cada EAN. Un registro por día (INSERT OR REPLACE)."""
    hoy     = datetime.utcnow().strftime("%Y-%m-%d")
    now_iso = datetime.utcnow().isoformat()

    with sqlite3.connect(DB_PATH) as con:
        _init_tablas(con)
        for p in productos:
            con.execute(
                "INSERT OR REPLACE INTO toysrus_precios (ean, fecha, precio) VALUES (?,?,?)",
                (p["ean"], hoy, p["precio"])
            )
            con.execute("""
                INSERT INTO toysrus_productos
                    (ean, nombre, marca, imagen_url, url_afiliado, ultima_vez)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(ean) DO UPDATE SET
                    nombre       = excluded.nombre,
                    imagen_url   = excluded.imagen_url,
                    url_afiliado = excluded.url_afiliado,
                    ultima_vez   = excluded.ultima_vez
            """, (p["ean"], p["nombre"], p["marca"],
                  p["imagen_url"], p["url"], now_iso))
        con.commit()


# ── Cálculo de deals ──────────────────────────────────────────────────────

def _calcular_deals(productos: list[dict]) -> list[dict]:
    """
    Para cada EAN con ≥ MIN_DIAS_DATOS días de historial (excluyendo hoy),
    compara el precio actual con el máximo histórico de los últimos 30 días.
    """
    hoy      = datetime.utcnow().strftime("%Y-%m-%d")
    hace_30d = (datetime.utcnow() - timedelta(days=_DIAS_HISTORIAL)).strftime("%Y-%m-%d")

    prod_map = {p["ean"]: p for p in productos}

    with sqlite3.connect(DB_PATH) as con:
        _init_tablas(con)
        rows = con.execute("""
            SELECT ean,
                   MAX(precio)  AS precio_max,
                   COUNT(fecha) AS n_dias
            FROM   toysrus_precios
            WHERE  fecha >= ? AND fecha < ?
            GROUP  BY ean
            HAVING COUNT(fecha) >= ?
        """, (hace_30d, hoy, _MIN_DIAS_DATOS)).fetchall()

    deals: list[dict] = []
    for (ean, precio_max, _n_dias) in rows:
        prod = prod_map.get(ean)
        if not prod:
            continue  # producto ya no in-stock hoy

        precio_hoy = prod["precio"]
        if precio_hoy <= 0:
            continue
        if not (_PRECIO_MIN <= precio_hoy <= _PRECIO_MAX):
            continue
        if precio_max <= precio_hoy:
            continue   # sin bajada

        descuento_pct = int((1 - precio_hoy / precio_max) * 100)
        if descuento_pct < _DESCUENTO_MIN:
            continue

        deals.append({
            "titulo":          prod["nombre"],
            "asin":            prod["url"],
            "precio_actual":   precio_hoy,
            "precio_original": round(precio_max, 2),
            "descuento_pct":   descuento_pct,
            "tienda":          "ToysRus",
            "imagen_url":      prod["imagen_url"],
        })

    return deals


# ── Punto de entrada público ───────────────────────────────────────────────

def fetch_toysrus_productos() -> list[dict]:
    """
    Descarga el feed de ToysRus (caché 23h), registra precios en BD
    y devuelve deals detectados como list[dict] compatible con Producto(**d).

    Los primeros MIN_DIAS_DATOS días devuelve [] (acumulando historial).
    """
    global _last_fetch, _cache

    if not TRADEDOUBLER_TOKEN:
        print("⚠️  ToysRus feed: TRADEDOUBLER_TOKEN no configurado — saltando")
        return []

    with _lock:
        now = datetime.utcnow()
        if _last_fetch and (now - _last_fetch).total_seconds() < 23 * 3600:
            return _cache

        try:
            raw      = _fetch_all_products()
            prods    = _parsear_feed(raw)
            _registrar_precios(prods)
            deals    = _calcular_deals(prods)

            _cache      = deals
            _last_fetch = now
            print(
                f"📡 ToysRus feed: {len(prods)} productos registrados, "
                f"{len(deals)} deals detectados"
            )
            return deals

        except Exception as e:
            print(f"❌ ToysRus feed error: {e}")
            return _cache   # caché anterior si falla
