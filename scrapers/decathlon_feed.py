"""
scrapers/decathlon_feed.py — Feed de afiliado Decathlon ES (feed id=107).

Esquema XML:  <items><item>...</item>...</items>
Campos:       ModelID, SkuID, Name, Brand, Sport, Img, URL (ya es deep link
              afiliado), Price, Size, Seller.

⚠️ POR QUÉ id=107 Y NO id=98 (10-ago-2026):
El 14-jun se cambió a id=98 porque traía InitialPrice y prometía "descuento
directo". Fue un error doble:

  1. Sus precios se CONGELARON ese mismo día. Comprobado sobre 85 días de
     histórico propio: hasta el 14-jun había cientos o miles de cambios diarios;
     desde entonces, CERO cambios en 158.927 modelos durante 57 días. Como la
     detección compara contra los últimos 30 días, los deals se agotaron el
     10-jul y la tienda quedó muda sin que nada fallara aparentemente.
  2. InitialPrice no es un precio anterior real. En los recambios es el precio
     del producto PADRE: la vela del velero Tribord salió a 399,99€ "antes
     2.469,99€" (el velero completo), llevando 85 días a 399,99€.

id=107 trae precios FRESCOS (de 20.033 modelos comunes, 5.805 tienen precio
distinto del congelado de id=98) y solo catálogo propio: Seller="Decathlon" en
los 79.119 items, sin marketplace de terceros.

No trae precio de referencia, y está bien: la única referencia fiable es NUESTRO
histórico (decathlon_precios) — referencia = precio máximo sostenido ≥3 días en
los últimos 30; deal solo si el de hoy cae ≥40% respecto a esa referencia real.

Se descarga en streaming a un fichero temporal (~85 MB) y se parsea con
ET.iterparse (memoria acotada), liberando cada nodo al procesarlo.

Caché 23h. Devuelve list[dict] compatible con Producto(**d) en flipazo_main.

Sigue registrando historial propio (decathlon_precios / decathlon_productos) para
auditoría y posibles validaciones futuras.
"""

import os
import re
import sqlite3
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from dotenv import load_dotenv

from datetime import timedelta

load_dotenv()

DECATHLON_FEED_URL = os.getenv("DECATHLON_FEED_URL", "")
DB_PATH            = os.getenv("DB_PATH", "flipazo_deals.db")

_DESCUENTO_MIN = 40     # % mínimo — mismo que el pipeline general
_DESCUENTO_CAP = 85     # % máximo de una bajada real (por encima = error de dato)
_PRECIO_MIN    = 25.0   # € mínimo para deals
_PRECIO_MAX    = 800.0  # € máximo para deals
# Detección por histórico propio (decathlon_precios), no por el PVP del feed:
_DIAS_HISTORIAL  = 30   # ventana de histórico considerada
_MIN_DIAS_DATOS  = 7    # días distintos de datos exigidos (fiabilidad)
_MIN_DIAS_EN_MAX = 3    # el precio de referencia debe haberse sostenido ≥N días

# ── Marcas PROPIAS de Decathlon ────────────────────────────────────────────
# ⚠️ NO usar InitialPrice como referencia, ni siquiera en estas marcas. Se probó
# el 10-ago-2026 y salió mal: de 11 deals publicados, en 9 NUNCA habíamos
# observado ese precio anterior. Casos reales:
#   · "Vela Velero Hinchable Tribord 5S V2": 399,99€ "antes 2.469,99€". Los
#     2.469€ son el precio del VELERO COMPLETO — el feed hereda el precio del
#     producto padre a cada recambio (rueda 9,99€, orza 77,99€, mástil 369,99€…).
#     Ese modelo lleva 85 días a 399,99€ y nunca estuvo a otro precio.
#   · "Chaqueta Offshore 900": publicada a 119,99€ "antes 249,99€" cuando en
#     nuestro histórico llevaba 55 días a 49,99€ — anunciábamos como oferta un
#     precio MÁS CARO del que habíamos visto.
# InitialPrice es un precio de catálogo que no podemos verificar. La única
# referencia fiable es nuestro propio histórico (decathlon_precios).
#
# El conjunto se conserva porque distingue el catálogo propio del marketplace
# (86% del feed son terceros), útil para futuras decisiones.
_MARCAS_PROPIAS = {
    "quechua", "kalenji", "domyos", "kipsta", "nabaiji", "wedze", "forclaz",
    "tribord", "artengo", "btwin", "rockrider", "van rysel", "solognac",
    "caperlan", "geologic", "simond", "olaian", "newfeel", "inesis", "fouganza",
    "aptonia", "kiprun", "decathlon", "corique", "offload", "allsix", "perfly",
    "outshock", "copaya", "evadict", "wanabee", "subea", "itiwit", "orao",
}

