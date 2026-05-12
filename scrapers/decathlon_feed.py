"""
scrapers/decathlon_feed.py — Feed de productos Decathlon ES con historial propio.

El feed XML de Decathlon no incluye precio de referencia/tachado. Este módulo
construye su propio historial de precios en SQLite: cada vez que se descarga
el feed (1 vez/día, caché 23h), guarda el precio de cada modelo. Cuando un
modelo acumula >= MIN_DIAS_DATOS días de historial, podemos detectar bajadas
reales respecto al máximo de los últimos 30 días.

Feed URL:  os.getenv("DECATHLON_FEED_URL")
Formato:   XML  <items><item>...</item></items>
Campos:    ModelID, SkuID, Name, Price, Img, URL (ya es deep link afiliado),
           Brand, Sport, Size

Descarga 1 vez/día (caché 23h).
Devuelve list[dict] con campos de Producto para que flipazo_main convierta con
Producto(**d) y filtre con _es_producto_valido / _precio_aceptable.

Tablas propias en la BD (no interfieren con deals_publicados):
  decathlon_precios   — historial de precios: (model_id, fecha, precio)
  decathlon_productos — metadatos del modelo: nombre, url_afiliado, imagen, etc.
"""

import os
import re
import sqlite3
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

DECATHLON_FEED_URL = os.getenv("DECATHLON_FEED_URL", "")
DB_PATH            = os.getenv("DB_PATH", "flipazo_deals.db")

_DESCUENTO_MIN  = 40    # % mínimo — mismo que el pipeline general
_PRECIO_MIN     = 25.0  # € mínimo para deals
_PRECIO_MAX     = 800.0 # € máximo para deals
_PRECIO_TRACK_MAX = 1500.0  # máximo para registrar historial (más amplio)
_DIAS_HISTORIAL = 30    # días hacia atrás para calcular precio de referencia
_MIN_DIAS_DATOS = 7     # días distintos requeridos antes de publicar deals

# Regex para descartar variantes cuya única diferencia sea una talla de letra.
# Ejemplos que captura: "S / W30 L31", "M", "XL", "XXL / 40"
# Ejemplos que NO captura: "60 cm", "38", "Talla única", "89-95cm 2-3A"
_TALLA_LETRA_RE = re.compile(
    r'^\s*(?:XXL|XXXL|XXS|XS|XL|[SML])\s*(?:/|$)',
    re.IGNORECASE
)

_lock       = threading.Lock()
_last_fetch: datetime | None = None
_cache:      list[dict] = []


# ── Helpers ────────────────────────────────────────────────────────────────

def _parse_precio(s) -> float:
    try:
        return float(str(s or "0").replace(",", ".").strip())
    except Exception:
        return 0.0


def _tiene_talla_letra(size_str: str) -> bool:
    return bool(_TALLA_LETRA_RE.match(size_str or ""))


# ── SQLite: tablas propias de Decathlon ────────────────────────────────────

