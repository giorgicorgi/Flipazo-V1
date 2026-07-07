"""
scrapers/tradedoubler_feed.py — Feeds de producto Tradedoubler.

Tiendas activas:
  MediaMarkt ES  fid=24915  strike_price = MSRP/precio ref. regulado
  PCBox ES       fid=50247  PreviousPrice = precio ref. regulado (monitores, cajas, componentes)
  Esdemarca ES   fid=116972 PreviousPRICE = precio ref. — solo marcas premium, descuento ≥60%
  Toni Pons ES   fid=118025 PreviousPRICE = precio ref. — alpargatas/calzado mujer, descuento ≥40%
  Desigual ES    fid=256429 estructura INVERTIDA: priceHistory[0] = precio original, fields["Sale price"] = precio rebajado

Tiendas desactivadas:
  Beep ES        fid=51903  PreviousPrice = MSRP fabricante → migrado a beep_feed.py (historial propio)
  ToysRus ES     fid=21529  sin campo precio original → descuento incalculable

Descarga una vez al día (caché 23h). Devuelve list[dict] con los campos de Producto
listos para que flipazo_main los convierta y filtre con _es_producto_valido.
"""

import os
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

TRADEDOUBLER_TOKEN = os.getenv("TRADEDOUBLER_TOKEN", "")

_API_BASE    = "https://api.tradedoubler.com/1.0/productsUnlimited.json"
_CACHE_TTL_H = 23

_cache: list[dict] = []
_last_fetch: datetime | None = None
# Último resultado BUENO por feed (fid → deals filtrados). Si un feed falla la descarga
# (timeout/429), reutilizamos su último resultado en vez de dejar la tienda fuera 23h.
_feed_cache: dict[str, list[dict]] = {}

# ---------------------------------------------------------------------------
# Constantes Esdemarca
# ---------------------------------------------------------------------------

_ESDEMARCA_DESCUENTO_MIN = 50   # moda premium (bajado de 60→50 para más volumen de marca)
_ESDEMARCA_PRECIO_MIN    = 25.0
_ESDEMARCA_PRECIO_MAX    = 1200.0  # bolsos/abrigos premium superan los 800€ generales

