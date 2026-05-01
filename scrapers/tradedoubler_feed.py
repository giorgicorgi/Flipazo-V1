"""
scrapers/tradedoubler_feed.py — Feeds de producto Tradedoubler.

Tiendas activas:
  MediaMarkt ES  fid=24915  strike_price = MSRP/precio ref. regulado
  PCBox ES       fid=50247  PreviousPrice = precio ref. regulado (monitores, cajas, componentes)

Tiendas desactivadas:
  Beep ES        fid=51903  PreviousPrice = MSRP fabricante, no precio 30d → falsos descuentos
  ToysRus ES     fid=21529  sin campo precio original → descuento incalculable

Descarga una vez al día (caché 23h). Devuelve list[dict] con los campos de Producto
listos para que flipazo_main los convierta y filtre con _es_producto_valido.
"""

import os
import re
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

TRADEDOUBLER_TOKEN = os.getenv("TRADEDOUBLER_TOKEN", "")

_FEEDS = [
    {"tienda": "MediaMarkt", "fid": "24915"},
    {"tienda": "PCBox",      "fid": "50247"},
    # Beep: PreviousPrice es MSRP fabricante, no precio 30d → falsos descuentos sistemáticos.
    # {"tienda": "Beep", "fid": "51903"},
    # ToysRus: feed sin precio original → descuento incalculable.
    # {"tienda": "ToysRus", "fid": "21529"},
]

_API_BASE    = "https://api.tradedoubler.com/1.0/productsUnlimited.json"
_CACHE_TTL_H = 23

_cache: list[dict] = []
_last_fetch: datetime | None = None


def _parse_precio(valor) -> float:
    if valor is None:
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    s = re.sub(r"[^\d.,]", "", str(valor)).strip()
    if not s:
        return 0.0
    # Formato europeo: 1.234,56 → 1234.56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _get_field(fields, nombre: str) -> str:
    """Extrae un campo de fields tanto si es dict como si es list[{name, value}]."""
    if isinstance(fields, dict):
        return str(fields.get(nombre) or "")
    if isinstance(fields, list):
        for f in fields:
            if isinstance(f, dict) and f.get("name") == nombre:
                return str(f.get("value") or "")
    return ""


def _fetch_unlimited(fid: str) -> list[dict]:
    """Descarga el feed completo en una sola petición (productsUnlimited)."""
    url = f"{_API_BASE};fid={fid}?token={TRADEDOUBLER_TOKEN}"
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        data = r.json()
        return data.get("products", [])
    except Exception as e:
        print(f"   ❌ TD fid={fid}: {e}")
        return []


def _filtrar(
    raw: list[dict],
    tienda: str,
    descuento_minimo: int,
    precio_minimo: float,
    precio_maximo: float,
) -> list[dict]:
    """
    Aplica filtros básicos (precio, descuento, stock) y devuelve dicts con los
    campos exactos que necesita el constructor de Producto en flipazo_main.
    _es_producto_valido se aplica después en flipazo_main para evitar import circular.
    """
    resultado: list[dict] = []
    vistos: set[str] = set()

    for item in raw:
        try:
            titulo = (item.get("name") or "").strip()
            if not titulo or len(titulo) < 8:
                continue

            offers = item.get("offers") or []
            if not offers:
                continue
            offer = offers[0]

            price_history = offer.get("priceHistory") or []
            precio_actual = _parse_precio(
                (price_history[0].get("price") or {}).get("value") if price_history else None
            )
            if not (precio_minimo <= precio_actual <= precio_maximo):
                continue

            fields_raw = item.get("fields", {})
            strike_raw = _get_field(fields_raw, "strike_price") or _get_field(fields_raw, "PreviousPrice")
            precio_original = _parse_precio(strike_raw)
            if precio_original <= precio_actual:
                continue

            descuento_pct = int((1 - precio_actual / precio_original) * 100)
            if descuento_pct < descuento_minimo:
                continue

            disponibilidad = (offer.get("availability") or "").lower()
            if disponibilidad not in ("in stock", "available", "en stock"):
                continue

            ean = ((item.get("identifiers") or {}).get("ean") or "")
            clave = ean if ean else titulo[:50].lower()
            if clave in vistos:
                continue
            vistos.add(clave)

            resultado.append({
                "titulo":          titulo,
                "asin":            offer.get("productUrl", ""),
                "precio_actual":   precio_actual,
                "precio_original": precio_original,
                "descuento_pct":   descuento_pct,
                "tienda":          tienda,
                "imagen_url":      ((item.get("productImage") or {}).get("url") or ""),
            })
        except Exception:
            continue

    return resultado


def fetch_tradedoubler_productos(
    descuento_minimo: int = 37,
    precio_minimo: float = 25.0,
    precio_maximo: float = 800.0,
) -> list[dict]:
    """
    Descarga y filtra los feeds de MediaMarkt y PCBox de Tradedoubler.
    Usa caché de 23h para no re-descargar en cada ciclo completo del pipeline.
    Retorna list[dict] con campos compatibles con el constructor de Producto.
    """
    global _cache, _last_fetch

    if not TRADEDOUBLER_TOKEN:
        print("   ⚠️ TRADEDOUBLER_TOKEN no configurado — skip feeds TD")
        return []

    ahora = datetime.now()
    if _last_fetch and (ahora - _last_fetch) < timedelta(hours=_CACHE_TTL_H):
        h_rest = _CACHE_TTL_H - int((ahora - _last_fetch).total_seconds() / 3600)
        print(f"   📦 TD caché activa: {len(_cache)} deals (siguiente descarga en ~{h_rest}h)")
        return _cache

    todos: list[dict] = []
    for feed in _FEEDS:
        tienda, fid = feed["tienda"], feed["fid"]
        print(f"   📡 TD feed: {tienda} (fid={fid})...")
        raw = _fetch_unlimited(fid)
        filtrados = _filtrar(raw, tienda, descuento_minimo, precio_minimo, precio_maximo)
        print(f"      → {len(raw)} descargados, {len(filtrados)} con ≥{descuento_minimo}% descuento")
        todos.extend(filtrados)

    _cache = todos
    _last_fetch = ahora
    print(f"   ✅ TD total: {len(todos)} deals de {len(_FEEDS)} tiendas")
    return todos