def _es_marca_propia(marca: str) -> bool:
    m = (marca or "").lower().strip()
    return any(m == p or m.startswith(p) for p in _MARCAS_PROPIAS)

# Descarta variantes cuya única diferencia sea una talla de letra (S/M/L/XL…),
# para preferir una URL/representación de talla numérica o única por modelo.
_TALLA_LETRA_RE = re.compile(r'^\s*(?:XXL|XXXL|XXS|XS|XL|[SML])\s*(?:/|$)', re.IGNORECASE)

_lock       = threading.Lock()
_last_fetch: datetime | None = None
_cache:      list[dict] = []


# ── Helpers ────────────────────────────────────────────────────────────────

def _parse_precio(s) -> float:
    try:
        return float(str(s or "0").replace(",", ".").strip())
    except Exception:
        return 0.0


def _parse_int(s) -> int:
    try:
        return int(float(str(s or "0").replace(",", ".").strip()))
    except Exception:
        return 0


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


def _registrar_precios(modelos: dict[str, dict]) -> None:
    """Guarda el precio actual de HOY para cada modelo (un registro por día)."""
    hoy     = datetime.utcnow().strftime("%Y-%m-%d")
    now_iso = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        _init_tablas(con)
        for m in modelos.values():
            con.execute(
                "INSERT OR REPLACE INTO decathlon_precios (model_id, fecha, precio) VALUES (?,?,?)",
                (m["model_id"], hoy, m["precio_actual"]),
            )
            con.execute("""
                INSERT INTO decathlon_productos
                    (model_id, nombre, marca, deporte, imagen_url, url_afiliado, ultima_vez)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(model_id) DO UPDATE SET
                    nombre       = excluded.nombre,
                    marca        = excluded.marca,
                    deporte      = excluded.deporte,
                    imagen_url   = excluded.imagen_url,
                    url_afiliado = excluded.url_afiliado,
                    ultima_vez   = excluded.ultima_vez
            """, (m["model_id"], m["nombre"], m["marca"], m["deporte"],
                  m["imagen_url"], m["url"], now_iso))
        con.commit()


# ── Descarga + parseo ──────────────────────────────────────────────────────

def _descargar_a_fichero() -> str:
    """Descarga el feed en streaming a un fichero temporal. Devuelve la ruta.

    Valida que lo descargado sea XML: si se pide el feed varias veces seguidas, el
    proveedor responde 200 con el cuerpo "File not found" (15 bytes). Sin este
    control se parseaba esa cadena y saltaba un 'syntax error: line 1, column 0'
    que parecía un feed corrupto, cuando en realidad no había feed."""
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="decathlon_feed_")
    with os.fdopen(fd, "wb") as f:
        with requests.get(DECATHLON_FEED_URL, timeout=180, stream=True) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)

    with open(path, "rb") as f:
        cabecera = f.read(200).lstrip()
    if not cabecera.startswith(b"<?xml") and not cabecera.startswith(b"<items"):
        detalle = cabecera[:80].decode("utf-8", "replace").strip()
        os.unlink(path)
        raise RuntimeError(f"el proveedor no devolvió XML ({os.path.getsize(path) if os.path.exists(path) else 0} b): {detalle!r}")
    return path


def _parsear(path: str) -> tuple[dict[str, dict], int]:
    """
    iterparse del XML grande. Agrupa por Product_ID (colapsa tallas en un modelo).
    Devuelve (modelos, total_productos). Memoria acotada: limpia cada nodo y la raíz.
    """
    modelos: dict[str, dict] = {}
    total = 0

    def _procesar(el) -> None:
        def _t(tag: str) -> str:
            v = el.findtext(tag)
            return v.strip() if v else ""
        mid = _t("ModelID")
        if not mid:
            return
        precio = _parse_precio(_t("Price"))
        if precio <= 0:
            return
        es_letra = bool(_TALLA_LETRA_RE.match(_t("Size")))
        if mid not in modelos:
            modelos[mid] = {
                "model_id":      mid,
                "nombre":        _t("Name"),
                "marca":         _t("Brand") or "DECATHLON",
                "deporte":       _t("Sport"),
                "precio_actual": precio,
                "precio_ref":    0.0,     # este feed NO trae precio de referencia: se usa el histórico
                "imagen_url":    _t("Img"),
                "url":           _t("URL"),
                "disp":          1,       # id=107 solo lista producto disponible
                "_letra":        es_letra,
            }
        else:
            m = modelos[mid]
            # Preferir variante de talla no-letra (mejor URL/representación)
            if m["_letra"] and not es_letra:
                m.update({
                    "precio_actual": precio,
                    "url":           _t("URL") or m["url"],
                    "_letra":        False,
                })

    context = ET.iterparse(path, events=("start", "end"))
    _, root = next(context)  # primer start = <items>
    try:
        for event, el in context:
            if event == "end" and el.tag == "item":
                total += 1
                _procesar(el)
                el.clear()
                root.clear()
    except ET.ParseError as e:
        # Feed truncado / descarga incompleta: conservamos lo parseado hasta el corte
        print(f"   ⚠️  Decathlon feed truncado tras {total} productos: {e}")

    return modelos, total