_ESDEMARCA_MARCAS = {m.lower() for m in [
    # Premium italiano / lujo accesible
    "Polo Ralph Lauren", "Lauren Ralph Lauren", "Weekend Max Mara", "Max Mara",
    "Michael Kors", "Rotate", "C.P. Company", "Premiata", "Stone Island",
    "Moncler", "Karl Lagerfeld", "A|X Armani Exchange", "Armani Exchange",
    # Británico / heritage
    "Barbour", "BOSS", "Hugo Boss", "Hackett London", "Hackett",
    "Fred Perry", "Superdry", "Ben Sherman", "Lyle & Scott", "Belstaff",
    # Denim premium / casual
    "Tommy Hilfiger", "Tommy Jeans", "Calvin Klein", "Lacoste",
    "Diesel", "Pepe Jeans", "Levi's", "Levis", "Replay", "G-Star", "G-Star Raw",
    "Carhartt", "Scotch & Soda", "Dockers",
    # Streetwear / surf-skate
    "Element", "Quiksilver", "Billabong", "O'Neill", "Volcom",
    "Vans", "Converse", "DC Shoes",
    # Sport mainstream
    "Nike", "Adidas", "Puma", "Reebok", "New Balance", "Asics",
    "Champion", "Fila", "Le Coq Sportif", "Diadora", "Kappa",
    # Outdoor / running técnico
    "Columbia", "The North Face", "North Face", "Patagonia", "Helly Hansen",
    "Salomon", "Merrell", "Timberland", "HOKA", "On", "On Running",
    "Saucony", "Brooks", "Mizuno", "Berghaus", "Jack Wolfskin",
    # Calzado urbano y outlet
    "Birkenstock", "UGG", "Dr. Martens", "Dr Martens", "Skechers",
    "Camper", "Pikolinos", "Panama Jack", "Mustang", "Geox", "Clarks",
    "Hispanitas", "Wonders", "HOFF", "Art", "Crocs",
    # Mujer / lujo accesorios
    "BA&SH", "Guess", "Liu Jo", "Pinko", "Patrizia Pepe",
    "Furla", "Longchamp", "Coach", "Tory Burch", "Kate Spade",
    # Mid-luxury contemporáneo
    "Maje", "Sandro", "The Kooples",
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
    descuento_minimo_fn=None,
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
            # Umbral por producto: el callback baja a 30% para gran electrodoméstico caro
            # (lavadoras, secadoras, etc.); el resto mantiene el mínimo estándar.
            dmin = descuento_minimo_fn(titulo, precio_actual) if descuento_minimo_fn else descuento_minimo
            if descuento_pct < dmin:
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
    # Paso 1: recoger todos los candidatos válidos (sin dedup aún) para contar variantes
    candidatos: list[tuple[str, dict]] = []

    for item in raw:
        try:
            brand = (item.get("brand") or "").strip()
            if brand.lower() not in _ESDEMARCA_MARCAS:
                continue

            titulo = (item.get("name") or "").strip()
            if not titulo or len(titulo) < 8:
                continue

            titulo_lower = titulo.lower()

            if any(excl in titulo_lower for excl in _ESDEMARCA_EXCLUIR):
                continue

            cats_raw = item.get("categories") or []
            cat_text = " ".join(
                (c.get("name") or "") for c in cats_raw if isinstance(c, dict)
            ).lower()
            if any(kw in cat_text for kw in _ESDEMARCA_CAT_EXCLUIR):
                continue

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

            clave = _clave_dedup_esdemarca(brand, titulo)

            raw_url = offer.get("productUrl", "")
            idx = raw_url.rfind("url(")
            if idx != -1 and raw_url.endswith(")"):
                product_url = urllib.parse.unquote(raw_url[idx + 4 : -1])
            else:
                product_url = raw_url

            titulo_out = (
                f"{brand} {titulo}" if brand.lower() not in titulo_lower else titulo
            )

            candidatos.append((clave, {
                "titulo":          titulo_out,
                "asin":            product_url,
                "precio_actual":   precio_actual,
                "precio_original": precio_original,
                "descuento_pct":   descuento_pct,
                "tienda":          "Esdemarca",
                "imagen_url":      ((item.get("productImage") or {}).get("url") or ""),
            }))
        except Exception:
            continue

    # Paso 2: contar variantes por modelo y deduplicar
    conteo = Counter(c for c, _ in candidatos)
    vistos: set[str] = set()
    resultado: list[dict] = []
    for clave, d in candidatos:
        if clave in vistos:
            continue
        vistos.add(clave)
        n = conteo[clave]
        d["pocas_unidades"] = "Últimas unidades" if n == 1 else ("Pocas tallas" if n <= 3 else "")
        resultado.append(d)

    return resultado


# ---------------------------------------------------------------------------
# Constantes Toni Pons
# ---------------------------------------------------------------------------

_TONI_PONS_DESCUENTO_MIN = 60
_TONI_PONS_PRECIO_MIN    = 25.0
_TONI_PONS_PRECIO_MAX    = 200.0   # alpargatas raramente superan los 200€


def _filtrar_toni_pons(raw: list[dict], precio_minimo: float, precio_maximo: float) -> list[dict]:
    """
    Filtro para Toni Pons: calzado femenino (alpargatas/espadrilles).
    Usa PreviousPRICE como referencia de precio original.
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
            if not (_TONI_PONS_PRECIO_MIN <= precio_actual <= _TONI_PONS_PRECIO_MAX):
                continue

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
            if descuento_pct < _TONI_PONS_DESCUENTO_MIN:
                continue

            disponibilidad = (offer.get("availability") or "").lower()
            if disponibilidad not in ("in stock", "available", "en stock"):
                continue

            ean = ((item.get("identifiers") or {}).get("ean") or "")
            clave = ean if ean else titulo[:50].lower()
            if clave in vistos:
                continue
            vistos.add(clave)

            stock_raw = _get_field(fields_raw, "sell_on_google_quantity")
            stock_qty = int(stock_raw) if stock_raw.isdigit() else 0

            pocas = ""
            if stock_qty == 1:
                pocas = "Últimas unidades"
            elif 2 <= stock_qty <= 3:
                pocas = "Pocas tallas"

            resultado.append({
                "titulo":          titulo,
                "asin":            offer.get("productUrl", ""),
                "precio_actual":   precio_actual,
                "precio_original": precio_original,
                "descuento_pct":   descuento_pct,
                "tienda":          "Toni Pons",
                "imagen_url":      ((item.get("productImage") or {}).get("url") or ""),
                "stock_qty":       stock_qty,
                "pocas_unidades":  pocas,
            })
        except Exception:
            continue

    return resultado


# ---------------------------------------------------------------------------
# Constantes Desigual
# ---------------------------------------------------------------------------

_DESIGUAL_DESCUENTO_MIN = 50  # outlet Desigual ofrece pocos productos al 60%+; con 50% hay ~675/ciclo
_DESIGUAL_PRECIO_MIN    = 25.0
_DESIGUAL_PRECIO_MAX    = 300.0  # calzado y bolsos Desigual raramente superan los 300€

# Ropa básica que se descarta (búsqueda en título lowercase)
_DESIGUAL_EXCLUIR = [
    "camiseta", "camisetas", "t-shirt",
    "blusa", "blusas",
    "vestido", "vestidos",
    "pantalón", "pantalones",
    "vaquero", "vaqueros",
    "sudadera", "sudaderas", "hoodie",
    "mono de", "mono para", "jumpsuit",
    "calcetín", "calcetines",
    "pijama", "pijamas",
    "ropa interior", "bañador", "bikini",
    "leggings", "leggins", "mallas",
]


def _clave_dedup_desigual(titulo: str) -> str:
    """Agrupa tallas del mismo modelo: elimina tamaño numérico final (ej. ', 39')."""
    return re.sub(r',\s*\d{2}\s*$', '', titulo)[:70].lower()


def _filtrar_desigual(raw: list[dict], precio_minimo: float, precio_maximo: float) -> list[dict]:
    """
    Filtro para Desigual outlet.

    Estructura de precio INVERTIDA respecto a otros feeds TD:
      - priceHistory[0].price.value = precio original (precio RRP del artículo)
      - fields["Sale price"]        = precio actual rebajado en outlet

    Se excluye ropa básica; se permiten calzado, bolsos, accesorios y prendas exteriores.
    """
    # Paso 1: recoger candidatos válidos sin dedup para contar variantes por modelo
    candidatos: list[tuple[str, dict]] = []

    for item in raw:
        try:
            titulo = (item.get("name") or "").strip()
            if not titulo or len(titulo) < 8:
                continue

            titulo_lower = titulo.lower()
            if any(excl in titulo_lower for excl in _DESIGUAL_EXCLUIR):
                continue

            offers = item.get("offers") or []
            if not offers:
                continue
            offer = offers[0]

            price_history = offer.get("priceHistory") or []
            precio_original = _parse_precio(
                (price_history[0].get("price") or {}).get("value") if price_history else None
            )

            fields_raw = item.get("fields", {})
            precio_actual = _parse_precio(_get_field(fields_raw, "Sale price"))

            if not precio_actual or not precio_original:
                continue
            if not (_DESIGUAL_PRECIO_MIN <= precio_actual <= _DESIGUAL_PRECIO_MAX):
                continue
            if precio_original <= precio_actual:
                continue

            descuento_pct = int((1 - precio_actual / precio_original) * 100)
            if descuento_pct < _DESIGUAL_DESCUENTO_MIN:
                continue

            disponibilidad = (offer.get("availability") or "").lower()
            if disponibilidad and disponibilidad not in ("in stock", "available", "en stock"):
                continue

            clave = _clave_dedup_desigual(titulo)
            candidatos.append((clave, {
                "titulo":          titulo,
                "asin":            offer.get("productUrl", ""),
                "precio_actual":   precio_actual,
                "precio_original": precio_original,
                "descuento_pct":   descuento_pct,
                "tienda":          "Desigual",
                "imagen_url":      ((item.get("productImage") or {}).get("url") or ""),
            }))
        except Exception:
            continue

    # Paso 2: contar variantes por modelo y deduplicar
    conteo = Counter(c for c, _ in candidatos)
    vistos: set[str] = set()
    resultado: list[dict] = []
    for clave, d in candidatos:
        if clave in vistos:
            continue
        vistos.add(clave)
        n = conteo[clave]
        d["pocas_unidades"] = "Últimas unidades" if n == 1 else ("Pocas tallas" if n <= 3 else "")
        resultado.append(d)

    return resultado


def _filtrar_sale_price(raw, precio_minimo, precio_maximo, tienda, campo, desc_min=40):
    """Filtro genérico para feeds con estructura INVERTIDA (formato Google Shopping):
    priceHistory[0] = precio regular/original, fields[campo] = precio rebajado (sale).
    Publica solo si el rebajado es realmente < original y el descuento ≥ desc_min."""
    out: list[dict] = []
    for item in raw:
        try:
            titulo = (item.get("name") or "").strip()
            if not titulo or len(titulo) < 8:
                continue
            offers = item.get("offers") or []
            if not offers:
                continue
            offer = offers[0]
            ph = offer.get("priceHistory") or []
            precio_original = _parse_precio((ph[0].get("price") or {}).get("value") if ph else None)
            precio_actual   = _parse_precio(_get_field(item.get("fields", {}), campo))
            if not precio_actual or not precio_original or precio_original <= precio_actual:
                continue
            if not (precio_minimo <= precio_actual <= precio_maximo):
                continue
            descuento_pct = int((1 - precio_actual / precio_original) * 100)
            if descuento_pct < desc_min:
                continue
            disp = (offer.get("availability") or "").lower()
            if disp and disp not in ("in stock", "available", "en stock"):
                continue
            out.append({
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
    return out


def _filtrar_onebioshop(raw, precio_minimo, precio_maximo):
    # Cosmética natural/bio — feed Google Shopping con sale_price real (≥40%).
    return _filtrar_sale_price(raw, precio_minimo, precio_maximo, "OneBioShop", "sale_price", 40)


def _filtrar_tiendanimal(raw, precio_minimo, precio_maximo):
    # Mascotas — sale_price (pocas rebajas; solo publica bajadas reales ≥40%).
    return _filtrar_sale_price(raw, precio_minimo, precio_maximo, "Tiendanimal", "sale_price", 40)


# Cada feed puede tener filtrar_fn propio. None → _filtrar estándar.
_FEEDS = [
    {"tienda": "MediaMarkt", "fid": "24915",  "filtrar_fn": None},
    {"tienda": "PCBox",      "fid": "50247",  "filtrar_fn": None},
    {"tienda": "Esdemarca",  "fid": "116972", "filtrar_fn": _filtrar_esdemarca},
    {"tienda": "Toni Pons",  "fid": "118025", "filtrar_fn": _filtrar_toni_pons},
    {"tienda": "Desigual",   "fid": "256429", "filtrar_fn": _filtrar_desigual},
    {"tienda": "OneBioShop",  "fid": "117666", "filtrar_fn": _filtrar_onebioshop},
    {"tienda": "Tiendanimal", "fid": "50625",  "filtrar_fn": _filtrar_tiendanimal},
    # Beep: PreviousPrice es MSRP fabricante, no precio 30d → falsos descuentos sistemáticos.
    # {"tienda": "Beep", "fid": "51903", "filtrar_fn": None},
    # ToysRus: feed sin precio original → descuento incalculable.
    # {"tienda": "ToysRus", "fid": "21529", "filtrar_fn": None},
]

# ── Feeds SIN precio de referencia usable — modo SOLO HISTORIAL ────────────────
# Estos feeds no traen un precio "antes" fiable (ni strike_price/PreviousPrice ni un
# sale_price con descuento real), así que NO se pueden publicar todavía. Se ingieren
# solo para acumular historial de precios propio (price_history); en ~2 semanas habrá
# suficiente histórico para detectar bajadas reales (igual que hacemos con ECI/Decathlon).
_FEEDS_HISTORIAL = [
    {"tienda": "Braun",                    "fid": "39258"},   # braunhousehold.com
    {"tienda": "De'Longhi",                "fid": "37728"},   # delonghi.com
    {"tienda": "Tefal",                    "fid": "51766"},   # tefal.es
    {"tienda": "Suunto",                   "fid": "108428"},  # suunto.com
    {"tienda": "L'Occitane",               "fid": "19327"},   # loccitane.com
    {"tienda": "The Beauty Corner",        "fid": "49574"},   # thebeautycorner.eu
    {"tienda": "Eureka Electrodomésticos", "fid": "38346"},   # eurekaelectrodomesticos.es
    {"tienda": "DC Shoes",                 "fid": "42613"},   # dcshoes.es
    {"tienda": "Quiksilver",               "fid": "42467"},   # quiksilver.es
    {"tienda": "Roxy",                     "fid": "43218"},   # roxy.es
    {"tienda": "Element",                  "fid": "258062"},  # elementbrand.es
]

_cache_hist: list[dict] = []
_last_fetch_hist: "datetime | None" = None
_feed_cache_hist: dict[str, list[dict]] = {}


def _observacion_historial(item: dict, tienda: str, precio_minimo: float, precio_maximo: float):
    """Extrae una observación de precio (sin descuento) para price_history.
    Precio actual = priceHistory[0] (o price.value). id estable = product(fid-CODE) de la URL."""
    offers = item.get("offers") or []
    if not offers:
        return None
    off = offers[0]
    disp = (off.get("availability") or "").lower()
    if disp and disp not in ("in stock", "available", "en stock"):
        return None
    ph = off.get("priceHistory") or []
    val = (ph[0].get("price") or {}).get("value") if ph else (off.get("price") or {}).get("value")
    precio = _parse_precio(val)
    if not precio:
        # Algunos feeds (Suunto, Tefal, Roxy, Element…) traen el precio en un campo,
        # no en priceHistory: probar sale_price / discount_price / price.
        fields = item.get("fields", {})
        for campo in ("sale_price", "Sale price", "discount_price", "price", "Price"):
            precio = _parse_precio(_get_field(fields, campo))
            if precio:
                break
    if not precio or not (precio_minimo <= precio <= precio_maximo):
        return None
    titulo = (item.get("name") or "").strip()
    if len(titulo) < 8:
        return None
    url = off.get("productUrl", "")
    m = re.search(r"product\(([^)]+)\)", url)
    pid = m.group(1) if m else (url[:60] or titulo[:40].lower())
    return {"titulo": titulo, "precio_actual": precio, "precio_original": 0,
            "tienda": tienda, "asin": pid}


def fetch_tradedoubler_historial(precio_minimo: float = 8.0, precio_maximo: float = 800.0) -> list[dict]:
    """Observaciones de precio de los feeds SIN precio de referencia (NO publicables aún).
    Devuelve list[dict] {titulo, precio_actual, precio_original:0, tienda, asin} para
    registrar en price_history. Caché 23h propia (separada de la de deals publicables)."""
    global _cache_hist, _last_fetch_hist
    if not TRADEDOUBLER_TOKEN:
        return []
    ahora = datetime.now()
    if _last_fetch_hist and (ahora - _last_fetch_hist) < timedelta(hours=_CACHE_TTL_H):
        return _cache_hist

    obs: list[dict] = []
    total_raw = 0
    fallos = 0
    for feed in _FEEDS_HISTORIAL:
        tienda, fid = feed["tienda"], feed["fid"]
        raw = _fetch_unlimited(fid)
        total_raw += len(raw)
        if not raw:
            prev = _feed_cache_hist.get(fid, [])
            fallos += 1
            obs.extend(prev)
            continue
        feed_obs = [o for o in (_observacion_historial(x, tienda, precio_minimo, precio_maximo) for x in raw) if o]
        _feed_cache_hist[fid] = feed_obs
        print(f"   🗂️  TD historial: {tienda} (fid={fid}) → {len(raw)} prod, {len(feed_obs)} observaciones")
        obs.extend(feed_obs)

    if total_raw == 0:
        return _cache_hist
    _cache_hist = obs
    if fallos == 0:
        _last_fetch_hist = ahora
    return obs


def fetch_tradedoubler_productos(
    descuento_minimo: int = 40,
    precio_minimo: float = 25.0,
    precio_maximo: float = 800.0,
    descuento_minimo_fn=None,
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
    total_raw = 0
    fallos = 0  # feeds cuya descarga falló (0 productos crudos)
    for feed in _FEEDS:
        tienda, fid = feed["tienda"], feed["fid"]
        filtrar_fn = feed.get("filtrar_fn")
        print(f"   📡 TD feed: {tienda} (fid={fid})...")
        raw = _fetch_unlimited(fid)
        total_raw += len(raw)
        desc_min_map = {"Esdemarca": _ESDEMARCA_DESCUENTO_MIN, "Toni Pons": _TONI_PONS_DESCUENTO_MIN, "Desigual": _DESIGUAL_DESCUENTO_MIN}
        desc_min = desc_min_map.get(tienda, descuento_minimo)
        if not raw:
            # Descarga fallida (timeout/429/red). Estos feeds SIEMPRE traen miles de
            # productos, así que 0 crudos = error, no "sin ofertas". Reutilizamos el
            # último resultado bueno de ESTE feed para no dejar la tienda fuera 23h.
            prev = _feed_cache.get(fid, [])
            fallos += 1
            print(f"      ⚠️  0 descargados (fallo de red) → reutilizando caché previa de {tienda}: {len(prev)} deals")
            todos.extend(prev)
            continue
        if filtrar_fn is not None:
            filtrados = filtrar_fn(raw, precio_minimo, precio_maximo)
        else:
            filtrados = _filtrar(raw, tienda, descuento_minimo, precio_minimo, precio_maximo, descuento_minimo_fn)
        _feed_cache[fid] = filtrados  # guardar último resultado bueno por feed
        print(f"      → {len(raw)} descargados, {len(filtrados)} con ≥{desc_min}% descuento")
        todos.extend(filtrados)

    # Si NINGÚN feed devolvió datos, es un fallo de descarga (red / 429 rate-limit),
    # no un "0 deals" legítimo. No cacheamos el vacío para no bloquear 23h: reintentamos
    # en el próximo ciclo y mantenemos la caché anterior si la había.
    if total_raw == 0:
        print(f"   ⚠️  TD: 0 productos descargados en todos los feeds (red/429) — no se cachea, se reintentará. Caché previa: {len(_cache)} deals")
        return _cache

    _cache = todos
    # Solo fijamos la caché de 23h si TODOS los feeds cargaron. Si alguno falló (y se
    # rellenó con su caché previa), NO marcamos _last_fetch → se reintenta el próximo
    # ciclo hasta que todos carguen, evitando dejar una tienda ausente 23h.
    if fallos == 0:
        _last_fetch = ahora
    else:
        print(f"   ⚠️  {fallos} feed(s) fallaron — no se fija caché 23h, se reintentará el próximo ciclo")
    print(f"   ✅ TD total: {len(todos)} deals de {len(_FEEDS)} tiendas")
    return todos
