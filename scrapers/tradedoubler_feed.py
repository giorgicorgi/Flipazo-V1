"""
scrapers/tradedoubler_feed.py — Feeds de producto Tradedoubler.

Tiendas activas:
  MediaMarkt ES  fid=24915  strike_price = MSRP/precio ref. regulado
  PCBox ES       fid=50247  PreviousPrice = precio ref. regulado (monitores, cajas, componentes)
  Esdemarca ES   fid=116972 PreviousPRICE = precio ref. — solo marcas premium, descuento ≥50%

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

_API_BASE    = "https://api.tradedoubler.com/1.0/productsUnlimited.json"
_CACHE_TTL_H = 23

_cache: list[dict] = []
_last_fetch: datetime | None = None

# ---------------------------------------------------------------------------
# Constantes Esdemarca
# ---------------------------------------------------------------------------

_ESDEMARCA_DESCUENTO_MIN = 50   # umbral más estricto para moda
_ESDEMARCA_PRECIO_MIN    = 25.0
_ESDEMARCA_PRECIO_MAX    = 1200.0  # bolsos/abrigos premium superan los 800€ generales

_ESDEMARCA_MARCAS = {m.lower() for m in [
    "Polo Ralph Lauren", "Lauren Ralph Lauren", "Birkenstock", "BA&SH",
    "Hispanitas", "UGG", "Wonders", "Skechers", "Dr. Martens", "Dr Martens",
    "Weekend Max Mara", "Michael Kors", "Rotate", "Guess", "HOFF", "Art",
    "C.P. Company", "Premiata", "New Balance", "Asics", "Timberland",
    "A|X Armani Exchange", "Armani Exchange", "HOKA", "Barbour", "BOSS",
    "Hackett London", "Hackett", "Fred Perry",
]}

# Palabras que descartan el producto (búsqueda en título lowercase)
_ESDEMARCA_EXCLUIR = [
    # Ropa íntima y baño
    "calcetín", "calcetines", "calzoncillo", "calzoncillos", "bóxer", "boxer",
    "ropa interior", "pijama", "pijamas", "sujetador", "braga", "bragas", "tanga",
    "bañador", "bañadora", "bikini", "traje de baño", "moda baño",
    # Chanclas
    "chancla", "chanclas", "flip flop", "flip-flop",
    # Sudaderas
    "sudadera", "sudaderas", "hoodie",
    # Pantalones
    "pantalón", "pantalones", "vaquero", "vaqueros", "jogger", "joggers",
    "leggings", "leggins", "mallas",
    # Blusas / camisetas básicas
    "blusa", "blusas", "camiseta", "camisetas", "t-shirt",
    # Monos y bodys ("mono " con espacio para no bloquear "monedero")
    "mono de", "mono para", "jumpsuit", "pelele",
]

# Palabras clave de categorías aceptadas (en nombre de categoría TD o título)
_ESDEMARCA_INCLUIR = [
    # Calzado
    "calzado", "zapato", "zapatilla", "bota", "botín", "mocasín", "zueco",
    "sneaker", "deportiva", "oxford", "sandalia", "mercedita", "stiletto",
    # Complementos y accesorios
    "bolso", "cartera", "mochila", "maletín", "bandolera", "riñonera",
    "cinturón", "maleta", "trolley", "equipaje", "neceser", "billetera",
    # Prendas exteriores premium
    "chaqueta", "abrigo", "cazadora", "anorak", "parka", "trench", "blazer",
    "americana", "camisa", "camisas", "polo ", "jersey",
]

# Palabras que descartan por categoría TD (texto de categoría, lowercase)
_ESDEMARCA_CAT_EXCLUIR = [
    "camiseta", "sudadera", "pantalón", "ropa interior", "calcetín",
    "bañador", "bikini", "legging", "jogger", "blusa", "monos y",
]


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


def _clave_dedup_esdemarca(brand: str, titulo: str) -> str:
    """Clave de deduplicación para Esdemarca: elimina talla y atributos finales.

    Los títulos tienen formato: "BOSS Camisa algodón negra (M), Corte slim, ..."
    Se elimina todo desde el primer paréntesis para agrupar tallas del mismo modelo.
    """
    t = re.sub(r'\s*\([^)]*\).*$', '', titulo)
    return f"{brand.lower()}:{t[:70].lower()}"


def _filtrar_esdemarca(raw: list[dict], precio_minimo: float, precio_maximo: float) -> list[dict]:
    """
    Filtro específico para Esdemarca: solo marcas premium seleccionadas,
    categorías aceptadas (calzado, complementos, prendas exteriores),
    y descuento ≥50%. Se ignoran los parámetros genéricos y se usan las
    constantes _ESDEMARCA_* para mantener control independiente del pipeline.
    """
    resultado: list[dict] = []
    vistos: set[str] = set()

    for item in raw:
        try:
            # Marca (campo dedicado, más fiable que parsear el título)
            brand = (item.get("brand") or "").strip()
            if brand.lower() not in _ESDEMARCA_MARCAS:
                continue

            titulo = (item.get("name") or "").strip()
            if not titulo or len(titulo) < 8:
                continue

            titulo_lower = titulo.lower()

            # Exclusiones por título
            if any(excl in titulo_lower for excl in _ESDEMARCA_EXCLUIR):
                continue

            # Exclusiones por categoría TD
            cats_raw = item.get("categories") or []
            cat_text = " ".join(
                (c.get("name") or "") for c in cats_raw if isinstance(c, dict)
            ).lower()
            if any(kw in cat_text for kw in _ESDEMARCA_CAT_EXCLUIR):
                continue

            # Debe encajar en al menos una categoría aceptada
            texto_check = titulo_lower + " " + cat_text
            if not any(kw in texto_check for kw in _ESDEMARCA_INCLUIR):
                continue

            offers = item.get("offers") or []
            if not offers:
                continue
            offer = offers[0]

            price_history = offer.get("priceHistory") or []
            precio_actual = _parse_precio(
                (price_history[0].get("price") or {}).get("value") if price_history else None
            )
            if not (_ESDEMARCA_PRECIO_MIN <= precio_actual <= _ESDEMARCA_PRECIO_MAX):
                continue

            # Esdemarca usa "PreviousPRICE" (capital PRICE)
            fields_raw = item.get("fields", {})
            strike_raw = (
                _get_field(fields_raw, "PreviousPRICE")
                or _get_field(fields_raw, "PreviousPrice")
                or _get_field(fields_raw, "strike_price")
            )
            precio_original = _parse_precio(strike_raw)
            if precio_original <= precio_actual:
                continue

            descuento_pct = int((1 - precio_actual / precio_original) * 100)
            if descuento_pct < _ESDEMARCA_DESCUENTO_MIN:
                continue

            disponibilidad = (offer.get("availability") or "").lower()
            if disponibilidad not in ("in stock", "available", "en stock"):
                continue

            # Deduplicar por modelo (ignorar variantes de talla/color del mismo artículo)
            clave = _clave_dedup_esdemarca(brand, titulo)
            if clave in vistos:
                continue
            vistos.add(clave)

            resultado.append({
                "titulo":          titulo,
                "asin":            offer.get("productUrl", ""),
                "precio_actual":   precio_actual,
                "precio_original": precio_original,
                "descuento_pct":   descuento_pct,
                "tienda":          "Esdemarca",
                "imagen_url":      ((item.get("productImage") or {}).get("url") or ""),
            })
        except Exception:
            continue

    return resultado


# Cada feed puede tener filtrar_fn propio. None → _filtrar estándar.
_FEEDS = [
    {"tienda": "MediaMarkt", "fid": "24915", "filtrar_fn": None},
    {"tienda": "PCBox",      "fid": "50247", "filtrar_fn": None},
    {"tienda": "Esdemarca",  "fid": "116972", "filtrar_fn": _filtrar_esdemarca},
    # Beep: PreviousPrice es MSRP fabricante, no precio 30d → falsos descuentos sistemáticos.
    # {"tienda": "Beep", "fid": "51903", "filtrar_fn": None},
    # ToysRus: feed sin precio original → descuento incalculable.
    # {"tienda": "ToysRus", "fid": "21529", "filtrar_fn": None},
]


def fetch_tradedoubler_productos(
    descuento_minimo: int = 40,
    precio_minimo: float = 25.0,
    precio_maximo: float = 800.0,
) -> list[dict]:
    """
    Descarga y filtra los feeds de MediaMarkt, PCBox y Esdemarca de Tradedoubler.
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
        filtrar_fn = feed.get("filtrar_fn")
        print(f"   📡 TD feed: {tienda} (fid={fid})...")
        raw = _fetch_unlimited(fid)
        if filtrar_fn is not None:
            filtrados = filtrar_fn(raw, precio_minimo, precio_maximo)
        else:
            filtrados = _filtrar(raw, tienda, descuento_minimo, precio_minimo, precio_maximo)
        desc_min = _ESDEMARCA_DESCUENTO_MIN if tienda == "Esdemarca" else descuento_minimo
        print(f"      → {len(raw)} descargados, {len(filtrados)} con ≥{desc_min}% descuento")
        todos.extend(filtrados)

    _cache = todos
    _last_fetch = ahora
    print(f"   ✅ TD total: {len(todos)} deals de {len(_FEEDS)} tiendas")
    return todos