def _init_tablas(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS decathlon_precios (
            model_id  TEXT NOT NULL,
            fecha     TEXT NOT NULL,
            precio    REAL NOT NULL,
            PRIMARY KEY (model_id, fecha)
        );
        CREATE TABLE IF NOT EXISTS decathlon_productos (
            model_id     TEXT PRIMARY KEY,
            nombre       TEXT,
            marca        TEXT,
            deporte      TEXT,
            imagen_url   TEXT,
            url_afiliado TEXT,
            ultima_vez   TEXT
        );
    """)
    con.commit()


def _registrar_precios(productos: list[dict]) -> None:
    """Guarda precio de HOY para cada modelo. Un registro por día (INSERT OR REPLACE)."""
    hoy     = datetime.utcnow().strftime("%Y-%m-%d")
    now_iso = datetime.utcnow().isoformat()

    with sqlite3.connect(DB_PATH) as con:
        _init_tablas(con)
        for p in productos:
            mid = p["model_id"]
            con.execute(
                "INSERT OR REPLACE INTO decathlon_precios (model_id, fecha, precio) VALUES (?,?,?)",
                (mid, hoy, p["precio"])
            )
            con.execute("""
                INSERT INTO decathlon_productos
                    (model_id, nombre, marca, deporte, imagen_url, url_afiliado, ultima_vez)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(model_id) DO UPDATE SET
                    nombre       = excluded.nombre,
                    imagen_url   = excluded.imagen_url,
                    url_afiliado = excluded.url_afiliado,
                    ultima_vez   = excluded.ultima_vez
            """, (mid, p["nombre"], p["marca"], p["deporte"],
                  p["imagen_url"], p["url"], now_iso))
        con.commit()


def _calcular_deals(productos: list[dict]) -> list[dict]:
    """
    Para cada modelo con >= MIN_DIAS_DATOS días de historial (excluyendo hoy),
    compara el precio actual con el máximo histórico para detectar bajadas reales.
    """
    hoy      = datetime.utcnow().strftime("%Y-%m-%d")
    hace_30d = (datetime.utcnow() - timedelta(days=_DIAS_HISTORIAL)).strftime("%Y-%m-%d")

    # Lookup rápido: model_id → producto completo (precio de hoy + metadatos)
    prod_map = {p["model_id"]: p for p in productos}

    with sqlite3.connect(DB_PATH) as con:
        _init_tablas(con)
        # Máximo y conteo de días ANTES de hoy (excluyendo el registro de hoy para
        # que el precio de referencia sea siempre histórico, no el de la bajada)
        rows = con.execute("""
            SELECT model_id,
                   MAX(precio)  AS precio_max,
                   COUNT(fecha) AS n_dias
            FROM   decathlon_precios
            WHERE  fecha >= ? AND fecha < ?
            GROUP  BY model_id
            HAVING COUNT(fecha) >= ?
        """, (hace_30d, hoy, _MIN_DIAS_DATOS)).fetchall()

    deals = []
    for (mid, precio_max, n_dias) in rows:
        prod = prod_map.get(mid)
        if not prod:
            continue

        precio_hoy = prod["precio"]
        if precio_hoy <= 0:
            continue
        if not (_PRECIO_MIN <= precio_hoy <= _PRECIO_MAX):
            continue
        if precio_max <= precio_hoy:
            continue  # no hay bajada

        descuento_pct = int((1 - precio_hoy / precio_max) * 100)
        if descuento_pct < _DESCUENTO_MIN:
            continue

        deals.append({
            "titulo":          prod["nombre"],
            "asin":            prod["url"],   # URL con tracking afiliado ya incluido
            "precio_actual":   precio_hoy,
            "precio_original": round(precio_max, 2),
            "descuento_pct":   descuento_pct,
            "tienda":          "Decathlon",
            "imagen_url":      prod["imagen_url"] or "",
        })

    return deals


# ── Parse y agrupación ─────────────────────────────────────────────────────

def _parsear_feed(xml_text: str) -> list[dict]:
    """
    Parsea el XML y devuelve UN dict por ModelID (no por SKU/talla).
    Para cada modelo, prioriza SKUs con talla no-letra (ej. numérica o "Talla única")
    sobre variantes de letra (S, M, L, XL…) para obtener mejor URL de referencia.
    Filtra precios fuera de rango de tracking.
    """
    root   = ET.fromstring(xml_text)
    models: dict[str, dict] = {}

    for item in root.findall("item"):
        mid = (item.findtext("ModelID") or "").strip()
        if not mid:
            continue

        precio = _parse_precio(item.findtext("Price") or "0")
        if precio <= 0 or precio > _PRECIO_TRACK_MAX:
            continue

        size            = (item.findtext("Size") or "").strip()
        es_talla_letra  = _tiene_talla_letra(size)

        if mid not in models:
            models[mid] = {
                "model_id":        mid,
                "nombre":          (item.findtext("Name") or "").strip(),
                "precio":          precio,
                "marca":           (item.findtext("Brand") or "DECATHLON").strip(),
                "deporte":         (item.findtext("Sport") or "").strip(),
                "imagen_url":      (item.findtext("Img") or "").strip(),
                "url":             (item.findtext("URL") or "").strip(),
                "_talla_solo_letra": es_talla_letra,
            }
        elif models[mid]["_talla_solo_letra"] and not es_talla_letra:
            # Reemplazar la entrada con una variante de talla numérica/única (mejor URL)
            m = models[mid]
            m["url"]               = (item.findtext("URL") or m["url"]).strip()
            m["_talla_solo_letra"] = False

    # Limpiar campo interno antes de devolver
    for m in models.values():
        m.pop("_talla_solo_letra", None)

    return list(models.values())


# ── Punto de entrada público ───────────────────────────────────────────────

def fetch_decathlon_productos() -> list[dict]:
    """
    Descarga el feed XML de Decathlon (caché 23h), registra precios en BD
    y devuelve deals detectados como list[dict] compatible con Producto(**d).

    Los primeros MIN_DIAS_DATOS días devuelve [] (acumulando historial).
    A partir de entonces devuelve deals reales con descuento calculado
    contra el máximo de los últimos 30 días.
    """
    global _last_fetch, _cache

    if not DECATHLON_FEED_URL:
        print("⚠️  Decathlon feed: DECATHLON_FEED_URL no configurado — saltando")
        return []

    with _lock:
        now = datetime.utcnow()
        if _last_fetch and (now - _last_fetch).total_seconds() < 23 * 3600:
            return _cache

        try:
            resp = requests.get(DECATHLON_FEED_URL, timeout=90)
            resp.raise_for_status()

            productos = _parsear_feed(resp.text)
            _registrar_precios(productos)
            deals     = _calcular_deals(productos)

            _cache      = deals
            _last_fetch = now
            print(
                f"📡 Decathlon feed: {len(productos)} modelos registrados, "
                f"{len(deals)} deals detectados"
            )
            return deals

        except Exception as e:
            print(f"❌ Decathlon feed error: {e}")
            return _cache  # caché anterior si falla