def _cargar_referencias_historico() -> dict[str, float]:
    """Referencia REAL por modelo desde decathlon_precios: el precio máximo que ha estado
    vigente de forma SOSTENIDA (≥_MIN_DIAS_EN_MAX días distintos) en los últimos
    _DIAS_HISTORIAL días, excluyendo hoy. Solo modelos con ≥_MIN_DIAS_DATOS días de datos
    (robusto frente a precios puntuales/erróneos). NO usa el InitialPrice (PVP) del feed."""
    hoy      = datetime.utcnow().strftime("%Y-%m-%d")
    hace_30d = (datetime.utcnow() - timedelta(days=_DIAS_HISTORIAL)).strftime("%Y-%m-%d")
    ref: dict[str, float] = {}
    try:
        with sqlite3.connect(DB_PATH) as con:
            _init_tablas(con)
            rows = con.execute("""
                WITH base AS (
                    SELECT model_id, MAX(precio) AS pmax, COUNT(fecha) AS n_dias
                    FROM   decathlon_precios
                    WHERE  fecha >= ? AND fecha < ? AND precio > 0
                    GROUP  BY model_id
                    HAVING COUNT(fecha) >= ?
                )
                SELECT b.model_id, b.pmax
                FROM   base b
                JOIN   decathlon_precios p
                       ON  p.model_id = b.model_id
                       AND p.fecha >= ? AND p.fecha < ?
                       AND p.precio >= b.pmax * 0.98
                GROUP  BY b.model_id
                HAVING COUNT(p.fecha) >= ?
            """, (hace_30d, hoy, _MIN_DIAS_DATOS,
                  hace_30d, hoy, _MIN_DIAS_EN_MAX)).fetchall()
        ref = {mid: pmax for mid, pmax in rows}
    except Exception as e:
        print(f"   ⚠️  Decathlon referencias histórico: {e}")
    return ref


def _detectar_deals(modelos: dict[str, dict]) -> list[dict]:
    """Bajada REAL: precio de hoy vs máximo sostenido de NUESTRO histórico propio
    (no el PVP del feed, que es RRP inflado). Solo descuentos verificados por nosotros."""
    ref = _cargar_referencias_historico()
    deals = []
    for m in modelos.values():
        pa   = m["precio_actual"]
        pmax = ref.get(m["model_id"])
        if not pmax or pmax <= pa:
            continue                                   # sin bajada real vs nuestro histórico
        if not (_PRECIO_MIN <= pa <= _PRECIO_MAX):
            continue
        if m["disp"] <= 0:
            continue                                   # sin stock
        descuento_pct = int((1 - pa / pmax) * 100)
        if not (_DESCUENTO_MIN <= descuento_pct <= _DESCUENTO_CAP):
            continue
        deals.append({
            "titulo":          m["nombre"],
            "asin":            m["url"],                # deep link afiliado ya incluido
            "precio_actual":   pa,
            "precio_original": round(pmax, 2),         # precio real anterior (histórico propio)
            "descuento_pct":   descuento_pct,
            "tienda":          "Decathlon",
            "imagen_url":      m["imagen_url"] or "",
        })
    return deals


# ── Punto de entrada público ───────────────────────────────────────────────

def fetch_decathlon_productos() -> list[dict]:
    """
    Descarga el feed Decathlon (caché 23h), registra historial y devuelve los deals
    detectados como list[dict] compatible con Producto(**d).
    """
    global _last_fetch, _cache

    if not DECATHLON_FEED_URL:
        print("⚠️  Decathlon feed: DECATHLON_FEED_URL no configurado — saltando")
        return []

    with _lock:
        now = datetime.utcnow()
        if _last_fetch and (now - _last_fetch).total_seconds() < 23 * 3600:
            return _cache

        path = None
        try:
            path = _descargar_a_fichero()
            modelos, total = _parsear(path)
            _registrar_precios(modelos)
            deals = _detectar_deals(modelos)

            _cache      = deals
            _last_fetch = now
            print(
                f"📡 Decathlon feed (id=107): {total} productos, "
                f"{len(modelos)} modelos, {len(deals)} deals detectados"
            )
            return deals

        except Exception as e:
            print(f"❌ Decathlon feed error: {e}")
            return _cache  # caché anterior si falla

        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
