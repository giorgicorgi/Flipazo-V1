#!/usr/bin/env python3
"""
Flipazo Amazon - Pipeline MVP
Amazon.es → CamelCamelCamel (precio histórico) → Claude AI → Wallapop → Telegram
"""

import asyncio
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# import anthropic  # pausado — zona gris resuelta con heurística local
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page
from playwright_stealth import Stealth

from affiliate.link_builder import build_affiliate_url
from scrapers.pss_email import get_pss_productos
from scrapers.tradedoubler_feed import fetch_tradedoubler_productos, fetch_tradedoubler_historial
from scrapers.awin_feed         import fetch_awin_productos
import scrapers.awin_feed       as awin_feed_mod   # para leer ultimo_fetch_truncado
from scrapers.price_drop        import revalidar_publicados
from scrapers.awin_promotions   import fetch_awin_promociones
from scrapers.tradedoubler_vouchers import fetch_td_vouchers
from scrapers.decathlon_feed   import fetch_decathlon_productos
from scrapers.toysrus_feed     import fetch_toysrus_productos
from discovery import calcular_deal_score, asignar_tags, generar_hooks_batch

load_dotenv()

# ── Credenciales (desde .env) ────────────────────────────────────
TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY")
REDIRECT_BASE_URL    = os.getenv("REDIRECT_BASE_URL", "https://flipazo.es")  # dominio propio para /r/{id}
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")  # chat personal para alertas de error

# ── Threads (Meta) ────────────────────────────────────────────────
# Setup: developers.facebook.com → App → Threads API → User token
# Permisos necesarios: threads_basic, threads_content_publish
# ── Keepa (historial de precios Amazon) ──────────────────────────
# API docs: https://keepa.com/#!api  — domain 9 = Amazon.es
# Tier free: 100 tokens/día. Deal típico = 1 token. Plan $15/mes = 2500 tokens/día.
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "")

THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")   # ID numérico del usuario Threads
THREADS_TOKEN   = os.getenv("THREADS_TOKEN", "")     # Long-lived access token (60 días)

# ── WhatsApp Cloud API (Meta) ─────────────────────────────────────
# Setup: developers.facebook.com → App → WhatsApp → Getting Started
# Número de teléfono verificado en Meta Business → PHONE_NUMBER_ID
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")  # ID del número de negocio
WA_TOKEN           = os.getenv("WA_TOKEN", "")            # System user token permanente

# ── Umbrales Track A: ARBITRAJE (reventa) ────────────────────────
DESCUENTO_MINIMO        = 40    # % mínimo
PRECIO_MINIMO           = 25.0  # € mínimo producto
PRECIO_MAXIMO           = 9999.0 # € sin límite superior — publicar todos los deals válidos
SCORE_ARBITRAJE_MINIMO  = 60    # Score reventa mínimo para ir a Wallapop
BENEFICIO_NETO_MINIMO   = 20.0  # € margen neto real mínimo para publicar
RATIO_HISTORICO_MAX     = 1.20  # Precio actual <= 120% del mínimo histórico CCC (era 1.15)
RATIO_PRECIO_REF_INFLADO = 1.25 # Si precio_original > 125% del promedio histórico → referencia inflada artificialmente
_DESC_MAX_SIN_VERIFICAR = 70    # Amazon SIN historial ni CCC: un descuento ≥70% es inverificable → descartar (evita refs infladas tipo SSD 1249€→340€)

# ── Umbrales Track B: OFERTA PURA (sin reventa) ──────────────────
SCORE_OFERTA_MINIMO     = 58    # Score calidad/valor mínimo
DESCUENTO_OFERTA_MINIMO = 40    # % mínimo para ofertas puras

# ── Low Cost (productos < PRECIO_MINIMO) ──────────────────────────
PRECIO_MINIMO_LC    = 8.0   # € mínimo para aceptar items low cost
DESCUENTO_LC_MINIMO = 40    # % mínimo para items low cost (igual que descuento estándar)

# ── Gran electrodoméstico (gasto fuerte) ──────────────────────────
# Lavadoras, secadoras, lavavajillas, frigoríficos, hornos, etc. casi nunca bajan del 40%
# (MediaMarkt los rebaja típicamente 20-30%), pero son una compra cara donde el ahorro
# ABSOLUTO importa (−30% de 600€ = 180€). Umbral reducido a 30% si el precio es alto.
# La card lleva un disclaimer ("no llega al 40%, pero es un gran ahorro").
GRAN_ELECTRO_PRECIO_MIN    = 300.0
GRAN_ELECTRO_DESCUENTO_MIN = 30
_GRAN_ELECTRO_RE = re.compile(
    r'\b(lavadora|secadora|lavavajillas|frigor[ií]fico|nevera|congelador|'
    r'vitrocer[aá]mic\w*|campana|microondas)\b|placa.*inducci[oó]n|\bhorno\b',
    re.I,
)

def _es_gran_electrodomestico(titulo: str, precio: float) -> bool:
    """True si el título es un gran electrodoméstico y el precio supera el suelo."""
    return precio >= GRAN_ELECTRO_PRECIO_MIN and bool(_GRAN_ELECTRO_RE.search(titulo or ""))

def _descuento_minimo_para(titulo: str, precio: float) -> int:
    """Umbral de descuento aplicable: 30% para gran electrodoméstico caro, 40% normal."""
    return GRAN_ELECTRO_DESCUENTO_MIN if _es_gran_electrodomestico(titulo, precio) else DESCUENTO_MINIMO

# ── Pipeline ─────────────────────────────────────────────────────
BATCH_SIZE_CLAUDE       = 15    # Productos por llamada a la API
DEBUG_SCREENSHOTS       = os.getenv("DEBUG_SCREENSHOTS", "false").lower() == "true"

# ── Deduplicación ────────────────────────────────────────────────
DB_PATH         = "flipazo_deals.db"
DEDUP_TTL_HORAS = 168  # No republica el mismo deal hasta pasadas 168h (7 días)

# ── Scheduling ───────────────────────────────────────────────────
# Loop rápido: Amazon flash deals cada 60 min
# Loop completo: Todas las tiendas cada 2h (análisis profundo)
CICLO_FLASH_MIN         = 60
CICLO_COMPLETO_MIN      = 120

# ── Costes reales de reventa en Wallapop ─────────────────────────
WALLAPOP_COMISION       = 0.13  # 13% (10% comisión + ~3% pasarela de pago)
WALLAPOP_ENVIO          = 5.0   # € envío medio (Correos/MRW)

# ── Límite de productos del mismo tipo por ciclo ─────────────────
MAX_MISMO_TIPO          = 99    # Sin límite efectivo — publicar todos los deals válidos
MAX_PUBLICAR_POR_TIPO   = 99    # Sin límite efectivo — publicar todos los deals válidos
WALLAPOP_EMBALAJE       = 2.0   # € materiales embalaje
WALLAPOP_COSTES_FIJOS   = WALLAPOP_ENVIO + WALLAPOP_EMBALAJE  # 7€

# ── Cobertura del catálogo Amazon: matriz (categoría × marca) ────────────────────
# CLAVE: Amazon topa cada búsqueda en ~400 resultados. Una query multi-marca
# (`k=samsung+lg+sony+...`) colapsa todas esas marcas en 400 resultados totales; UNA query
# por marca da a CADA marca su propio universo de ~400 → separar marcas multiplica la
# cobertura del catálogo. El descuento se filtra en la URL (`_AMAZON_DISCOUNT_NODE`).
_AMAZON_DISCOUNT_NODE = "p_n_pct-off-with-tax%3A2388626011"
_AMAZON_SORT          = "exact-aware-popularity-rank"

# categoría i= de Amazon → marcas (cada par es una búsqueda propia)
_AMAZON_MARCAS = {
    "electronics": ["samsung", "lg", "sony", "xiaomi", "philips", "panasonic", "tcl", "hisense",
                    "jbl", "bose", "marshall", "anker", "sonos", "tp-link", "logitech", "sandisk",
                    "kingston", "seagate", "western digital", "crucial", "huawei", "echo dot",
                    "fire tv", "airpods", "quietcomfort", "sennheiser", "jabra", "hyperx"],
    "computers":   ["lenovo", "hp", "dell", "asus", "acer", "msi", "gigabyte", "corsair", "razer",
                    "logitech", "steelseries", "netgear", "microsoft surface", "aoc", "benq"],
    "videogames":  ["nintendo switch", "playstation 5", "xbox", "ps5", "ps4", "juego switch",
                    "mando gaming", "turtle beach"],
    "shoes":       ["nike", "adidas", "new balance", "asics", "puma", "reebok", "vans", "converse",
                    "skechers", "timberland", "salomon", "brooks", "saucony", "hoka"],
    "beauty":      ["perfume", "eau de parfum", "colonia", "la roche posay", "isdin", "cerave",
                    "avene", "vichy", "eucerin", "bioderma", "sesderma", "l'oreal", "garnier",
                    "nivea", "maybelline", "rituals", "dyson airwrap"],
    "kitchen":     ["nespresso", "delonghi", "cecotec", "tefal", "kenwood", "kitchenaid", "krups",
                    "moulinex", "russell hobbs", "taurus", "braun", "instant pot", "sage", "oster"],
    "appliances":  ["dyson", "samsung", "lg", "bosch", "siemens", "balay", "whirlpool", "haier",
                    "beko", "candy", "aeg", "hisense", "teka", "rowenta", "shark", "irobot roomba"],
    "hpc":         ["braun", "philips", "oral-b", "remington", "wahl", "cecotec", "gillette",
                    "babyliss", "xiaomi", "ghd"],
    "toys":        ["playmobil", "hasbro", "mattel", "hot wheels", "funko", "nerf", "barbie", "lego",
                    "fisher-price", "ravensburger", "schleich", "vtech", "clementoni", "spin master",
                    "bandai", "bruder", "famosa", "play-doh", "monopoly"],
    "watches":     ["casio", "seiko", "g-shock", "citizen", "fossil", "garmin", "polar", "suunto",
                    "amazfit", "festina", "tissot", "michael kors"],
    "photo":       ["canon", "nikon", "sony", "gopro", "fujifilm", "dji", "insta360"],
    "sports":      ["garmin", "fitbit", "polar", "amazfit", "under armour", "salomon",
                    "the north face", "columbia", "wilson", "head"],
    "diy":         ["bosch", "dewalt", "makita", "milwaukee", "karcher", "stanley", "black decker",
                    "einhell", "worx", "ryobi", "gardena", "bahco"],
    "luggage":     ["samsonite", "american tourister", "eastpak", "delsey", "roncato", "gabol"],
    "baby":        ["chicco", "maxi-cosi", "cybex", "jane", "philips avent", "nuk", "suavinex"],
    "automotive":  ["michelin", "bosch coche", "castrol", "osram", "xiaomi patinete", "garmin gps"],
}
# Categorías sin i= fiable → búsqueda de marca en todo el catálogo (igual de válida)
_AMAZON_MARCAS_GLOBAL = ["yamaha", "fender", "roland teclado", "hp impresora", "epson impresora",
                         "brother impresora", "royal canin", "purina"]

def _build_amazon_urls() -> list[str]:
    base = "https://www.amazon.es/s?{cat}k={k}&rh=" + _AMAZON_DISCOUNT_NODE + "&s=" + _AMAZON_SORT
    urls = []
    for cat, marcas in _AMAZON_MARCAS.items():
        for m in marcas:
            urls.append(base.format(cat=f"i={cat}&", k=urllib.parse.quote_plus(m)))
    for m in _AMAZON_MARCAS_GLOBAL:
        urls.append(base.format(cat="", k=urllib.parse.quote_plus(m)))
    return urls

AMAZON_SEARCH_URLS = _build_amazon_urls()   # ~230 búsquedas (categoría × marca)

# Nº de búsquedas por ciclo (rotación) — barre todo el catálogo en varios ciclos sin
# martillear Amazon de golpe (limita CAPTCHA y tiempo de ciclo). Env-ajustable.
_AMAZON_QUERIES_POR_CICLO = int(os.getenv("AMAZON_QUERIES_POR_CICLO", "45"))

# Página principal de deals (fuente extra, JS-heavy)
AMAZON_DEALS_URL = "https://www.amazon.es/deals"

# Nº de páginas por búsqueda. Con la matriz marca×categoría (~230 búsquedas) la cobertura
# la da la AMPLITUD (muchas marcas distintas), no la profundidad, así que por defecto 1 página
# por búsqueda (más marcas por ciclo, menos riesgo de CAPTCHA). Subir a 2+ si interesa. La
# paginación se corta si una página viene vacía/bloqueada. Env-ajustable.
_AMAZON_PAGINAS = int(os.getenv("AMAZON_PAGINAS", "1"))

# ── PcComponentes — ofertas especiales ordenadas por % descuento ──
# La página usa React (SPA): esperar networkidle antes de evaluar el DOM
PCCOMPONENTES_URLS = [
    "https://www.pccomponentes.com/ofertas-especiales?sort=discount",
    "https://www.pccomponentes.com/ofertas-especiales?sort=discount&page=2",
    "https://www.pccomponentes.com/ofertas-especiales?sort=discount&page=3",
    "https://www.pccomponentes.com/componentes?sort=discount",
    "https://www.pccomponentes.com/portatiles?sort=discount",
]

# ── Barrabes — outlet de montaña/esquí/trail/escalada ────────────
BARRABES_URLS = [
    "https://www.barrabes.com/outlet/outlet/o-269",        # outlet general (~427 productos)
    "https://www.barrabes.com/outlet/ultimas-tallas/o-518", # últimas tallas (~75 productos, hasta 80% off)
]

# ── Mammoth Bikes — outlet pages ──────────────────────────────────
MAMMOTH_URLS = [
    "https://www.mammothbikes.com/outlet/ultimas-unidades/o-2857",
    "https://www.mammothbikes.com/outlet/outlet-bicicletas/o-2864",
    # Liquidación primavera — consolidación de cascos/accesorios/componentes/marcas (antes 404)
    "https://www.mammothbikes.com/outlet/liquidacion-primavera/o-3211",
]
# Bicicletas pueden superar los 800€ del PRECIO_MAXIMO general
PRECIO_MAXIMO_BICI = 5000.0

# ── Palabras prohibidas — productos sin potencial de reventa ─────
PALABRAS_PROHIBIDAS = [
    # Accesorios de bajo valor
    "funda", "case", "carcasa", "cristal", "correa", "cable", "tóner",
    "repuesto", "adhesivo", "soporte", "cargador", "adaptador",
    "stylus", "pellicola",
    # "protector" en frases específicas (protector de pantalla/cristal/cámara). NO bloquear
    # "protector" sola: cazaba "protector solar" (dermocosmética de marca) por subcadena.
    "protector de pantalla", "protector pantalla",
    "protector de cristal", "protector cristal",
    "protector de cámara", "protector cámara", "protector de lente",
    # Alimentación y salud (frases específicas, no "café" sola — bloquearía cafeteras)
    "café en grano", "café molido", "café soluble", "cápsulas de café",
    "café en cápsulas", "té verde", "té negro", "té rojo", "infusión",
    "cacao en polvo", "chocolate negro", "chocolate con leche",
    "vitamina", "suplemento",
    "proteína", "colágeno", "omega", "snack", "galleta", "barrita",
    # Belleza básica (no perfumes de marca)
    "champú", "acondicionador", "gel de ducha", "crema hidratante",
    "sérum", "mascarilla facial", "esmalte de uñas",
    # Limpieza del hogar
    "detergente", "suavizante", "limpiador", "desinfectante",
    "ambientador", "bayeta", "fregona",
    # Papelería
    "bolígrafo", "rotulador", "agenda", "cuaderno", "carpeta", "archivador",
    # Textil básico sin valor de reventa (independientemente de la marca)
    "calcetines", "ropa interior", "boxer", "calzoncillo", "pijama",
    "sábanas", "toalla", "almohada",
    "vaquero", "vaqueros", "jeans", "jean", "pantalón", "pantalones",
    "leggins", "leggings", "mallas", "medias", "bragas",
    # NOTA: camiseta, sudadera, chaqueta, polo, etc. se filtran en _es_producto_valido
    # con lógica contextual (marca conocida + descuento ≥50% los permite)
    # Zapatillas de gama baja (modelos básicos sin valor de reventa)
    "tanjun", "revolution", "quest ", "court vision", "downshifter",
    "cloudfoam", "lite racer", "run 60s", "run 70s", "grand court",
    "response", "duramo", "breaknet",
    # Libros y medios físicos
    "libro", " novela ", "manual de", "guía de", "dvd", "blu-ray",
    # Multipacks genéricos (no bloquear si incluye marca de herramienta: "Kit Makita 18V")
    "lote de", "caja de",
    # Estado del producto — no deals de segunda mano ni refurbished
    "reacondicionado", "reacondicionada", "reacondicionados", "reacondicionadas",
    "seminuevo", "seminueva", "seminuevos", "seminuevas",
    "remanufacturado", "remanufacturada",
    # Accesorios genéricos
    "accesorio",
    # Repuestos de electrodomésticos
    "junta",        # juntas/sellos de recambio (ej. Breville, cafetera)
    # Recambios y repuestos (ya está "repuesto" — añadir plurales y variantes)
    "recambio", "recambios",
    # Consumibles de impresora — "cartucho" cubre todos los casos; "toner"/"tóner" ambas grafías
    "cartucho", "toner", "tóner", "kit de tinta",
    # Pilas sueltas (baterías como producto principal, no accesorios de otro artículo)
    "pilas aa",      # AA y AAA — "pilas aa" es subcadena de "pilas aaa"
    "pilas cr",      # pilas de litio tipo botón (CR2032, CR123, etc.)
    "pilas lr",      # código IEC de pilas alcalinas (LR6=AA, LR03=AAA, etc.)
    "pack de pilas", "pilas alcalinas", "pilas recargables",
    # Periféricos de bajo valor
    "hub usb", "ladrón usb",
    # Bases y docks de carga sueltos
    "base de carga", "estación de carga",
    # Organizadores (no son producto de valor)
    "organizador de cables", "organizador de escritorio",
    # Consumibles de limpieza/jardín
    "manguera",
    # Suscripciones y productos digitales (sin valor de reventa)
    "código de descarga",   # licencias/juegos digitales sin soporte físico
    "antivirus",            # siempre suscripción de software (McAfee, Norton, etc.)
    "suscripción",          # productos puramente de suscripción (software, servicios)
    # Recambios de máquinas de café (ULKA solo hace bombas vibrantes para cafeteras)
    "ulka",             # marca exclusiva de bombas de presión para cafeteras
    "bomba vibrante",   # bomba de presión genérica (siempre recambio de cafetera)
    "electroválvula",   # válvula solenoide (recambio de electrodoméstico)
    # Componentes hidráulicos de frenos de bicicleta
    "oliva+pin",        # kit de racores hidráulicos (ej. Shimano SM-BH59)
    "oliva pin",        # variante sin signo +
    "kit de sangrado",  # kit de purga de frenos hidráulicos
    "kit sangrado",
]

# ── Dermocosmética premium (whitelist) ────────────────────────────
# Marcas de farmacia/dermo reconocidas con valor real. Solo estas se aceptan en
# cuidado facial/solar; el resto de cosmética genérica sigue bloqueada.
_MARCAS_DERMO = frozenset([
    "la roche-posay", "la roche posay", "roche-posay", "roche posay",
    "isdin", "cerave", "avène", "avene", "vichy", "eucerin", "bioderma",
    "sesderma", "svr", "filorga", "caudalie", "nuxe", "a-derma", "aderma",
    "cetaphil", "ducray", "martiderm", "endocare", "heliocare", "uriage",
    "rilastil", "mustela", "neutrogena", "babé", "cantabria labs", "anthelios",
])
# Términos de cosmética que normalmente bloqueamos (bajo valor genérico) pero que
# SÍ permitimos cuando el producto es de una marca dermo premium de la whitelist.
_PALABRAS_COSMETICA = frozenset([
    "crema hidratante", "sérum", "mascarilla facial", "champú",
    "acondicionador", "gel de ducha", "esmalte de uñas",
])
# Droguería / gama media: marcas de gran consumo que la gente busca en chollo
# (maquillaje, capilar, solar, higiene, afeitado). Se reconocen como marca y se
# eximen del bloqueo de cosmética genérica (champú de marca ≠ champú sin marca).
# Solo formas SEGURAS como subcadena (las ambiguas —dove, essence, axe, lacer,
# astor— van únicamente en las listas con límite de palabra \b).
_MARCAS_DROGUERIA = frozenset([
    # Maquillaje
    "maybelline", "l'oréal", "l'oreal", "loreal", "rimmel", "revlon", "max factor",
    "catrice", "nyx", "bourjois", "kiko milano", "deborah milano",
    # Capilar
    "pantene", "garnier", "elvive", "fructis", "tresemmé", "tresemme", "syoss",
    "schwarzkopf", "gliss", "wella", "herbal essences", "john frieda", "ogx",
    "aussie", "batiste",
    # Facial / corporal
    "nivea", "sanex", "johnson's", "johnsons", "denenes", "natural honey",
    # Solar
    "piz buin", "ambre solaire", "delial",
    # Higiene bucal
    "colgate", "sensodyne", "parodontax", "listerine",
    # Afeitado / depilación
    "gillette", "wilkinson", "schick", "veet",
    # Desodorante
    "rexona",
])

# Marcas cosméticas relevantes para OneBioShop (cosmética natural/bio + K-beauty + premium).
# OneBioShop tiene 112k productos; sin filtro de marca colaban marcas básicas/sin relevancia
# (y hasta calzado). Solo se publican marcas cosméticas reconocidas. Formas seguras como subcadena.
_MARCAS_KBEAUTY_BIO = frozenset([
    # Cosmética natural / bio
    "cocunat", "freshly cosmetics", "purobio", "so bio etic", "sobio etic", "weleda",
    "cattier", "logona", "lavera", "dr. hauschka", "hauschka", "melvita", "florame",
    "natura siberica", "alma secret",
    # Skincare / maquillaje premium
    "clarins", "shiseido", "estée lauder", "estee lauder", "lancôme", "lancome", "biotherm",
    "kiehl's", "kiehls", "sisley", "l'occitane", "loccitane", "rituals", "elizabeth arden",
    "urban decay", "charlotte tilbury", "fenty", "the ordinary", "paula's choice",
    "paulas choice", "drunk elephant", "typology",
    # K-beauty
    "skin 1004", "purito", "some by mi", "cos de baha", "cosrx", "beauty of joseon",
    "isntree", "numbuzin", "round lab", "torriden", "mixsoon", "haruharu", "axis-y",
    "klairs", "benton", "missha", "innisfree", "laneige", "dr. jart", "dr jart", "medicube",
    "tirtir", "sioris", "pyunkang yul", "heimish", "abib", "mediheal", "skinfood",
    "holika holika", "banila co", "etude house", "iunik", "by wishtrend",
])
# Marcas cosméticas aceptables en OneBioShop = dermo + droguería + natural/K-beauty.
_MARCAS_COSMETICA_OK = _MARCAS_DERMO | _MARCAS_DROGUERIA | _MARCAS_KBEAUTY_BIO

# Recambios y componentes de bicicleta — bloqueados solo para Mammoth Bikes
# (términos demasiado especializados; no aplica globalmente porque en otros contextos
# "cassette" puede ser electrónica, "freno" puede ser pieza de coche, etc.)
MAMMOTH_COMPONENTES = frozenset([
    "piñón", "piñones", "biela", "bielas", "cassette",
    "desviador", "kit freno", "freno disco", "pastilla de freno",
    "horquilla", "cuadro ", "pedalier", "rodamiento",
    "cadena shimano", "cadena sram", "cadena kmc",
    "sillín", "manillar", "buje", "bujes",
    "cable de freno", "cable de cambio",
    "plato shimano", "plato sram",
    "potencia",     # manillar stem (ej. Cannondale C1, Specialized S-Works)
    "potencias",
    "tija",         # tija de sillín (seatpost)
    "hub rep kit",  # kit de reparación de buje (ej. Scott Hub Rep Kit)
    "hub repair kit",
    "chainring",    # plato de cadena en inglés (ej. Cannondale SpideRing Chainring)
    "spidering",    # sistema de plato Cannondale SpideRing
])

# Regex para desviadores/cambios: "Cambio Shimano 105 Trasero" — las palabras no son adyacentes
_CAMBIO_RE = re.compile(
    r'\bcambio\b.*\b(shimano|sram|campagnolo|campag|microshift|deore|ultegra|dura.?ace|105|apex|rival|force|red)\b'
    r'|\b(shimano|sram|campagnolo)\b.*\bcambio\b',
    re.IGNORECASE
)

# Ropa y calzado de ciclismo de Mammoth — solo si descuento ≥55%
_MAMMOTH_ROPA = frozenset(["maillot", "culote", "maillots", "culotes"])
_MAMMOTH_CALZADO_CICLO = frozenset([
    "zapatillas giro", "zapatillas shimano", "zapatillas sidi",
    "zapatillas fizik", "zapatillas northwave", "zapatillas bontrager",
    "zapatillas specialized", "zapatillas gaerne", "zapatillas bont",
    "zapatillas scott", "zapatillas lake",
])


def _mammoth_es_valido(titulo: str, descuento: int) -> bool:
    """Filtros específicos para Mammoth Bikes: bloquea recambios y requiere ≥55% para ropa."""
    t = titulo.lower()
    if any(c in t for c in MAMMOTH_COMPONENTES):
        return False
    if _CAMBIO_RE.search(titulo):
        return False
    if any(r in t for r in _MAMMOTH_ROPA) and descuento <= 60:
        return False
    if any(z in t for z in _MAMMOTH_CALZADO_CICLO) and descuento < 55:
        return False
    # Zapatillas genéricas en Mammoth son siempre de ciclismo → umbral 55%
    if "zapatillas" in t and descuento < 55:
        return False
    return True


# ── Modelo de datos ──────────────────────────────────────────────
@dataclass
class Producto:
    titulo: str
    asin: str
    precio_actual: float
    precio_original: float
    descuento_pct: int
    # Enriquecidos en pipeline
    tienda: str = "Amazon"
    tipo: str = "PENDIENTE"       # "ARBITRAJE" | "OFERTA" | "DESCARTAR"
    precio_historico_min: float = 0.0
    score_ai: int = 0
    score_liquidez: int = 0       # 0-100: rapidez de venta en Wallapop
    score_oferta: int = 0         # 0-100: calidad/valor como oferta pura
    resale_viable: bool = False
    precio_wallapop: float = 0.0
    razonamiento: str = ""
    copy: str = ""
    imagen_url: str = ""
    categoria: str = ""           # "tecnologia" | "herramientas" | "deportes" | etc.
    pros: list = field(default_factory=list)    # Hasta 3 puntos fuertes
    contras: list = field(default_factory=list) # Hasta 2 consideraciones
    # ── Stock (solo feeds TD con datos de inventario) ──────────────────────
    stock_qty: int = 0       # unidades en stock; 0 = desconocido
    pocas_unidades: str = "" # "Pocas tallas" | "Últimas unidades" | ""
    tallas: str = ""         # tallas disponibles (Esdemarca/Desigual), ej. "S, M, L"
    # Clave del producto en price_history — solo en deals detectados por bajada propia.
    # Permite revalidar más tarde si el descuento anunciado sigue en pie (ver price_drop).
    hist_pid: str = ""
    # ── Capa de discovery (poblada en Fase 4.5) ────────────────────────────
    deal_score:     int  = 0                          # 0-100 ranking discovery
    hook:           str  = ""                         # Titular emocional Haiku
    social_context: str  = ""                         # Frase contextual Haiku
    emotional_tags: list = field(default_factory=list)  # Tags emocionales

    @property
    def beneficio_neto(self) -> float:
        """Margen real tras comisión Wallapop (13%) + envío + embalaje (7€)."""
        if self.precio_wallapop <= 0:
            return 0.0
        return round(self.precio_wallapop * (1 - WALLAPOP_COMISION) - self.precio_actual - WALLAPOP_COSTES_FIJOS, 2)

    @property
    def roi(self) -> float:
        if self.precio_actual <= 0 or self.beneficio_neto <= 0:
            return 0.0
        return round(self.beneficio_neto / self.precio_actual * 100, 1)

    @property
    def url_affiliate(self) -> str:
        return build_affiliate_url(self.tienda, self.asin)

    @property
    def url_ccc(self) -> str:
        if self.tienda == "Amazon" and self.asin:
            return f"https://camelcamelcamel.com/es/product/{self.asin}"
        return ""

# ════════════════════════════════════════════════════════════════
# FASE 1 — SCRAPING AMAZON.ES
#
#  Estrategia dual:
#    A) Páginas de búsqueda por categoría → selectores estables, data-asin
#    B) Página /deals como fuente extra (JS-heavy, scroll necesario)
# ════════════════════════════════════════════════════════════════

async def scrape_amazon_deals(context: BrowserContext) -> list[Producto]:
    productos: list[Producto] = []
    vistos: set[str] = set()
    page = await context.new_page()

    # ── A) Búsqueda por categoría (fuente principal, selectores estables) ──
    # Rotación: escaneamos un tramo distinto del catálogo en cada ciclo (avanza ~1 tramo por
    # hora) → barremos las ~230 búsquedas en varios ciclos sin saturar Amazon en uno solo.
    if len(AMAZON_SEARCH_URLS) > _AMAZON_QUERIES_POR_CICLO:
        _off = (int(time.time() // 3600) * _AMAZON_QUERIES_POR_CICLO) % len(AMAZON_SEARCH_URLS)
        urls_ciclo = (AMAZON_SEARCH_URLS + AMAZON_SEARCH_URLS)[_off:_off + _AMAZON_QUERIES_POR_CICLO]
    else:
        urls_ciclo = AMAZON_SEARCH_URLS
    print(f"🔎 Amazon: {len(urls_ciclo)}/{len(AMAZON_SEARCH_URLS)} búsquedas este ciclo (rotación)")
    for i, url in enumerate(urls_ciclo):
        es_deals = "/deals" in url
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        categoria = "deals" if es_deals else parsed.get("i", parsed.get("k", [f"cat{i}"]))[0]
        print(f"\n📡 Categoría: {categoria} ...")
        # Paginación: los deals ≥40% están repartidos por varias páginas (orden por
        # popularidad). Leemos hasta _AMAZON_PAGINAS, parando si una viene vacía/bloqueada.
        paginas = 1 if es_deals else _AMAZON_PAGINAS
        for pg in range(1, paginas + 1):
            page_url = url if pg == 1 else f"{url}&page={pg}"
            try:
                ok = await _cargar_con_reintento(page, page_url, f"Amazon/{categoria} p{pg}")
                if not ok:
                    break

                if es_deals:
                    await asyncio.sleep(6)
                    await _scroll_pagina(page, veces=5)
                    await asyncio.sleep(3)
                    nuevos = await _extraer_de_deals(page, vistos)
                else:
                    await _scroll_pagina(page, veces=5)
                    nuevos = await _extraer_de_busqueda(page, vistos)

                if DEBUG_SCREENSHOTS and i == 0 and pg == 1:
                    await page.screenshot(path=f"debug_{categoria}.png")

                productos.extend(nuevos)
                print(f"   ✅ p{pg}: {len(nuevos)} nuevos | Total: {len(productos)}")
                if len(nuevos) == 0:
                    break  # categoría agotada / todo duplicado → no seguir paginando

            except Exception as e:
                print(f"   ❌ Error en {categoria} p{pg}: {e}")
                break
            await asyncio.sleep(2)

    # ── B) Página /deals (fuente extra, requiere más espera) ──────────────
    print(f"\n📡 Página /deals ...")
    try:
        ok = await _cargar_con_reintento(page, AMAZON_DEALS_URL, "Amazon/deals")
        if ok:
            # El widget de deals necesita tiempo para renderizarse con JS
            await asyncio.sleep(6)
            await _scroll_pagina(page, veces=5)
            await asyncio.sleep(3)

            titulo_pagina = await page.title()
            print(f"   📄 Título: {titulo_pagina}")

            if DEBUG_SCREENSHOTS:
                await page.screenshot(path="debug_deals.png")
                print(f"   📸 Screenshot: debug_deals.png")

            nuevos = await _extraer_de_deals(page, vistos)
            productos.extend(nuevos)
            print(f"   ✅ {len(nuevos)} productos nuevos | Total: {len(productos)}")

    except Exception as e:
        print(f"   ❌ Error en /deals: {e}")

    await page.close()
    print(f"\n✅ {len(productos)} productos únicos tras pre-filtro")
    return productos


async def _scroll_pagina(page: Page, veces: int = 3):
    """Scroll gradual para activar lazy loading."""
    for _ in range(veces):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await asyncio.sleep(1.2)


# ════════════════════════════════════════════════════════════════
# ANTI-DETECCIÓN: detector de bloqueo + comportamiento humano + reintentos
# ════════════════════════════════════════════════════════════════

# Señales que indican que el sitio nos ha bloqueado
_TITULOS_BLOQUEO = [
    "just a moment", "un momento",   # Cloudflare challenge EN/ES
    "verificación", "verification", "verifying", "verificando",
    "access denied", "acceso denegado",
    "robot", "captcha", "cloudflare", "403", "429", "pardon",
    "blocked", "bloqueado", "attention required", "too many requests",
    "rate limit", "forbidden", "denied", "error 1020",
]
_URLS_BLOQUEO = ["captcha", "blocked", "challenge", "verify?", "error", "/403", "/429"]
_SELECTORES_BLOQUEO = [
    "#cf-challenge-running", ".cf-error-code", "#recaptcha",
    ".h-captcha", '[data-hcaptcha-widget-id]',
    'iframe[src*="captcha"]', 'iframe[src*="recaptcha"]',
    'iframe[src*="challenge"]', "#px-captcha", "#distil_identify_cookie",
]


async def _detectar_bloqueo(page: Page) -> tuple[bool, str]:
    """
    Comprueba múltiples señales de bot-detection:
      1. Título de la página
      2. URL actual
      3. Elementos de challenge en el DOM
      4. Página demasiado corta (página de bloqueo vacía)
    Devuelve (bloqueado, motivo).
    """
    try:
        titulo = (await page.title()).lower()
        for senal in _TITULOS_BLOQUEO:
            if senal in titulo:
                return True, f"título: '{await page.title()}'"

        url_actual = page.url.lower()
        for senal in _URLS_BLOQUEO:
            if senal in url_actual:
                return True, f"URL sospechosa: {page.url}"

        for sel in _SELECTORES_BLOQUEO:
            try:
                if await page.locator(sel).count() > 0:
                    return True, f"elemento challenge: {sel}"
            except Exception:
                pass

        content = await page.content()
        if len(content) < 2500:
            return True, f"página demasiado corta ({len(content)} chars)"

    except Exception:
        pass

    return False, ""


async def _comportamiento_humano(page: Page):
    """
    Simula comportamiento humano para reducir señales de bot:
    movimientos de ratón aleatorios + scroll con variación natural.
    """
    try:
        vp = page.viewport_size or {"width": 1440, "height": 900}
        w, h = vp["width"], vp["height"]

        # Movimientos de ratón no lineales
        for _ in range(random.randint(3, 7)):
            await page.mouse.move(
                random.randint(80, w - 80),
                random.randint(80, h - 80),
            )
            await asyncio.sleep(random.uniform(0.08, 0.35))

        # Scroll con variaciones (no uniforme)
        for _ in range(random.randint(2, 5)):
            px = random.randint(150, 700)
            await page.evaluate(f"window.scrollBy(0, {px})")
            await asyncio.sleep(random.uniform(0.4, 1.4))

        await asyncio.sleep(random.uniform(0.8, 2.0))
    except Exception:
        pass


# Circuit breaker: { "StoreName": datetime_hasta_cuando_ignorar }
_store_block_until: dict[str, datetime] = {}
_CIRCUIT_BREAKER_MINUTOS = 60   # Skip la tienda 60 min tras 3 fallos consecutivos
_CIRCUIT_BREAKER_MAX_MIN = 720  # …duplicando en cada tanda hasta 12 h como máximo
_store_fail_count: dict[str, int] = {}

# Tiendas bloqueadas por Cloudflare sin solución conocida: NO alertar al admin por
# "Scraper bloqueado" (el circuit breaker sigue actuando; solo se silencia el aviso).
_NO_ALERTAR_BLOQUEO = {"PcComponentes"}


async def _cargar_con_reintento(
    page: Page,
    url: str,
    store: str,
    max_intentos: int = 3,
) -> bool:
    """
    Carga una URL detectando bloqueos y reintentando con comportamiento humano.
    Devuelve True si la página cargó sin bloqueo, False si se agotaron los intentos.
    Incluye circuit breaker: si una tienda falla 3 veces, se omite 60 minutos.
    """
    # Circuit breaker: comprobar si la tienda está en cooldown
    store_key = store.split("/")[0]  # "Amazon/electronics" → "Amazon"
    if store_key in _store_block_until:
        if datetime.now() < _store_block_until[store_key]:
            restante = int((_store_block_until[store_key] - datetime.now()).seconds / 60)
            print(f"   ⏭️  [{store_key}] Circuit breaker activo — {restante}min restantes")
            return False
        else:
            # Cooldown expirado: resetear
            del _store_block_until[store_key]
            _store_fail_count[store_key] = 0

    for intento in range(1, max_intentos + 1):
        try:
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await _aceptar_cookies(page)

            # Cloudflare JS challenge ("Un momento…") se auto-resuelve en 3-8s.
            # Esperamos hasta 20s antes de declarar bloqueo — sin esperar, siempre falla.
            titulo_inmediato = (await page.title()).lower()
            if "un momento" in titulo_inmediato or "just a moment" in titulo_inmediato:
                print(f"   ⏳ [{store}] CF challenge JS — esperando auto-resolución (≤20s)...")
                try:
                    await page.wait_for_function(
                        "(function(){ var t = document.title.toLowerCase();"
                        " return !t.includes('un momento') && !t.includes('just a moment'); })()",
                        timeout=20000,
                    )
                    print(f"   ✅ [{store}] CF challenge resuelto — continuando")
                except Exception:
                    pass  # Si no resolvió en 20s, _detectar_bloqueo lo confirmará

            await asyncio.sleep(random.uniform(2.5, 4.5))

            bloqueado, motivo = await _detectar_bloqueo(page)

            if not bloqueado:
                if intento > 1:
                    print(f"   ✅ [{store}] Acceso OK en intento {intento}")
                return True

            print(f"   🚫 [{store}] Bloqueo detectado (intento {intento}/{max_intentos}) — {motivo}")

            if intento < max_intentos:
                espera = random.uniform(25, 55)
                print(f"   ⏳ Simulando comportamiento humano y esperando {espera:.0f}s...")
                await _comportamiento_humano(page)
                await asyncio.sleep(espera)
                await page.reload(timeout=60000, wait_until="domcontentloaded")

        except Exception as e:
            print(f"   ❌ [{store}] Error en intento {intento}: {e}")
            if intento < max_intentos:
                await asyncio.sleep(random.uniform(10, 20))

    print(f"   ⚠️  [{store}] No accesible tras {max_intentos} intentos — tienda omitida en este ciclo")
    if store_key not in _NO_ALERTAR_BLOQUEO:
        alertar_admin(f"Scraper bloqueado: {store}", f"No accesible tras {max_intentos} intentos.\nURL: {url}")

    # Circuit breaker con espera creciente. Una tienda con el scraping bloqueado
    # de forma permanente (PcComponentes lleva semanas con el reto de Cloudflare)
    # gastaba un navegador y ~2 min en 3 intentos fallidos CADA ciclo. Con el
    # backoff, tras varias tandas fallidas se reintenta una vez cada varias horas:
    # sigue pudiendo recuperarse sola, pero deja de robar tiempo al resto.
    _store_fail_count[store_key] = _store_fail_count.get(store_key, 0) + 1
    tandas = _store_fail_count[store_key] // 3
    if _store_fail_count[store_key] >= 3:
        minutos = min(_CIRCUIT_BREAKER_MINUTOS * (2 ** (tandas - 1)), _CIRCUIT_BREAKER_MAX_MIN)
        _store_block_until[store_key] = datetime.now() + timedelta(minutes=minutos)
        print(f"   🔴 [{store_key}] Circuit breaker activado — pausando {minutos}min "
              f"(tanda {tandas} de fallos seguidos)")

    return False


async def _extraer_de_busqueda(page: Page, vistos: set) -> list[Producto]:
    """
    Extrae productos de páginas de búsqueda de Amazon.
    Usa data-asin (atributo estable) como ancla principal.
    """
    productos = []

    # data-asin está en el div raíz de cada resultado — selector muy estable
    cards = await page.locator('[data-component-type="s-search-result"][data-asin]').all()
    print(f"   📦 {len(cards)} resultados encontrados")

    for card in cards:
        try:
            asin = await card.get_attribute("data-asin") or ""
            if not asin or asin in vistos:
                continue

            # Descuento — badge con "%"
            descuento = 0
            for sel in ['.a-badge-text', '[class*="badge"]', '.s-badge-text']:
                loc = card.locator(sel)
                if await loc.count() > 0:
                    txt = await loc.first.inner_text()
                    m = re.search(r'(\d+)\s*%', txt)
                    if m:
                        descuento = int(m.group(1))
                        break

            # Calcular descuento desde precios si no hay badge
            precio_actual, precio_original = await _extraer_precios_busqueda(card)
            if precio_actual <= 0:
                continue
            if descuento == 0 and precio_original > precio_actual:
                descuento = round((1 - precio_actual / precio_original) * 100)

            # Registrar observación de precio ANTES del filtro — acumula historial propio.
            # precio_original es el "was price" de Amazon = referencia real de mercado.
            _registrar_precio_amazon(asin, precio_actual, precio_original)

            # Tope de descuento: >90% sin badge externo es siempre un error de precio por unidad/kg.
            if descuento > 90:
                continue

            if not _precio_aceptable(precio_actual, descuento):
                continue

            # Título
            titulo = ""
            for sel in ['h2 span', 'h2 a span', '.a-text-normal']:
                loc = card.locator(sel)
                if await loc.count() > 0:
                    titulo = (await loc.first.inner_text()).strip()
                    if len(titulo) > 10:
                        break

            if not titulo or not _es_producto_valido(titulo, descuento, precio=precio_actual):
                continue

            # Imagen del producto — Amazon usa lazy loading, probar src, data-src y srcset
            imagen_url = ""
            img_loc = card.locator('img.s-image')
            if await img_loc.count() > 0:
                img = img_loc.first
                src = await img.get_attribute("src") or ""
                # Descartar placeholders base64 o URLs de 1px
                if src and not src.startswith("data:") and "gif" not in src and len(src) > 20:
                    imagen_url = src
                if not imagen_url:
                    imagen_url = await img.get_attribute("data-src") or ""
                if not imagen_url:
                    srcset = await img.get_attribute("srcset") or ""
                    if srcset:
                        # Tomar la primera URL del srcset
                        imagen_url = srcset.split()[0].rstrip(",")

            vistos.add(asin)
            productos.append(Producto(
                titulo=titulo[:120],
                asin=asin,
                precio_actual=precio_actual,
                precio_original=precio_original if precio_original > 0 else round(precio_actual / (1 - descuento / 100), 2),
                descuento_pct=descuento,
                imagen_url=imagen_url,
            ))

        except Exception:
            continue

    return productos


async def _extraer_precios_busqueda(card) -> tuple[float, float]:
    """Extrae precio actual y original de una card de búsqueda."""
    precio_actual = 0.0
    precio_original = 0.0
    try:
        # Precio actual: span.a-price (el primero no tachado)
        precios_loc = card.locator('span.a-price:not(.a-text-strike) span.a-offscreen')
        if await precios_loc.count() > 0:
            txt = await precios_loc.first.inner_text()
            precio_actual = float(re.sub(r'[^\d,]', '', txt).replace(',', '.'))

        # Precio original: Amazon usa data-a-strike="true" en span.a-text-price (antes a-text-strike).
        # Puede ser list price del fabricante; el sanity check de ratio ×10 filtra outliers extremos.
        original_loc = card.locator('span.a-text-price span.a-offscreen')
        if await original_loc.count() > 0:
            txt = await original_loc.first.inner_text()
            precio_original = float(re.sub(r'[^\d,]', '', txt).replace(',', '.'))

        # Sanity check 1: ratio extremo (precio por kg/litro, ej. precio_original=500€ para espresso 10€)
        if precio_actual > 0 and precio_original > precio_actual * 10:
            precio_original = 0.0

        # Sanity check 2: verificar que precio_original no coincide con un precio-por-unidad
        # en el texto de la card. Amazon lo muestra como "X€ / l", "X€ / kg", "X€/100 ml",
        # "X€ / unidad"… — la CANTIDAD es opcional ("/ l" sin número), por eso la regex la hace
        # opcional (antes exigía dígito y dejaba pasar "101,88€ / l" → descuento falso del 75%).
        # Tolerancia RELATIVA (1%) para absorber el redondeo (101,88 vs 101,96).
        if precio_original > 0:
            try:
                card_text = await card.inner_text()
                _upm = re.compile(
                    r'(\d+[.,]\d+)\s*€\s*/\s*(?:\d+(?:[.,]\d+)?\s*)?'
                    r'(?:ml|cl|l|g|kg|mg|und?|unidad|metro|m|pieza|lavado|c[aá]psulas?|caps?)\b',
                    re.IGNORECASE,
                )
                for m in _upm.finditer(card_text):
                    uval = float(m.group(1).replace(',', '.'))
                    if uval > 0 and abs(uval - precio_original) / precio_original < 0.01:
                        precio_original = 0.0
                        break
            except Exception:
                pass

    except Exception:
        pass
    return precio_actual, precio_original


async def _buscar_precio_amazon_mas_barato(
    titulo: str, precio_no_amazon: float, browser: BrowserContext
) -> dict | None:
    """
    Busca el producto en Amazon.es sin el filtro URL ≥40%.
    Usa el número de modelo como ancla (ej: WH-CH520, QC45) para evitar falsos positivos.
    Devuelve datos de Amazon si encuentra el modelo, incluso cuando Amazon no es más barato
    (campo 'es_mas_barato' indica si conviene sustituir). Solo omite cuando Amazon es
    claramente más caro (>15% sobre el precio de la oferta de origen).
    Retorna None si no se encuentra el modelo o Amazon es claramente más caro.
    """
    modelo_m = _MODELO_RE.search(titulo.upper())
    if not modelo_m:
        return None  # Sin número de modelo claro → riesgo alto de falso positivo

    modelo = modelo_m.group(1).upper()
    query  = urllib.parse.quote(modelo)
    url    = f"https://www.amazon.es/s?k={query}&i=electronics"

    page = await browser.new_page()
    try:
        await page.goto(url, timeout=20000, wait_until="domcontentloaded")
        cards = await page.locator('[data-component-type="s-search-result"][data-asin]').all()

        for card in cards[:5]:
            try:
                asin = await card.get_attribute("data-asin") or ""
                if not asin or len(asin) != 10:
                    continue

                # Verificar que el resultado de Amazon contiene el mismo número de modelo
                titulo_res = ""
                for sel in ["h2 span", "h2 a span", ".a-text-normal"]:
                    loc = card.locator(sel)
                    if await loc.count() > 0:
                        titulo_res = (await loc.first.inner_text()).strip()
                        if titulo_res:
                            break
                if not titulo_res:
                    continue

                modelo_en_res = _MODELO_RE.search(titulo_res.upper())
                if not modelo_en_res or modelo_en_res.group(1).upper() != modelo:
                    continue  # Modelo diferente → falso positivo

                # Guard anti-colisión de SKU: además del modelo, los títulos deben
                # compartir ≥1 palabra significativa. Sin esto, dos productos sin
                # relación que comparten un código (ej. "H-6706" en un botín y en unas
                # fundas de cojín) se emparejarían y el deal acabaría con título de uno
                # y URL/precio del otro.
                if not _titulos_comparten_termino(titulo, titulo_res):
                    continue

                precio_actual, precio_original = await _extraer_precios_busqueda(card)
                if precio_actual <= 0:
                    continue

                # Registrar precio real de Amazon (sin filtro de descuento = precio de mercado).
                _registrar_precio_amazon(asin, precio_actual, precio_original)

                # Solo omitir si Amazon es claramente más caro (>15%)
                if precio_actual > precio_no_amazon * 1.15:
                    continue

                imagen_url = ""
                img_loc = card.locator("img.s-image")
                if await img_loc.count() > 0:
                    src = await img_loc.first.get_attribute("src") or ""
                    if src and not src.startswith("data:"):
                        imagen_url = src

                return {
                    "asin":                   asin,
                    "precio_actual":          precio_actual,
                    "precio_original_amazon": precio_original,
                    "imagen_url":             imagen_url,
                    "es_mas_barato":          precio_actual < precio_no_amazon,
                }
            except Exception:
                continue
    except Exception as e:
        print(f"   ⚠️  Amazon price-check error: {e}")
    finally:
        await page.close()

    return None


async def _extraer_de_deals(page: Page, vistos: set) -> list[Producto]:
    """
    Extrae productos de la página /deals de Amazon.
    Más frágil (JS-heavy), actúa como fuente extra.
    """
    productos = []

    # Intentar primero con data-asin (si el widget los tiene)
    cards_asin = await page.locator('[data-asin]').all()
    print(f"   📦 Elementos con data-asin: {len(cards_asin)}")

    for card in cards_asin:
        try:
            asin = await card.get_attribute("data-asin") or ""
            if not asin or asin in vistos or len(asin) != 10:
                continue

            texto = await card.inner_text()
            match_desc = re.search(r'(\d+)\s*%', texto)
            if not match_desc:
                continue
            descuento = int(match_desc.group(1))

            # >90% nunca es real en Amazon — precio por unidad, recambio o error
            if descuento > 90:
                continue

            # Eliminar precios por unidad de medida (ej. "52,33€/100 ml", "3,20€/100 g")
            # antes de extraer precios — Amazon los muestra junto al precio real en el card text
            # y el regex los recogería como precio_original generando descuentos falsos.
            _UPM_RE = re.compile(
                r'\d+[.,]\d+\s*€\s*/\s*(?:\d+(?:[.,]\d+)?\s*)?'
                r'(?:ml|cl|l|g|kg|mg|und?|unidad|metro|m|pieza|lavado|c[aá]psulas?|caps?)\b',
                re.IGNORECASE,
            )
            texto_p = _UPM_RE.sub('', texto)
            precios = re.findall(r'(\d+[.,]\d{2})\s*€', texto_p)
            if not precios:
                continue
            precio_actual = float(precios[0].replace(',', '.'))
            if not _precio_aceptable(precio_actual, descuento):
                continue
            precio_badge = round(precio_actual / (1 - descuento / 100), 2)
            if len(precios) > 1:
                precio_ext = float(precios[1].replace(',', '.'))
                # Si el segundo precio excede 1.5× el badge → es MSRP, no was-price real
                precio_original = precio_ext if precio_ext <= precio_badge * 1.5 else precio_badge
            else:
                precio_original = precio_badge

            # Título desde primer texto largo del elemento
            titulo = next(
                (t.strip() for t in texto.split('\n') if len(t.strip()) > 20),
                ""
            )
            if not titulo or not _es_producto_valido(titulo, descuento, precio=precio_actual):
                continue

            vistos.add(asin)
            productos.append(Producto(
                titulo=titulo[:120],
                asin=asin,
                precio_actual=precio_actual,
                precio_original=precio_original,
                descuento_pct=descuento,
            ))
        except Exception:
            continue

    return productos


# Tallas de ropa (S/M/L/XL/XXL con palabra "Talla" o standalone)
# Solo letra — tallas numéricas (42, 43) son de calzado y se permiten
_TALLA_RE = re.compile(
    r'\bTalla\s+(?:XS|XXS|XXXL|XXL|XL|[SML])\b'
    r'|\bsize[:\s]+(?:XS|XXS|XXXL|XXL|XL|[SML])\b',
    re.IGNORECASE
)

# Detección de ROPA (prendas de vestir) — para excluir de Threads.
# Incluye términos en inglés (Barrabés/outdoor) y prendas íntimas/técnicas.
# No incluye calzado (zapatillas/botas) ni accesorios (mochilas, gafas, relojes).
_ROPA_RE = re.compile(
    r'\b('
    # Español
    r'camiseta|camisas?|polo|polos|sudadera|sudaderas|jersey|jers[eé]is?|'
    r'chaquetas?|cazadoras?|abrigos?|parkas?|anorak|plum[ií]feros?|chalecos?|'
    r'americanas?|blazer|pantal[oó]n|pantalones|vaqueros?|jeans?|bermudas?|'
    r'faldas?|vestidos?|leggings?|mallas?|maillot|culotte|chandal|ch[aá]ndal|jogger|'
    r'calcet[ií]n|calcetines|bragas?|b[oó]xer|calzoncillos?|sujetadores?|'
    r'bikinis?|ba[ñn]adores?|pijamas?|bufandas?|guantes?|blusas?|cardigans?|'
    r't[uú]nicas?|kimono|peto|gorros?|forro\s+polar|forros\s+polares|'
    # Inglés (tiendas outdoor / técnicas)
    r'shorts?|shirts?|t-?shirt|tee|jackets?|hoody|hoodie|sweater|pants?|tights?|'
    r'legging|bra|panty|panties|briefs?|underwear|base\s?layer|baselayer|'
    r'top|vest|socks?|beanie|hat|suw|longsleeve|long\s?sleeve|tank'
    r')\b',
    re.IGNORECASE
)

# Número de modelo — ancla para el check cross-tienda de mejor precio Amazon.
# Captura patrones tipo: WH-CH520, WH-1000XM5, QC45, RTX-4080, MX300, DS-4, K380
_MODELO_RE = re.compile(
    r'\b([A-Z]{2,5}-[A-Z]{0,3}[0-9]{2,5}[A-Z0-9]{0,5}'   # WH-CH520, WH-1000XM5
    r'|[A-Z]{2,5}[0-9]{3,5}[A-Z0-9]{0,3})\b'              # QC45, WF1000XM5, RTX4080
)
# Nota: el prefijo antes del guion exige ≥2 letras a propósito. SKUs genéricos tipo
# "H-6706" (1 letra + dígitos) NO son modelos reales y colisionan entre productos sin
# relación (p.ej. un botín y unas fundas de cojín que comparten ese código de vendedor),
# provocando que el cross-check de Amazon empareje productos distintos.

# Tiendas de electrónica/tech donde tiene sentido el cross-check contra Amazon
# (búsqueda en i=electronics anclada en número de modelo real). Para moda, calzado,
# deportes, bicis o juguetes, ese cross-check es ruido y provoca falsos emparejamientos
# por códigos SKU coincidentes → NO se aplica.
_CROSSCHECK_AMAZON_TIENDAS = frozenset({"MediaMarkt", "PCBox", "PcComponentes", "Beep", "Carrefour"})

# Marketplaces: el vendedor fija el precio Y el "PVP", así que NUESTRO histórico no
# vale como referencia. Ejemplo real: un Logitech MX Master 2S llevaba dos semanas
# listado en Carrefour a 303-343€ y bajó a 179€ → detectamos un −41% correcto contra
# su propio histórico… siendo 179€ un 41% MÁS CARO que los 127€ de Amazon.
# En estas tiendas la bajada propia NO basta: si Amazon no confirma el precio, no se
# publica. Es la única referencia externa de mercado que tenemos.
_TIENDAS_MARKETPLACE = frozenset({"Carrefour"})

# Stopwords para comparar títulos en el cross-check Amazon (colores, género, conectores).
# No deben contar como "término compartido" porque son demasiado genéricos.
_TITULO_STOP = frozenset([
    "para", "mujer", "hombre", "unisex", "niño", "niños", "niña", "niñas",
    "con", "sin", "del", "las", "los", "para", "the", "and", "color",
    "talla", "tallas", "negro", "negra", "blanco", "blanca", "azul", "rojo",
    "roja", "verde", "gris", "rosa", "marron", "marrón", "beige", "plata",
    "dorado", "dorada", "plateado", "casual", "regalo", "juego", "pack",
])

def _titulos_comparten_termino(t1: str, t2: str) -> bool:
    """True si dos títulos comparten ≥1 palabra significativa (≥4 letras, no stopword).

    Guard del cross-check Amazon: aunque dos productos compartan un código tipo modelo
    (SKU coincidente), si no comparten ninguna palabra real del nombre son productos
    distintos y NO deben emparejarse (evita el bug botín↔fundas de cojín por "H-6706").
    """
    def toks(t: str) -> set:
        return {w for w in re.findall(r'[a-záéíóúñü]{4,}', t.lower()) if w not in _TITULO_STOP}
    return bool(toks(t1) & toks(t2))


# Prendas de ropa/moda: se permiten solo si hay marca conocida + descuento ≥50%
_PALABRAS_ROPA = frozenset([
    "camiseta", "camisetas", "camisa", "camisas",
    "polo", "polos", "jersey", "jerseys",
    "sudadera", "sudaderas", "hoodie",
    "chaqueta", "chaquetas", "abrigo", "abrigos", "anorak",
    "vestido", "vestidos", "falda", "faldas", "blusa", "blusas",
])

# Marcas con valor real en ropa/moda (usadas solo en el filtro de ropa)
_MARCAS_ROPA = frozenset([
    "nike", "adidas", "jordan", "new balance", "asics", "puma", "reebok",
    "north face", "columbia", "patagonia", "helly hansen", "timberland",
    "lacoste", "ralph lauren", "tommy", "calvin klein", "armani",
    "stone island", "burberry", "levi", "salomon", "gore",
    "castelli", "sportful", "rapha", "poc", "oakley",
    # Outdoor / montaña / escalada (Barrabes, Mammoth)
    "mammut", "black diamond", "mountain equipment", "arc'teryx", "arcteryx",
    "rab", "millet", "haglofs", "haglöfs", "fjallraven", "fjällräven",
    "scarpa", "salewa", "la sportiva", "ternua", "trangoworld",
    "norrona", "norrøna", "icebreaker", "sherpa", "compressport",
    "dynafit", "ortovox", "montura", "karpos",
    # Marcas premium Esdemarca (camisas, polos, chaquetas, abrigos)
    "boss", "barbour", "hackett", "fred perry", "ba&sh", "ba & sh",
    "rotate", "max mara", "weekend max mara", "michael kors",
    "c.p. company", "cp company", "guess", "hoff",
    # Outerwear / moda premium (ECI, Esdemarca) y ropa surf/skate
    "moncler", "canada goose", "napapijri", "belstaff", "parajumpers", "woolrich",
    "jack wolfskin", "k-way", "superdry", "g-star", "pepe jeans", "scalpers",
    "bimba y lola", "purificacion garcia", "adolfo dominguez", "hugo boss",
    "quiksilver", "dc shoes", "billabong", "rip curl", "o'neill", "oneill", "volcom", "hurley",
])


# Precio mínimo por categoría de alto valor.
# Un recambio/accesorio de cafetera puede costar €7, pero una cafetera real nunca.
# Los ASINs hijo (accesorios) a veces heredan el título del producto padre en Amazon.
_PRECIO_SUELO_CATEGORIA: list[tuple[list[str], float]] = [
    (["cafetera express", "cafetera espresso", "cafetera superautomática", "cafetera con molinillo",
      "cafetera de goteo", "cafetera de cápsulas", "cafetera nespresso",
      "nespresso vertuo", "nespresso original", "dolce gusto",
      "máquina de café", "máquina espresso"], 40.0),
    (["televisor", "smart tv", "qled", "oled tv", "miniled"], 120.0),
    (["frigorífico", "lavadora", "lavavajillas", "horno eléctrico",
      "campana extractora", "vitrocerámica", "placa de inducción"], 150.0),
    (["ordenador portátil", "laptop", "notebook", "macbook"], 180.0),
    (["robot de cocina", "thermomix", "monsieur cuisine", "magimix"], 80.0),
    (["robot aspirador", "aspiradora robot", "roomba"], 60.0),
]


def _precio_valido_para_categoria(titulo: str, precio: float) -> bool:
    """Devuelve False si el precio es demasiado bajo para el tipo de producto detectado en el título."""
    t = titulo.lower()
    for keywords, precio_min in _PRECIO_SUELO_CATEGORIA:
        if any(kw in t for kw in keywords) and precio < precio_min:
            return False
    return True


_MIN_DIAS_HISTORIAL_AMAZON = 3   # días mínimos para usar historial propio


def _registrar_precio_amazon(asin: str, precio: float, precio_original: float) -> None:
    """Registra una observación diaria de precios Amazon en price_history.
    Un registro por ASIN por día (INSERT OR IGNORE — conserva la primera del día).
    Se llama para TODOS los productos vistos en Amazon, no solo los que pasan filtros.
    """
    if not asin or precio <= 0:
        return
    try:
        fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT OR IGNORE INTO price_history (asin, tienda, precio, precio_original, fecha) "
                "VALUES (?, 'Amazon', ?, ?, ?)",
                (asin, precio, precio_original or 0.0, fecha),
            )
    except Exception:
        pass


def _calcular_historial_amazon(asin: str, dias: int = 30) -> tuple[float, float, int]:
    """
    Estadísticas del historial propio de precios Amazon para un ASIN.
    Devuelve (min_precio_original, avg_precio_original, n_dias).

    Usamos precio_original (el "was price" de Amazon) como referencia real —
    no el precio descontado, que siempre es bajo porque nuestras URLs filtran ≥40%.
    El avg/min de precio_original a lo largo de días es el precio normal del producto.
    """
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                """SELECT MIN(precio_original), AVG(precio_original), COUNT(DISTINCT fecha)
                   FROM price_history
                   WHERE asin = ? AND tienda = 'Amazon'
                     AND fecha >= ? AND precio_original > 0""",
                (asin, desde),
            ).fetchone()
        if row and row[2] and row[2] >= _MIN_DIAS_HISTORIAL_AMAZON:
            return round(row[0], 2), round(row[1], 2), int(row[2])
    except Exception:
        pass
    return 0.0, 0.0, 0


def _registrar_observacion_precio(d: dict) -> None:
    """Guarda una observación de precio pre-filtro en price_history (una por día y producto).

    Al registrar ANTES de aplicar filtros, acumulamos el precio real de mercado del
    retailer independientemente de si el deal se publica. Tras varias semanas podemos
    comparar precio_original del feed contra el precio_actual histórico para detectar
    referencias MSRP infladas.
    """
    try:
        precio_act = d.get("precio_actual", 0)
        if not precio_act:
            return
        fecha_hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        asin_key  = d.get("asin") or (d.get("titulo", "")[:40].lower())
        tienda    = d.get("tienda", "")
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """INSERT OR IGNORE INTO price_history
                       (asin, tienda, precio, precio_original, fecha)
                   VALUES (?, ?, ?, ?, ?)""",
                (asin_key, tienda, precio_act, d.get("precio_original", 0), fecha_hoy),
            )
    except Exception:
        pass


def _registrar_observaciones_batch(items: list[dict]) -> None:
    """Registra muchas observaciones de precio en UNA sola conexión (feeds grandes de
    solo-historial: apparel puede traer decenas de miles). Una fila por producto/tienda/día."""
    if not items:
        return
    fecha_hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filas = []
    for d in items:
        precio_act = d.get("precio_actual", 0)
        if not precio_act:
            continue
        asin_key = d.get("asin") or (d.get("titulo", "")[:40].lower())
        filas.append((asin_key, d.get("tienda", ""), precio_act, d.get("precio_original", 0), fecha_hoy))
    if not filas:
        return
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.executemany(
                """INSERT OR IGNORE INTO price_history
                       (asin, tienda, precio, precio_original, fecha)
                   VALUES (?, ?, ?, ?, ?)""",
                filas,
            )
    except Exception as e:
        print(f"   ⚠️  batch price_history error: {e}")


def _es_producto_valido(titulo: str, descuento_pct: int = 0, tienda: str = "", precio: float = 0.0) -> bool:
    titulo = (titulo or "").strip()
    # Filtro de longitud: títulos demasiado cortos suelen ser sólo marca o imagen rota
    # (ej. "Cacharel" como single token → referencia inflada; "picture" → scrape roto)
    if len(titulo) < 10 or len(titulo.split()) < 2:
        return False
    t = titulo.lower()
    # Tiendas outdoor/especializadas: pantalones técnicos, mallas y leggings son
    # ropa técnica de montaña, no ropa básica genérica — se eximen del bloqueo global.
    _OUTDOOR_EXEMPT = frozenset({"pantalón", "pantalones", "mallas", "malla", "leggings", "leggins"})
    _prohibidas = (
        PALABRAS_PROHIBIDAS
        if tienda not in {"Barrabes", "Decathlon"}
        else [p for p in PALABRAS_PROHIBIDAS if p not in _OUTDOOR_EXEMPT]
    )
    # Dermocosmética premium (La Roche-Posay, ISDIN…), droguería de marca (Pantene,
    # Garnier, Nivea…) o tienda de cosmética verificada (OneBioShop): se eximen del bloqueo
    # de cosmética genérica (champú de marca sí, champú sin marca no) — la marca valida calidad.
    if tienda == "OneBioShop" or any(m in t for m in _MARCAS_DERMO) or any(m in t for m in _MARCAS_DROGUERIA):
        _prohibidas = [p for p in _prohibidas if p not in _PALABRAS_COSMETICA]
    if any(p in t for p in _prohibidas):
        return False
    if _TALLA_RE.search(titulo):
        return False
    # Maillot y culotte: ropa ciclismo — solo con descuento > 60%
    if re.search(r'\b(maillot|culott?e)s?\b', t) and descuento_pct <= 60:
        return False
    # Ropa de moda/deporte: solo si marca conocida + descuento real ≥50%
    # Excepción: Barrabés (outdoor técnico), Decathlon (deporte), Esdemarca y Desigual
    # (tiendas de moda de marca con descuentos reales de outlet — su catálogo ES ropa).
    if tienda not in ("Barrabes", "Decathlon", "Esdemarca", "Desigual", "Zalando", "Deporte Outlet", "Adidas", "Bikila") and any(r in t for r in _PALABRAS_ROPA):
        if descuento_pct < 50 or not any(m in t for m in _MARCAS_ROPA):
            return False
    # Cecotec: marca de gama baja con precios de referencia inflados — solo descuentos fuertes
    if "cecotec" in t and descuento_pct < 60:
        return False
    # Descuentos imposibles (≥90%) casi siempre indican error de dato en el feed
    if descuento_pct >= 90:
        return False
    # Precio demasiado bajo para la categoría detectada → recambio disfrazado de producto completo
    if precio > 0 and not _precio_valido_para_categoria(titulo, precio):
        return False
    return True


# Umbral LC por tienda para items < 25€. El default (40%) aplica a MediaMarkt, ToysRus, Amazon…
# Esdemarca sube a 60% (ropa de temporada con precio hinchado); Decathlon a 50% (deporte técnico).
_LC_DESCUENTO_MIN_POR_TIENDA: dict[str, int] = {
    "Esdemarca": 60,
    "Decathlon": 50,
}


def _precio_aceptable(precio_actual: float, descuento: int, tienda: str = "", titulo: str = "") -> bool:
    """Devuelve True si pasa el filtro estándar O el filtro low-cost (umbral LC por tienda).

    El umbral estándar baja a 30% para gran electrodoméstico caro (ver _descuento_minimo_para).
    """
    if precio_actual >= PRECIO_MINIMO and descuento >= _descuento_minimo_para(titulo, precio_actual):
        return True
    lc_min = _LC_DESCUENTO_MIN_POR_TIENDA.get(tienda, DESCUENTO_LC_MINIMO)
    if PRECIO_MINIMO_LC <= precio_actual < PRECIO_MINIMO and descuento >= lc_min:
        return True
    return False


async def _aceptar_cookies(page: Page):
    selectores = [
        '#sp-cc-accept',
        'input[id="sp-cc-accept"]',
        'button:has-text("Aceptar")',
        '#onetrust-accept-btn-handler',
        'button[data-cel-widget*="accept"]',
    ]
    for s in selectores:
        try:
            if await page.locator(s).is_visible(timeout=1500):
                await page.click(s, timeout=2000)
                await asyncio.sleep(0.5)
                break
        except Exception:
            pass

# ════════════════════════════════════════════════════════════════
# SCRAPERS ADICIONALES — PcComponentes, Mammoth Bikes, Barrabes
# ════════════════════════════════════════════════════════════════

async def scrape_pccomponentes(context: BrowserContext) -> list[Producto]:
    """
    Scrape de PcComponentes.com — ofertas especiales ordenadas por descuento.
    PcComponentes usa React SPA: esperar networkidle antes de evaluar el DOM.
    """
    print(f"\n📡 PcComponentes: {len(PCCOMPONENTES_URLS)} URLs")
    page = await context.new_page()
    productos: list[Producto] = []
    hrefs_vistos: set[str] = set()
    try:
        for url in PCCOMPONENTES_URLS:
            try:
                ok = await _cargar_con_reintento(page, url, "PcComponentes")
                if not ok:
                    continue

                # PcComponentes usa React SPA: esperar a que las llamadas AJAX carguen
                # los productos antes de evaluar el DOM
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass  # Timeout aceptable — intentar evaluar igualmente

                await asyncio.sleep(random.uniform(2, 4))
                await _scroll_pagina(page, veces=5)
                await asyncio.sleep(1.5)

                if DEBUG_SCREENSHOTS:
                    await page.screenshot(path="debug_pccomponentes.png")

                items = await page.evaluate(r"""
                    () => {
                        const BASE = 'https://www.pccomponentes.com';
                        const resultados = [];
                        const vistos = new Set();

                        // PcComponentes usa slugs: /brand-model-spec/CODE o /brand-model/CODE
                        // Filtro: slug con ≥2 guiones (productos) y sin palabras de navegación
                        const NAV = /\/(campanas|marca|blog|categoria|ayuda|contacto|news|cart|account)/i;
                        document.querySelectorAll('a[href]').forEach(link => {
                            const rawHref = link.getAttribute('href') || '';
                            const path = rawHref.replace(/^https?:\/\/www\.pccomponentes\.com/, '');
                            // Slug de producto: empieza con /, tiene ≥4 guiones, no es nav
                            if (!path.startsWith('/') || (path.match(/-/g) || []).length < 2) return;
                            if (NAV.test(path)) return;
                            const href = rawHref.startsWith('http') ? rawHref : BASE + rawHref;
                            if (vistos.has(href)) return;
                            vistos.add(href);

                            // En PcComponentes el <a> ES la card: precios e imagen son hijos del link.
                            // Comprobar el propio link primero; solo subir si no tiene € (compatibilidad otros scrapers).
                            let el = link;
                            let txt = el.innerText || '';
                            if (!txt.includes('€') || txt.length >= 800) {
                                txt = '';
                                for (let i = 0; i < 8; i++) {
                                    el = el.parentElement;
                                    if (!el) break;
                                    txt = el.innerText || '';
                                    if (txt.includes('€') && txt.length < 800) break;
                                    txt = '';
                                }
                            }
                            if (!txt.includes('€')) return;

                            // Título: preferir <h3> dentro del link para no coger el badge de descuento
                            const h3 = link.querySelector('h3');
                            const title = (h3 ? h3.innerText.trim() : null)
                                || link.getAttribute('title')
                                || link.getAttribute('aria-label')
                                || '';
                            if (!title) return;

                            const img = link.querySelector('img[src], img[data-src]');
                            const imagen = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
                            resultados.push({ href, title: title.trim(), text: txt, imagen });
                        });
                        return resultados;
                    }
                """)

                if len(items) == 0:
                    titulo_pag = await page.title()
                    print(f"   📦 0 productos en {url.split('/')[-1]} (título pág: '{titulo_pag[:80]}')")
                else:
                    print(f"   📦 {len(items)} productos en {url.split('/')[-1]}")

                for item in items:
                    try:
                        href = item.get("href", "")
                        if href in hrefs_vistos:
                            continue
                        hrefs_vistos.add(href)
                        titulo = (item.get("title") or "").strip()
                        texto  = item.get("text", "")

                        if not titulo or len(titulo) < 8 or not _es_producto_valido(titulo):
                            continue

                        precios = re.findall(r'(\d+[.,]\d{2})\s*€', texto)
                        if not precios:
                            continue

                        nums = [float(p.replace(',', '.')) for p in precios]
                        precio_actual   = min(nums)
                        precio_original = max(nums) if len(nums) > 1 else 0.0

                        m_desc = re.search(r'(\d+)\s*%', texto)
                        descuento = int(m_desc.group(1)) if m_desc else (
                            round((1 - precio_actual / precio_original) * 100)
                            if precio_original > precio_actual > 0 else 0
                        )

                        _registrar_observacion_precio({
                            "asin": href, "tienda": "PcComponentes",
                            "precio_actual": precio_actual, "precio_original": precio_original,
                        })

                        if not _precio_aceptable(precio_actual, descuento):
                            continue

                        productos.append(Producto(
                            titulo=titulo[:120],
                            asin=href,
                            precio_actual=precio_actual,
                            precio_original=precio_original if precio_original > 0 else round(precio_actual / (1 - descuento / 100), 2),
                            descuento_pct=descuento,
                            tienda="PcComponentes",
                            imagen_url=item.get("imagen", ""),
                        ))
                    except Exception:
                        continue

                # Delay anti-rate-limit entre URLs: Cloudflare bloquea ráfagas cortas
                # aunque cf_clearance sea válido. 40-70s imita tiempo de lectura humana.
                if url != PCCOMPONENTES_URLS[-1]:
                    espera = random.uniform(40, 70)
                    print(f"   ⏳ [PcComponentes] Espera anti-rate-limit {espera:.0f}s...")
                    await asyncio.sleep(espera)

            except Exception as e:
                print(f"   ⚠️ Error en {url}: {e}")
                continue

        print(f"   ✅ {len(productos)} ofertas de PcComponentes ({len(PCCOMPONENTES_URLS)} URLs)")
    except Exception as e:
        print(f"   ❌ Error PcComponentes: {e}")
    finally:
        await page.close()
    return productos




def _parse_precio_es(texto_precio: str) -> float:
    """Convierte '2.899,00' o '229,95' → float (formato español con separador de miles)."""
    return float(texto_precio.replace(".", "").replace(",", "."))


async def scrape_mammoth(context: BrowserContext) -> list[Producto]:
    """
    Scraper para Mammoth Bikes outlet.
    El sitio renderiza HTML server-side con cards .card--product.
    Ancla en links /p-XXXXXX (patrón estable de producto).
    Maneja precios en formato español (2.899,00 €) con separador de miles.
    """
    print(f"\n📡 Mammoth Bikes: {len(MAMMOTH_URLS)} categorías de outlet")
    page = await context.new_page()
    productos: list[Producto] = []
    hrefs_vistos: set[str] = set()

    try:
        for url in MAMMOTH_URLS:
            try:
                ok = await _cargar_con_reintento(page, url, "Mammoth")
                if not ok:
                    continue

                await asyncio.sleep(random.uniform(2, 3))

                # Clic en "Cargar más" hasta agotarlo (máx 15 rondas)
                for _ in range(15):
                    await _scroll_pagina(page, veces=3)
                    await asyncio.sleep(1.2)
                    try:
                        btn = page.locator(
                            'button:has-text("Cargar más"), '
                            'a:has-text("Cargar más"), '
                            '[class*="load-more"]:visible, '
                            '[class*="loadMore"]:visible, '
                            '[class*="ver-mas"]:visible'
                        )
                        if await btn.count() > 0 and await btn.first.is_visible(timeout=1000):
                            await btn.first.click()
                            await asyncio.sleep(2.5)
                        else:
                            break
                    except Exception:
                        break

                items = await page.evaluate("""
                    () => {
                        const BASE = 'https://www.mammothbikes.com';
                        const resultados = [];
                        const vistos = new Set();

                        document.querySelectorAll('a[href*="/p-"]').forEach(link => {
                            const rawHref = link.getAttribute('href') || '';
                            if (!rawHref.match(/\\/p-\\d+$/)) return;
                            const href = rawHref.startsWith('http') ? rawHref : BASE + rawHref;
                            if (vistos.has(href)) return;
                            vistos.add(href);

                            // Subir hasta encontrar el contenedor de la card con precios
                            let el = link;
                            for (let i = 0; i < 10; i++) {
                                el = el.parentElement;
                                if (!el) break;
                                const txt = el.innerText || '';
                                if (txt.includes('€') && txt.length < 1200) {
                                    // Título: atributo title del link, o primer texto largo
                                    let titulo = link.getAttribute('title')
                                        || link.getAttribute('aria-label')
                                        || '';
                                    if (!titulo) {
                                        const lines = (link.innerText || '').split('\\n')
                                            .map(l => l.trim()).filter(l => l.length > 8);
                                        titulo = lines[0] || '';
                                    }
                                    // Imagen: preferir data-src (lazy) sobre src (placeholder)
                                    const img = el.querySelector('img');
                                    let imagen = '';
                                    if (img) {
                                        imagen = img.getAttribute('data-src')
                                            || img.getAttribute('src') || '';
                                        if (imagen && !imagen.startsWith('http'))
                                            imagen = 'https:' + imagen;
                                    }
                                    if (titulo)
                                        resultados.push({ href, titulo: titulo.trim(), imagen, txt });
                                    break;
                                }
                            }
                        });
                        return resultados;
                    }
                """)

                categoria = url.rstrip("/").split("/")[-2]
                print(f"   📦 {len(items)} productos en {categoria}")

                for item in items:
                    try:
                        href = item.get("href", "")
                        if href in hrefs_vistos:
                            continue
                        hrefs_vistos.add(href)

                        titulo = (item.get("titulo") or "").strip()
                        txt    = item.get("txt", "")

                        if not titulo or len(titulo) < 8:
                            continue

                        # Descuento explícito en la card (más fiable que calcular)
                        m_desc = re.search(r'-\s*(\d+)\s*%', txt)
                        descuento = int(m_desc.group(1)) if m_desc else 0

                        # Precios en formato español: "229,95 €" o "2.899,00 €"
                        precios_raw = re.findall(r'(\d[\d.]*,\d{2})\s*€', txt)
                        if not precios_raw:
                            continue
                        nums = sorted(set(_parse_precio_es(p) for p in precios_raw))

                        if len(nums) >= 2:
                            precio_actual   = nums[0]
                            precio_original = nums[-1]
                        elif descuento > 0:
                            precio_actual   = nums[0]
                            precio_original = round(precio_actual / (1 - descuento / 100), 2)
                        else:
                            continue

                        if descuento == 0 and precio_original > precio_actual > 0:
                            descuento = round((1 - precio_actual / precio_original) * 100)

                        _registrar_observacion_precio({
                            "asin": href, "tienda": "Mammoth Bikes",
                            "precio_actual": precio_actual, "precio_original": precio_original,
                        })

                        if descuento < DESCUENTO_MINIMO:
                            continue
                        if not (PRECIO_MINIMO <= precio_actual <= PRECIO_MAXIMO_BICI):
                            continue

                        # Filtros generales + específicos de Mammoth (ya tenemos descuento)
                        if not _es_producto_valido(titulo, descuento):
                            continue
                        if not _mammoth_es_valido(titulo, descuento):
                            continue

                        imagen = item.get("imagen", "")
                        if imagen and imagen.startswith("data:"):
                            imagen = ""

                        productos.append(Producto(
                            titulo=titulo[:120],
                            asin=href,
                            precio_actual=precio_actual,
                            precio_original=precio_original,
                            descuento_pct=descuento,
                            tienda="Mammoth Bikes",
                            imagen_url=imagen,
                        ))
                    except Exception:
                        continue

            except Exception as e:
                print(f"   ⚠️ Error en {url}: {e}")
                continue

        print(f"   ✅ {len(productos)} ofertas de Mammoth Bikes ({len(MAMMOTH_URLS)} categorías)")
    except Exception as e:
        print(f"   ❌ Error Mammoth Bikes: {e}")
    finally:
        await page.close()
    return productos


async def scrape_barrabes(context: BrowserContext) -> list[Producto]:
    """
    Scraper de Barrabes.com — outlet de outdoor/montaña/ski ordenado por % descuento.
    Barrabes usa HTML server-side (productos visibles sin JS pesado).
    Selectores: a[href*="/product-"], precios "29,90 €", descuento "-63%".
    Sin programa Awin confirmado → URLs directas hasta obtener BARRABES_AWIN_MID.
    """
    print(f"\n📡 Barrabes: {len(BARRABES_URLS)} URLs")
    productos: list[Producto] = []
    hrefs_vistos: set[str] = set()
    page = await context.new_page()

    try:
        for url in BARRABES_URLS:
            try:
                ok = await _cargar_con_reintento(page, url, "Barrabes")
                if not ok:
                    continue

                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)

                items = await page.evaluate("""
                    () => {
                        const resultados = [];
                        const vistos = new Set();
                        document.querySelectorAll('a[href*="/product-"]').forEach(link => {
                            const href = link.href;
                            if (!href || vistos.has(href)) return;
                            vistos.add(href);

                            let el = link;
                            for (let i = 0; i < 8; i++) {
                                el = el.parentElement;
                                if (!el) break;
                                const txt = el.innerText || '';
                                if (txt.includes('€') && txt.length < 700) {
                                    // Preferir alt de imagen (suele tener el nombre completo del producto)
                                    const img = el.querySelector('img[src*="cdn.barrabes"]')
                                        || el.querySelector('img[alt]')
                                        || el.querySelector('img');
                                    const altTitulo = img ? (img.getAttribute('alt') || '') : '';
                                    // Fallback: derivar del slug en la URL (p.ej. "the-north-face-chaqueta-resolve")
                                    const partes = href.split('/');
                                    const slug = partes.find(s => s.length > 12 && s.includes('-') && !s.startsWith('product-')) || '';
                                    const slugTitulo = slug.replace(/-/g, ' ').trim();
                                    const titulo = altTitulo || slugTitulo || link.innerText.trim().split('\\n')[0];
                                    const imagen = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
                                    resultados.push({ href, titulo: titulo.trim(), txt, imagen });
                                    break;
                                }
                            }
                        });
                        return resultados;
                    }
                """)

                print(f"   📦 {len(items)} productos en {url.split('/')[-2]}")

                for item in items:
                    try:
                        href = item.get("href", "")
                        if href in hrefs_vistos:
                            continue
                        hrefs_vistos.add(href)

                        titulo = (item.get("titulo") or "").strip()
                        txt    = item.get("txt", "")

                        if not titulo or len(titulo) < 8:
                            continue

                        # Descuento: formato Barrabes es "-63%"
                        m_desc = re.search(r'-\s*(\d+)\s*%', txt)
                        descuento = int(m_desc.group(1)) if m_desc else 0

                        # Precios en formato español: "29,90 €"
                        precios_raw = re.findall(r'(\d[\d.]*,\d{2})\s*€', txt)
                        if not precios_raw:
                            continue
                        nums = sorted(set(_parse_precio_es(p) for p in precios_raw))

                        if len(nums) >= 2:
                            precio_actual   = nums[0]
                            precio_original = nums[-1]
                        elif descuento > 0 and nums:
                            precio_actual   = nums[0]
                            precio_original = round(precio_actual / (1 - descuento / 100), 2)
                        else:
                            continue

                        if descuento == 0 and precio_original > precio_actual > 0:
                            descuento = round((1 - precio_actual / precio_original) * 100)

                        _registrar_observacion_precio({
                            "asin": href, "tienda": "Barrabes",
                            "precio_actual": precio_actual, "precio_original": precio_original,
                        })

                        if not _precio_aceptable(precio_actual, descuento):
                            continue
                        if not _es_producto_valido(titulo, descuento, tienda="Barrabes"):
                            continue

                        imagen = item.get("imagen", "")
                        if imagen and imagen.startswith("data:"):
                            imagen = ""

                        productos.append(Producto(
                            titulo=titulo[:120],
                            asin=href,
                            precio_actual=precio_actual,
                            precio_original=precio_original,
                            descuento_pct=descuento,
                            tienda="Barrabes",
                            imagen_url=imagen,
                        ))
                    except Exception:
                        continue

            except Exception as e:
                print(f"   ⚠️ Error en {url}: {e}")
                continue

        print(f"   ✅ {len(productos)} ofertas de Barrabes ({len(BARRABES_URLS)} URLs)")
    except Exception as e:
        print(f"   ❌ Error Barrabes: {e}")
    finally:
        await page.close()
    return productos


async def scrape_todas_las_tiendas(context: BrowserContext) -> list[Producto]:
    """Orquesta el scraping de todas las tiendas secuencialmente."""
    todos: list[Producto] = []
    vistos: set[str] = set()

    scrapers = [
        scrape_amazon_deals,
        scrape_pccomponentes,
        scrape_mammoth,
        scrape_barrabes,
    ]

    for scraper in scrapers:
        try:
            lote = await scraper(context)
            for p in lote:
                clave = f"{p.tienda}:{p.titulo[:40].lower()}"
                if clave not in vistos:
                    vistos.add(clave)
                    todos.append(p)
        except Exception as e:
            print(f"   ❌ Error en scraper {scraper.__name__}: {e}")
            alertar_admin(f"Error en scraper: {scraper.__name__}", str(e))
        await asyncio.sleep(3)

    # ── PSS: extraer productos directamente del newsletter (sin Playwright) ──
    try:
        pss_raw = await asyncio.to_thread(get_pss_productos)
        for d in pss_raw:
            _registrar_observacion_precio(d)
            if not _es_producto_valido(d["titulo"], d["descuento_pct"], precio=d.get("precio_actual", 0)):
                continue
            if not _precio_aceptable(d["precio_actual"], d["descuento_pct"]):
                continue
            p = Producto(**d)
            clave = f"PrivateSportShop:{p.titulo[:40].lower()}"
            if clave not in vistos:
                vistos.add(clave)
                todos.append(p)
    except Exception as e:
        print(f"   ❌ Error en scraper PSS: {e}")
        alertar_admin("Error en scraper PSS", str(e))

    # ── Tradedoubler feeds (MediaMarkt/ToysRus) — caché 23h ──────────────────
    try:
        td_raw = await asyncio.to_thread(
            fetch_tradedoubler_productos, DESCUENTO_MINIMO, PRECIO_MINIMO_LC, PRECIO_MAXIMO,
            _descuento_minimo_para,
        )
        for d in td_raw:
            _registrar_observacion_precio(d)
            # MediaMarkt y PCBox usan PreviousPrice = MSRP fabricante → si descuento >60% suele ser ficticio.
            # Esdemarca / Toni Pons / Desigual usan PreviousPrice/SalePrice real del retailer (outlet),
            # donde descuentos del 60-80% son habituales y legítimos — no aplicar el filtro 2.5×.
            _p_act = d.get("precio_actual", 0)
            _p_ori = d.get("precio_original", 0)
            _tienda = d.get("tienda", "")
            if _tienda in ("MediaMarkt", "PCBox") and _p_act > 0 and _p_ori > _p_act * 2.5:
                continue
            # OneBioShop (112k productos): solo marcas cosméticas reconocidas — bloquea
            # marcas básicas/sin relevancia y productos de otras categorías (p.ej. calzado).
            if _tienda == "OneBioShop" and not any(m in d["titulo"].lower() for m in _MARCAS_COSMETICA_OK):
                continue
            if not _es_producto_valido(d["titulo"], d["descuento_pct"], tienda=_tienda, precio=_p_act):
                continue
            if not _precio_aceptable(d["precio_actual"], d["descuento_pct"], tienda=_tienda, titulo=d["titulo"]):
                continue
            p = Producto(**d)
            clave = f"{p.tienda}:{p.titulo[:40].lower()}"
            if clave not in vistos:
                vistos.add(clave)
                todos.append(p)
    except Exception as e:
        print(f"   ❌ Error en Tradedoubler feeds: {e}")
        alertar_admin("Error en Tradedoubler feeds", str(e))

    # ── TD feeds SIN precio de referencia en el feed — HISTORIAL PROPIO ──────────
    # Braun, De'Longhi, Tefal, Suunto, L'Occitane, Beauty Corner, Eureka, DC Shoes,
    # Quiksilver, Roxy, Element: no traen precio "antes". Acumulamos price_history y, con
    # ≥7 días, detectamos BAJADAS REALES (≥40% bajo su máx sostenido) → se publican.
    try:
        hist_obs, hist_pub = await asyncio.to_thread(
            fetch_tradedoubler_historial, PRECIO_MINIMO_LC, PRECIO_MAXIMO, DB_PATH,
        )
        _registrar_observaciones_batch(hist_obs)
        for d in hist_pub:
            _tienda = d.get("tienda", "")
            if not _es_producto_valido(d["titulo"], d["descuento_pct"], tienda=_tienda, precio=d.get("precio_actual", 0)):
                continue
            if not _precio_aceptable(d["precio_actual"], d["descuento_pct"], tienda=_tienda, titulo=d["titulo"]):
                continue
            p = Producto(**d)
            clave = f"{p.tienda}:{p.titulo[:40].lower()}"
            if clave not in vistos:
                vistos.add(clave)
                todos.append(p)
        if hist_obs:
            print(f"   🗂️  TD historial: {len(hist_obs)} obs · {len(hist_pub)} bajadas reales detectadas")
    except Exception as e:
        print(f"   ❌ Error en TD historial: {e}")

    # ── Decathlon feed (historial de precios propio, caché 23h) ───────────────
    try:
        dec_raw = await asyncio.to_thread(fetch_decathlon_productos)
        # Rotación: el feed trae miles de deals ≥40%. Si cogemos siempre los 6 de
        # mayor descuento, se publican una vez y el dedup (TTL 96h) los bloquea →
        # Decathlon se queda "atascado" en esos 6 y nunca surfacea el resto del
        # catálogo. Barajando, cada ciclo entran productos distintos y rota todo.
        dec_pool = list(dec_raw)
        random.shuffle(dec_pool)
        dec_añadidos = 0
        for d in dec_pool:
            if dec_añadidos >= 8:
                break
            _registrar_observacion_precio(d)
            if not _es_producto_valido(d["titulo"], d["descuento_pct"], tienda="Decathlon", precio=d.get("precio_actual", 0)):
                continue
            if not _precio_aceptable(d["precio_actual"], d["descuento_pct"], tienda="Decathlon", titulo=d["titulo"]):
                continue
            p = Producto(**d)
            clave = f"Decathlon:{p.titulo[:40].lower()}"
            if clave not in vistos:
                vistos.add(clave)
                todos.append(p)
                dec_añadidos += 1
    except Exception as e:
        print(f"   ❌ Error en Decathlon feed: {e}")
        alertar_admin("Error en Decathlon feed", str(e))

    # ── ToysRus feed (historial de precios propio, caché 23h) ────────────────
    try:
        tr_raw = await asyncio.to_thread(fetch_toysrus_productos)
        for d in tr_raw:
            if not _es_producto_valido(d["titulo"], d["descuento_pct"], precio=d.get("precio_actual", 0)):
                continue
            if not _precio_aceptable(d["precio_actual"], d["descuento_pct"], tienda="ToysRus", titulo=d["titulo"]):
                continue
            p = Producto(**d)
            clave = f"ToysRus:{p.titulo[:40].lower()}"
            if clave not in vistos:
                vistos.add(clave)
                todos.append(p)
    except Exception as e:
        print(f"   ❌ Error en ToysRus feed: {e}")
        alertar_admin("Error en ToysRus feed", str(e))

    # ── AWIN feed (Padel Market publicable; ECI/Brico solo histórico) ─────────
    try:
        awin_raw = await asyncio.to_thread(
            fetch_awin_productos, DESCUENTO_MINIMO, PRECIO_MINIMO_LC, PRECIO_MAXIMO,
            DB_PATH, _descuento_minimo_para,
        )
        # Un feed a medias = tiendas enteras perdidas en silencio. Que avise.
        if awin_feed_mod.ultimo_fetch_truncado:
            alertar_admin(
                "Feed AWIN truncado — puede faltar una tienda entera",
                f"{awin_feed_mod.ultimo_fetch_truncado}. El feed va por comercios en serie: "
                f"lo que quede detrás del corte no se lee.",
            )
        # Comprueba que ninguna tienda haya dejado de registrar precios (ver
        # vigilar_frescura_feeds). Aquí porque el feed AWIN solo se refresca 1×/23h,
        # así que el chequeo queda naturalmente limitado a una vez al día.
        await asyncio.to_thread(vigilar_frescura_feeds, DB_PATH)
        # …y que ningún deal vivo siga anunciando un "antes" que ya caducó: el histórico
        # se mueve, y un -50% de hace tres semanas puede ser hoy el precio normal.
        # Solo cuando el feed se ha refrescado de verdad: recorrer el histórico entero
        # es caro y volver de caché no aporta datos nuevos que juzgar.
        if not awin_feed_mod.ultimo_fetch_cacheado:
            _caducados = await asyncio.to_thread(
                revalidar_publicados, DB_PATH, sorted(awin_feed_mod._SOLO_HISTORICO))
            if _caducados:
                print(f"   ⌛ {_caducados} deal(s) retirados: su descuento ya no se sostiene")
        # Barajar + tope por tienda: las publicables (Padel, Adidas) pueden traer
        # miles de deals; sin rotación se publicarían siempre los mismos y podrían
        # inundar el canal. Con shuffle + cap entran variados y acotados por ciclo.
        random.shuffle(awin_raw)
        _awin_por_tienda: dict[str, int] = {}
        _AWIN_CAP = 12
        for d in awin_raw:
            _registrar_observacion_precio(d)
            if _awin_por_tienda.get(d["tienda"], 0) >= _AWIN_CAP:
                continue
            if not _es_producto_valido(d["titulo"], d["descuento_pct"], tienda=d["tienda"], precio=d.get("precio_actual", 0)):
                continue
            if not _precio_aceptable(d["precio_actual"], d["descuento_pct"], tienda=d["tienda"], titulo=d["titulo"]):
                continue
            p = Producto(**d)
            clave = f"{p.tienda}:{p.titulo[:40].lower()}"
            if clave not in vistos:
                vistos.add(clave)
                todos.append(p)
                _awin_por_tienda[d["tienda"]] = _awin_por_tienda.get(d["tienda"], 0) + 1
    except Exception as e:
        print(f"   ❌ Error en AWIN feed: {e}")
        alertar_admin("Error en AWIN feed", str(e))


    print(f"\n✅ Total: {len(todos)} productos únicos de {len({p.tienda for p in todos})} tiendas")
    return todos


# ════════════════════════════════════════════════════════════════
# FASE 2 — CAMELCAMELCAMEL (verificación precio histórico)
#
#  CCC embebe los datos de la gráfica como JSON (Chart.js) en el HTML.
#  Buscamos el precio mínimo histórico del canal "Amazon" (venta directa).
# ════════════════════════════════════════════════════════════════

def _consultar_keepa(asin: str) -> tuple[float, float]:
    """
    Consulta el historial de precios en Keepa para Amazon.es (domain=9).
    Devuelve (precio_minimo_90d, precio_promedio_90d) en EUR.
    Usa Buy Box > Amazon directo > Marketplace New (orden de preferencia).
    Retorna (0.0, 0.0) si no hay API key o no hay datos.
    """
    if not KEEPA_API_KEY:
        return 0.0, 0.0
    try:
        resp = requests.get(
            "https://api.keepa.com/product",
            params={"key": KEEPA_API_KEY, "domain": "9", "asin": asin, "stats": "90"},
            timeout=10,
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
        if not products:
            return 0.0, 0.0

        stats = products[0].get("stats", {})
        if not stats:
            return 0.0, 0.0

        # Keepa stats.avgInInterval / stats.minInInterval = últimos 90 días
        # Fallback a stats.avg / stats.min si no hay intervalo.
        avg_arr = stats.get("avgInInterval") or stats.get("avg") or []
        min_arr = stats.get("minInInterval") or stats.get("min") or []

        # Índices Keepa: 3=Buy Box, 2=Amazon directo, 0=Marketplace New
        def _pick(arr: list) -> float:
            for idx in (3, 2, 0):
                if len(arr) > idx and arr[idx] not in (-1, None) and arr[idx] > 0:
                    return round(arr[idx] / 100, 2)   # cents → EUR
            return 0.0

        avg_eur = _pick(avg_arr)
        min_eur = _pick(min_arr)
        if avg_eur <= 0:
            return 0.0, 0.0
        return (min_eur if min_eur > 0 else avg_eur), avg_eur

    except Exception as e:
        print(f"   ⚠️  Keepa error ({asin}): {e}")
        return 0.0, 0.0


async def verificar_con_keepa(productos: list[Producto]) -> list[Producto]:
    """
    Filtra y corrige productos usando historial de precios de Keepa (90 días, Buy Box).
    Sin Playwright — simple HTTP. Funciona en ciclos FLASH si hay API key.

    Mismas dos comprobaciones que CCC:
      1. precio_original > avg_90d × 1.25 → referencia inflada → recalcular o descartar.
      2. precio_actual > min_90d × 1.20 → ya no es el mínimo → descartar.
    """
    if not KEEPA_API_KEY:
        print("   ⚠️  KEEPA_API_KEY no configurada — saltando verificación Keepa")
        return productos

    print(f"\n📊 Verificando historial en Keepa ({len(productos)} productos)...")
    verificados: list[Producto] = []

    for p in productos:
        min_h, avg_h = await asyncio.to_thread(_consultar_keepa, p.asin)
        p.precio_historico_min = min_h

        if avg_h > 0:
            ref_normal = avg_h

            # ── Comprobación 1: referencia de precio inflada ──────────────────────
            if p.precio_original > 0 and p.precio_original > ref_normal * RATIO_PRECIO_REF_INFLADO:
                descuento_real = round((1 - p.precio_actual / ref_normal) * 100)
                if descuento_real < DESCUENTO_MINIMO:
                    print(
                        f"   ❌ Desc. falso — avg 90d {avg_h}€ → desc. real {descuento_real}%: "
                        f"{p.titulo[:38]}"
                    )
                    await asyncio.sleep(0.3)
                    continue
                print(
                    f"   ⚠️  Ref. corregida {p.precio_original}€→{ref_normal}€ "
                    f"(desc. real {descuento_real}%): {p.titulo[:38]}"
                )
                p.precio_original = ref_normal
                p.descuento_pct   = max(0, descuento_real)

            # ── Comprobación 2: precio actual vs mínimo 90d ───────────────────────
            ratio = p.precio_actual / min_h if min_h > 0 else 1.0
            if ratio <= RATIO_HISTORICO_MAX:
                print(
                    f"   ✅ {p.titulo[:45]:<45} | "
                    f"{p.precio_actual}€ (avg 90d {avg_h}€ / mín {min_h}€)"
                )
                verificados.append(p)
            else:
                print(
                    f"   ❌ Precio actual {ratio:.2f}x del mínimo 90d ({min_h}€): "
                    f"{p.titulo[:40]}"
                )
        else:
            # Sin historial Keepa — aplicar check de ratio extremo
            if (p.precio_original > 0 and p.precio_actual > 0
                    and (p.descuento_pct >= _DESC_MAX_SIN_VERIFICAR
                         or p.precio_original / p.precio_actual > 8)):
                print(
                    f"   ❌ Sin Keepa + descuento inverificable "
                    f"({p.descuento_pct}%, {p.precio_original/p.precio_actual:.1f}x): {p.titulo[:40]}"
                )
                await asyncio.sleep(0.3)
                continue
            print(f"   ⚠️  Sin historial Keepa: {p.titulo[:45]}")
            verificados.append(p)

        await asyncio.sleep(0.3)   # ~3 req/s — dentro del free tier de Keepa

    print(f"✅ {len(verificados)}/{len(productos)} productos verificados con Keepa")
    return verificados


async def verificar_con_ccc(
    productos: list[Producto], context: BrowserContext
) -> list[Producto]:
    """
    Fallback cuando no hay KEEPA_API_KEY.
    Prioridad: historial propio (price_history) → CamelCamelCamel (Playwright).
    """
    print(f"\n📊 Verificando historial ({len(productos)} productos) — historial propio + CCC...")
    verificados: list[Producto] = []

    for p in productos:
        # 1. Historial propio: precio_original acumulado durante scans de Amazon
        min_h, avg_h, n_dias = _calcular_historial_amazon(p.asin)
        fuente = f"historial propio ({n_dias}d)"

        if avg_h <= 0:
            # 2. Fallback a CCC si no hay datos propios suficientes
            min_h, avg_h = await _scrape_ccc(p.asin, context)
            fuente = "CCC"
            await asyncio.sleep(1.5)

        p.precio_historico_min = min_h

        if min_h > 0:
            ref_normal = avg_h if avg_h > 0 else min_h
            if p.precio_original > 0 and p.precio_original > ref_normal * RATIO_PRECIO_REF_INFLADO:
                descuento_real = round((1 - p.precio_actual / ref_normal) * 100)
                if descuento_real < DESCUENTO_MINIMO:
                    print(
                        f"   ❌ Descuento falso — ref. Amazon {p.precio_original}€ vs "
                        f"promedio hist. {ref_normal}€ → desc. real {descuento_real}%: "
                        f"{p.titulo[:38]}"
                    )
                    await asyncio.sleep(1.5)
                    continue
                print(
                    f"   ⚠️  Ref. corregida {p.precio_original}€→{ref_normal}€ "
                    f"(desc. real {descuento_real}%): {p.titulo[:38]}"
                )
                p.precio_original = ref_normal
                p.descuento_pct   = max(0, descuento_real)

            ratio = p.precio_actual / min_h
            if ratio <= RATIO_HISTORICO_MAX:
                print(f"   ✅ [{fuente}] {p.titulo[:42]:<42} | {p.precio_actual}€ (mín {min_h}€ / avg {ref_normal}€)")
                verificados.append(p)
            else:
                print(f"   ❌ [{fuente}] {ratio:.2f}x del mínimo ({min_h}€): {p.titulo[:40]}")
        else:
            if (p.precio_original > 0 and p.precio_actual > 0
                    and (p.descuento_pct >= _DESC_MAX_SIN_VERIFICAR
                         or p.precio_original / p.precio_actual > 8)):
                print(
                    f"   ❌ Sin CCC + descuento inverificable "
                    f"({p.descuento_pct}%, {p.precio_original/p.precio_actual:.1f}x): {p.titulo[:40]}"
                )
                await asyncio.sleep(1.5)
                continue
            print(f"   ⚠️  Sin historial CCC: {p.titulo[:45]}")
            verificados.append(p)

        await asyncio.sleep(1.5)

    print(f"✅ {len(verificados)} productos con precio verificado")
    return verificados


# ════════════════════════════════════════════════════════════════
# FASE 2b — RATIO CAP para PCBox
#
#  PreviousPrice de Tradedoubler puede ser el MSRP del fabricante.
#  Si el ratio precio_original/precio_actual supera 3×, lo descartamos:
#  un descuento de más del 66% sobre precio de catálogo nunca verificado
#  es estadísticamente muy sospechoso en PCBox.
# ════════════════════════════════════════════════════════════════

_PCBOX_RATIO_MAX = 3.0  # Descartar si precio_original > 3× precio_actual


def _filtrar_pcbox_por_ratio(productos: list[Producto]) -> list[Producto]:
    """Descarta productos PCBox cuyo PreviousPrice supera 3× el precio actual.

    Ratios > 3× indican con alta probabilidad que PCBox usó el MSRP del fabricante
    como referencia, no su precio real de venta anterior (EU Omnibus Directive).
    """
    if not productos:
        return productos
    print(f"\n🔎 PCBox ratio check ({len(productos)} productos)...")
    verificados: list[Producto] = []
    for p in productos:
        ratio = p.precio_original / p.precio_actual if p.precio_actual > 0 else 0
        if ratio > _PCBOX_RATIO_MAX:
            print(
                f"   ❌ Ratio {ratio:.1f}x (>{_PCBOX_RATIO_MAX}x) — PreviousPrice sospechoso: "
                f"{p.titulo[:45]} ({p.precio_actual}€ vs {p.precio_original}€)"
            )
            continue
        if ratio >= 2.0:
            print(f"   ⚠️  Ratio {ratio:.1f}x (alto pero dentro del límite): {p.titulo[:45]}")
        verificados.append(p)
    print(f"✅ {len(verificados)}/{len(productos)} productos PCBox dentro del ratio permitido")
    return verificados


async def _scrape_ccc(asin: str, context: BrowserContext) -> tuple[float, float]:
    """
    Extrae historial de precios de CamelCamelCamel.
    Devuelve (precio_minimo, precio_promedio). (0.0, 0.0) si no hay datos.
    El promedio es más estable que el mínimo para detectar referencias infladas.
    """
    url = f"https://camelcamelcamel.com/es/product/{asin}"
    page = await context.new_page()
    try:
        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
        content = await page.content()

        # Opción 1: JSON Chart.js embebido — array de datos del canal Amazon
        match = re.search(
            r'"label"\s*:\s*"Amazon".*?"data"\s*:\s*\[([^\]]+)\]',
            content, re.DOTALL
        )
        if match:
            nums = re.findall(r'[\d.]+', match.group(1))
            precios = [float(n) for n in nums if float(n) > 1]
            if precios:
                precio_min = round(min(precios), 2)
                precio_avg = round(sum(precios) / len(precios), 2)
                return precio_min, precio_avg

        # Opción 2: texto visible "precio mínimo"
        match2 = re.search(
            r'(?:precio m[íi]nimo|lowest price)[^\d]*(\d+[.,]\d{2})',
            content, re.IGNORECASE
        )
        if match2:
            val = float(match2.group(1).replace(',', '.'))
            return val, val  # sin promedio disponible, usar el mismo

        # Opción 3: og:description con rango "desde X€"
        match3 = re.search(r'desde\s+(\d+[.,]\d{2})\s*€', content, re.IGNORECASE)
        if match3:
            val = float(match3.group(1).replace(',', '.'))
            return val, val

        return 0.0, 0.0
    except Exception:
        return 0.0, 0.0
    finally:
        await page.close()

# ════════════════════════════════════════════════════════════════
# FASE 3 — SCORING LOCAL + CLAUDE AI (solo zona gris)
# ════════════════════════════════════════════════════════════════

# Marcas con alta demanda / reventa en España
_MARCAS_CONOCIDAS = {
    "apple", "samsung", "sony", "lg", "philips", "bosch", "dyson",
    "nike", "adidas", "new balance", "jordan", "asics", "puma", "reebok",
    "lego", "nintendo", "playstation", "xbox", "switch",
    "playmobil", "hasbro", "mattel", "funko", "nerf", "barbie", "fisher-price",
    "bandai", "ravensburger", "schleich", "vtech", "clementoni", "spin master",
    "bose", "airpods", "jabra", "sennheiser", "hyperx", "jbl",
    "nespresso", "delonghi", "tefal", "rowenta", "braun", "siemens", "breville", "sage",
    "dior", "chanel", "armani", "calvin klein", "lacoste", "north face",
    "roborock", "roomba", "irobot", "lefant", "dreame", "ecovacs", "eufy", "cecotec",
    "xiaomi", "redmi", "poco", "realme", "oneplus", "oppo", "baseus",
    "braun", "oral-b", "oral b", "remington", "wahl", "panasonic",
    "shark", "bissell", "karcher", "kenwood", "magimix", "vitamix",
    "kindle",
    "gopro", "garmin", "fitbit", "polar",
    "makita", "dewalt", "milwaukee", "stanley",
    "canon", "nikon", "fujifilm", "olympus",
    "hp", "dell", "lenovo", "asus", "acer", "microsoft",
    "logitech", "razer", "corsair", "steelseries",
    "g-shock", "casio", "seiko", "citizen", "timex",
    # Deportes outdoor / ciclismo / ski
    "oakley", "poc", "giro", "scott", "salomon", "uvex",
    "specialized", "orbea", "trek", "giant", "conor", "bh", "cannondale", "canyon",
    "columbia", "helly hansen", "timberland", "patagonia",
    "wahoo", "suunto", "coros",
    # Outdoor / montaña / escalada (Barrabes)
    "mammut", "black diamond", "mountain equipment", "arc'teryx", "arcteryx",
    "rab", "millet", "haglofs", "haglöfs", "fjallraven", "fjällräven",
    "scarpa", "salewa", "la sportiva", "ternua", "trangoworld",
    "norrona", "icebreaker", "compressport", "dynafit", "ortovox",
    # Moda premium adicional
    "ralph lauren", "tommy hilfiger", "stone island", "burberry",
    # Pádel (Padel Market)
    "bullpadel", "babolat", "nox", "siux", "starvie", "varlion", "black crown",
    "drop shot", "vibor-a", "vibora", "joma", "wilson", "dunlop",
    # Equipaje / maletas de marca
    "samsonite", "american tourister", "eastpak", "delsey", "rimowa", "roncato", "gabol",
    # ── Perfumería / fragancia de lujo (Paco Perfumerias, Beauty Corner, Esdemarca) ──
    "paco rabanne", "jean paul gaultier", "carolina herrera", "hugo boss", "versace",
    "givenchy", "yves saint laurent", "ysl", "guerlain", "tom ford", "dolce & gabbana",
    "dolce gabbana", "azzaro", "montblanc", "issey miyake", "bvlgari", "bulgari",
    "gucci", "valentino", "lancôme", "lancome", "viktor & rolf", "narciso rodriguez",
    "mugler", "kenzo", "cacharel", "loewe", "jimmy choo", "marc jacobs", "adolfo dominguez",
    # ── Maquillaje / skincare premium (Beauty Corner, OneBioShop, ECI) ──
    "clinique", "estee lauder", "estée lauder", "shiseido", "clarins", "biotherm",
    "kiehl's", "kiehls", "sisley", "elizabeth arden", "urban decay", "charlotte tilbury",
    "fenty", "l'occitane", "loccitane", "rituals", "cocunat", "freshly cosmetics",
    # ── Calzado premium (Toni Pons, Cole Haan, Esdemarca) ──
    "cole haan", "pikolinos", "geox", "clarks", "dr martens", "dr. martens",
    "birkenstock", "hoka", "merrell", "toni pons",
    # ── Cocina / hogar premium (WMF y feeds de electrodomésticos) ──
    "wmf", "moulinex", "russell hobbs", "ninja", "cuisinart", "le creuset",
    "zwilling", "victorinox", "lékué", "lekue",
    # ── Surf / skate / acción (Billabong, DC Shoes, Quiksilver…) ──
    "quiksilver", "dc shoes", "billabong", "rip curl", "o'neill", "oneill", "volcom", "hurley",
    # ── Outerwear / moda premium (ECI, Esdemarca) ──
    "moncler", "canada goose", "napapijri", "belstaff", "parajumpers", "woolrich",
    "jack wolfskin", "k-way", "superdry", "g-star", "pepe jeans", "scalpers",
    "bimba y lola", "purificacion garcia",
    # ── Electrodomésticos gama media (chollos que la gente busca) ──
    "taurus", "orbegozo", "ufesa", "fagor", "tristar", "palson",
} | _MARCAS_DERMO | _MARCAS_DROGUERIA  # dermo premium + droguería gama media cuentan como marca reconocida

# Marcas con mercado real de segunda mano en Wallapop/eBay.es → candidatas a ARBITRAJE
_MARCAS_ARBITRAJE = {
    # Sneakers / moda premium
    "nike", "adidas", "jordan", "new balance", "asics", "puma", "reebok", "north face",
    # Tech
    "apple", "airpods", "samsung", "sony",
    # Gaming
    "nintendo", "playstation", "xbox", "switch", "lego",
    # Cámaras / wearables
    "gopro", "canon", "nikon", "fujifilm", "garmin",
    # Relojes
    "g-shock", "casio", "seiko", "citizen",
    # Perfumería de lujo
    "dior", "chanel", "armani", "calvin klein",
    # Herramientas profesionales
    "makita", "dewalt", "milwaukee", "bosch",
    # Auriculares premium
    "bose", "jabra", "sennheiser",
}

# Tiendas con feed curado + historial de precios PROPIO verificado: la bajada ya se
# valida contra su propio histórico de 30 días, así que no exigimos marca reconocida
# en la zona gris (sus marcas propias —Kiprun, Quechua…— no están en _MARCAS_CONOCIDAS).
_TIENDAS_FEED_CONFIABLE = {
    "Decathlon", "Padel Market", "Adidas",
    # Tiendas AWIN con descuento detectado por NUESTRO histórico (price_drop) → ya verificado
    "ElCorteIngles", "Zalando", "Deporte Outlet", "Brico Depot", "Paco Perfumerias", "Bikila",
    "OneBioShop", "Tiendanimal", "Carrefour", "ToysRus", "TodoConsolas", "Bauhaus",
    # Foot Locker sí trae product_price_old real, pero su catálogo es Nike/Adidas/
    # Jordan/New Balance: marcas reconocidas de sobra, no necesita la exención.
    "Foot Locker",
    # Feeds TD solo-historial: bajada detectada por NUESTRO histórico (price_drop) → verificada
    "Braun", "De'Longhi", "Tefal", "Suunto", "L'Occitane", "The Beauty Corner",
    "Eureka Electrodomésticos", "DC Shoes", "Quiksilver", "Roxy", "Element",
}

# ── Marca al frente del título ────────────────────────────────────
# Publicamos solo marcas reconocidas → la marca debe ser lo primero que se lee, en
# Telegram, Threads y web. La marca viene en posiciones distintas según la tienda
# (MediaMarkt "Cat - Marca Modelo", Decathlon "... Marca" al final, etc.).
# MANTENER EN SYNC con MARCAS_RECONOCIDAS de index.html. Curada para evitar palabras
# ambiguas en español (poco/honor/on/giro/polar...).
_MARCAS_TITULO = sorted([
    "The North Face","Helly Hansen","New Balance","Ultimate Ears","Calvin Klein","Ralph Lauren",
    "Tommy Hilfiger","Stone Island","Harman Kardon","Audio-Technica","De'Longhi","Arc'teryx",
    "Stanley","Shokz","Samsung","Xiaomi","Huawei","Realme","Motorola","Nokia","Sony","Apple",
    "Philips","Braun","Rowenta","Tefal","Siemens","Bosch","Balay","Haier","Beko","Candy","Teka",
    "Whirlpool","Electrolux","Hisense","Indesit","Smeg","Hoover","Liebherr","AEG","Zanussi","Cecotec",
    "iRobot","Roborock","Roomba","Dreame","Ecovacs","Eufy","Dyson","Shark","Bissell","Karcher",
    "Kenwood","Magimix","Vitamix","Nespresso","Breville","Sage","Razer","Logitech","Corsair",
    "SteelSeries","HyperX","Bose","JBL","Jabra","Sennheiser","Marshall","Beats","Anker","Soundcore",
    "Sonos","Denon","Pioneer","JVC","Klipsch","Nike","Adidas","Jordan","Asics","Puma","Reebok",
    "Patagonia","Columbia","Timberland","Mammut","Salomon","Scarpa","Salewa","Rab","Oakley","Uvex",
    "Regatta","Spiuk","Garmin","GoPro","Fitbit","Suunto","Coros","Wahoo","Casio","Seiko","Citizen",
    "Timex","G-Shock","Makita","DeWalt","Milwaukee","Canon","Nikon","Fujifilm","Olympus","Lego",
    "Nintendo","PlayStation","Xbox","Kindle","Dior","Chanel","Armani","Lacoste","Burberry","Panasonic",
    "Remington","Oral-B","Wahl","Microsoft","Lenovo","Asus","Acer","Dell","Orbea","Cannondale",
    "Specialized","Canyon","Scott","Giant","Trek","Conor","LG","TCL","Devialet","Teufel","Redmi",
    "OnePlus","Oppo","Baseus",
    # Dermocosmética premium
    "La Roche-Posay","ISDIN","CeraVe","Avène","Vichy","Eucerin","Bioderma","Sesderma","Filorga",
    "Caudalie","Nuxe","Cetaphil","Neutrogena","Martiderm","Heliocare","Uriage","Mustela","Ducray",
    "Endocare","Rilastil","A-Derma","Anthelios",
    # Pádel
    "Bullpadel","Babolat","StarVie","Varlion","Black Crown","Drop Shot","Vibor-A","Nox","Siux","Joma","Wilson","Dunlop",
    # Equipaje / maletas
    "Samsonite","American Tourister","Eastpak","Delsey","Rimowa","Roncato","Gabol",
    # Perfumería / fragancia de lujo
    "Paco Rabanne","Jean Paul Gaultier","Carolina Herrera","Hugo Boss","Versace","Givenchy",
    "Yves Saint Laurent","Guerlain","Tom Ford","Dolce & Gabbana","Azzaro","Montblanc",
    "Issey Miyake","Bvlgari","Prada","Gucci","Valentino","Lancôme","Viktor & Rolf",
    "Narciso Rodriguez","Mugler","Kenzo","Cacharel","Loewe","Jimmy Choo","Marc Jacobs","Tous","Adolfo Domínguez",
    # Maquillaje / skincare premium
    "Clinique","Estée Lauder","Shiseido","Clarins","Biotherm","Kiehl's","Sisley","Elizabeth Arden",
    "Urban Decay","Charlotte Tilbury","Fenty Beauty","L'Occitane","Rituals","Cocunat","Freshly Cosmetics","Benefit",
    # Calzado premium
    "Cole Haan","Pikolinos","Camper","Geox","Clarks","Dr. Martens","Birkenstock","UGG","Hoka","Merrell","Toni Pons",
    # Cocina / hogar premium
    "WMF","Moulinex","Russell Hobbs","Ninja","Cuisinart","Le Creuset","Zwilling","Victorinox","Lékué",
    # Surf / skate / acción
    "Quiksilver","Roxy","DC Shoes","Billabong","Element","Vans","Rip Curl","O'Neill","Volcom","Hurley",
    # Outerwear / moda premium
    "Moncler","Canada Goose","Napapijri","Belstaff","Parajumpers","Woolrich","Jack Wolfskin","K-Way",
    "Superdry","G-Star","Pepe Jeans","Scalpers","Bimba y Lola","Purificación García",
    # Droguería / gama media (maquillaje, capilar, solar, higiene, afeitado)
    "Maybelline","L'Oréal","Rimmel","Revlon","Max Factor","Catrice","NYX","Bourjois","Kiko Milano",
    "Deborah Milano","Essence","Astor","Pantene","Garnier","Elvive","Garnier Fructis","TRESemmé","Syoss",
    "Schwarzkopf","Gliss","Wella","Herbal Essences","John Frieda","OGX","Aussie","Batiste","Nivea","Sanex",
    "Johnson's","Natural Honey","Dove","Piz Buin","Ambre Solaire","Delial","Colgate","Sensodyne","Parodontax",
    "Listerine","Lacer","Gillette","Wilkinson","Schick","Veet","Rexona","Axe",
    "Taurus","Orbegozo","Ufesa","Fagor","Tristar","Palson",
], key=len, reverse=True)  # multi-palabra primero

def _brand_pat(b: str) -> str:
    return re.escape(b).replace("'", "['´’]?")

def _marca_al_frente(titulo: str) -> str:
    """Antepone la marca reconocida: 'Cat - Marca Modelo' → 'Marca - Cat Modelo'.
    Conserva el resto del título. Si ya empieza por la marca o no hay marca conocida, lo deja igual."""
    t = (titulo or "").strip()
    if not t:
        return t
    brand = next((b for b in _MARCAS_TITULO
                  if re.search(r'\b' + _brand_pat(b) + r'\b', t, re.I)), None)
    if not brand:
        return t
    if re.match(r'^\s*' + _brand_pat(brand) + r'\b', t, re.I):
        return t  # ya al frente
    rest = re.sub(r'\s*[-–—·,]?\s*\b' + _brand_pat(brand) + r'\b', ' ', t, count=1, flags=re.I)
    rest = re.sub(r'\s{2,}', ' ', rest).strip(' -–—·,:')
    return f"{brand} - {rest}" if rest else brand

# Umbrales pre-scorer
_SCORE_AUTO_APROBAR  = 70   # ≥70 → auto-aprobado (ARBITRAJE o OFERTA según marca), sin Claude
_SCORE_AUTO_DESCARTAR = 30  # <30 → descartado, sin Claude


def _copy_template(p: "Producto") -> str:
    """Copy de 1 frase para deals auto-aprobados (sin llamada a IA)."""
    desc = p.descuento_pct
    marca = next((m.title() for m in _MARCAS_CONOCIDAS if m in p.titulo.lower()), "")
    ahorro = round(p.precio_original - p.precio_actual) if p.precio_original > 0 else 0
    if ahorro >= 50 and marca:
        return f"{marca} a precio de oportunidad: ahorra {ahorro}€ reales"
    if desc >= 60:
        return f"Más de la mitad de descuento en un producto de calidad contrastada"
    if marca:
        return f"Precio mínimo en {marca}: una de las mejores ofertas del año"
    return f"Descuento del {desc}% en producto con alta demanda"


_CAT_RE = {
    # IMPORTANTE: el orden determina prioridad — la primera regex que matchea gana.
    # calzado va ANTES que deportes para que zapatillas (incluidas las deportivas/ciclismo) vayan a calzado.
    # Calzado: SOLO términos de calzado + modelos icónicos de zapatilla. NO marcas sueltas
    # (nike/adidas/salomon… también hacen ropa y material deportivo → contaminaban calzado).
    "calzado":      re.compile(
        r'zapatilla|zapato|deportiva[s]?\b|\bbota[s]?\b|bot[ií]n|botines|sandalia|chancla|'
        r'mocas[ií]n|sneaker|calzado\b|alpargata|espadrille|zueco|bailarina|merceditas|n[aá]utico|playera\b|'
        r'air\s*max|air\s*force|ultraboost|speedcross|gel-?(?:kayano|nimbus|cumulus|pulse)|'
        r'\bgazelle\b|\bsamba\b|stan\s*smith|superstar\b|\bdunk\b|\bpegasus\b|vapormax|\bcortez\b',
        re.I),
    "tecnologia":   re.compile(
        r'smartphone|m[oó]vil|iphone|galaxy\b|tablet|ipad|port[aá]til|laptop|macbook|'
        r'pc gaming|monitor\b|televisor|\btv\b|oled|qled|auricular|cascos|airpods|'
        r'wh-?1000|bose\s*q|kindle|c[aá]mara\b|gopro|smartwatch|consola\b|ps5|playstation|'
        r'xbox|nintendo|switch\b|\bps[345]\b|\bssd\b|disco duro|\bram\b|gpu|rtx|impresora|'
        r'microcadena|thrustmaster|simulador de vuelo|tarjeta gr[aá]fica|graphics card|'
        # "procesador" SOLO de CPU (antes cazaba "procesador de alimentos")
        r'procesador\s+(?:intel|amd|ryzen|core)|intel\s+core\b|\bryzen\b|\bcpu\b|'
        r'router|logitech|razer|corsair|steelseries|hyperx|teclado\b|rat[oó]n\b|'
        r'altavoz\b|barra de sonido|soundbar|subwoofer|echo dot|google home|chromecast|fire\s*tv|'
        r'proyector|projector|patinete el[eé]ctrico|scooter el[eé]ctrico|'
        r'bombilla|tira led|enchufe intelig|webcam|pendrive|microsd|tarjeta de memoria|'
        r'gamepad|joystick|repetidor wifi|power\s*bank|bater[ií]a.*externa|usb\s*hub|hub\s*usb',
        re.I),
    "herramientas": re.compile(
        r'dewalt|makita|milwaukee|k[aä]rcher|stanley\b|ryobi\b|bahco\b|knipex\b|'
        r'martillo|taladro|sierra\b|lijadora|compresor|soldad|atornillador|amoladora|'
        r'destornillador|nivel.*l[aá]ser|multim[eé]tro|flex[oó]metro|llave inglesa|'
        r'alicate|bosch.*(taladro|sierra|amoladora|compresor|atornillador|lijadora|gbh|gsr|gks|gws)',
        re.I),
    "deportes":     re.compile(
        r'bicicleta\b|\bbici\b|ciclismo|mountain bike|\bmtb\b|gravel\b|\btrek\b|'
        r'\bski\b|s-?lab|trail\b|trekking|monta[ñn]a\b|outdoor\b|running\b|runner\b|'
        r'senderismo|escalada|alpinismo|mancuerna|kettlebell|\bpesas\b|'
        r'nataci[oó]n|swim\b|fitness\b|gym\b|bal[oó]n|raqueta|p[aá]del|'
        r'esqu[ií]|snowboard|surf\b|alpinestars|\bgiro\b|casco\b.*bici|shimano|'
        r'under armour|garmin|polar\b|fitbit|'
        r'componente.*bici|sill[ií]n|manillar|potencia.*bici',
        re.I),
    "hogar":        re.compile(
        r'cafetera|nespresso|delonghi|dolce.?gusto|sage\b|breville\b|krups\b|jura\b|'
        r'aspirador|robot.?aspirador|roomba|irobot|roborock|lefant|dreame|ecovacs|eufy\b|'
        r'freidora|airfryer|air.?fryer|microondas|lavadora|lavavajillas|'
        r'frigor[ií]fico|nevera|secadora\b|'
        r'plancha\b(?!.*(?:pelo|cabello|alisad))|plancha.*vapor|centro.*planchado|vaporeta|vaporizador|cepillo.*vapor|vapor.*cepillo|'
        r'campana\b|campana.*extract|extractor.*humos|extractor.*cocina|\bteka\b|'
        r'batidora|thermomix|olla.*presi[oó]n|robot.*cocina|procesador de alimentos|'
        r'amasadora|exprimidor|licuadora|tostadora|hervidor|sandwichera|gofrera|molinillo|'
        r'\bolla\b|crock.?pot|cocci[oó]n lenta|slow cooker|freidora|'
        r'escoba.*vapor|mopa.*vapor|espumador|recipiente herm[eé]tic|fiambrera|'
        r'tefal|rowenta|shark\b|hoover\b|dyson|cecotec|bissell\b|kenwood\b|magimix\b|'
        r'calefactor|radiador.*el[eé]ctrico|aire.*acondicionado|\bsplit\b|ventilador\b|'
        r'purificador.*aire|humidificador|deshumidificador|'
        r'placa.*inducci[oó]n|inducci[oó]n\b|vitrocer[aá]mic|\bhorno\b|'
        r'colch[oó]n|l[aá]mpara|sill[oó]n|sof[aá]|escritorio|estanter[ií]a|'
        # Baño y reforma (catálogo Bauhaus): antes caían en "otras"
        r'plato de ducha|mampara|cabina de ducha|\bgrifo\b|griber[ií]a|monomando|'
        r'\blavabo\b|\bbid[eé]\b|\binodoro\b|\bcisterna\b|mueble de ba[ñn]o|'
        r'columna de ducha|\bmampara\b|toallero|\bazulejo|\bbald[oó]sa|\bparquet\b|'
        r'\btarima\b|c[eé]sped artificial|\bpérgola\b|\bpergola\b|\btoldo\b|'
        r'armario\b|c[oó]moda\b|\bmesita\b|somier|canap[eé]',
        re.I),
    "mascotas":     re.compile(
        r'\bpienso\b|comida.*(?:perro|gato|mascota)|snack.*(?:perro|gato)|premios.*(?:perro|gato)|'
        r'arena.*(?:gato|sanitaria)|rascador|comedero|bebedero|transport[ií]n|'
        r'antiparasit|pipeta.*(?:perro|gato)|collar.*(?:antiparasit|perro|gato)|correa.*perro|'
        r'\bacuario\b|pecera|terrario|royal canin|purina|whiskas|friskies|pedigree|'
        r'\bfelix\b|dentastix|acana|orijen',
        re.I),
    "belleza":      re.compile(
        r'perfume|colonia|eau de|fragancia|\bm[aá]quillaje\b|labial|'
        r'protector solar|fotoprotector|anthelios|la roche.?posay|isdin|cerave|'
        r'av[eè]ne|vichy|eucerin|bioderma|sesderma|filorga|caudalie|heliocare|'
        r'crema.*facial|crema.*corporal|crema.*hidratante|s[eé]rum.*facial|'
        r'lanc[oô]me|loreal|l\'or[eé]al|nivea|olay\b|est[eé]e lauder|'
        r'\bafeitadora\b|maquinilla.*afeit|\brasuradora\b|oneblade|cepillo.*dental|sonicare|irrigador.*bucal|'
        r'\bdepilador\b|\bepilador\b|'
        r'oral.?b|remington\b|wahl\b|\bbabyliss\b|ghd\b|'
        # NOTA: "plancha de pelo" va a HOGAR (no aquí). Marcas de lujo sueltas (dior/armani/
        # calvin klein…) van a MODA — aquí solo cosmética/grooming.
        # ⚠️ Palabras sueltas SIEMPRE con \b — "rizador" sin límite cazaba "Tempo-rizador" (ventilador).
        r'\brizador\b|\bmoldeador\b|\balisador\b|plancha.*(?:pelo|cabello)|plancha alisadora|'
        r'secador.*pelo|\bcortapelos\b|recortadora.*barba|recortadora.*pelo|\bcortabarba\b',
        re.I),
    "juguetes":     re.compile(
        r'playmobil|\blego\b|hasbro|mattel|hot wheels|barbie|funko\b|'
        r'juguete|juego de mesa|puzzle|puzle|scalextric|\bnerf\b|'
        r'rc\b.*coche|coche.*teledirigido|coche.*radiocontrol|'
        r'\bdron\b|\bdrone\b',
        re.I),
    "moda":         re.compile(
        r'mochila|bolso\b|cartera\b|monedero|maleta\b|neceser|'
        r'camiseta|\bcamisa\b|pantal[oó]n|pantalones|\bshort\b|bermuda|sudadera|\bpolo\b|jersey|jers[eé]y|'
        r'abrigo|chaqueta|chaquet[oó]n|cazadora|b[oó]mber|parka|anorak|chubasquero|'
        r'vestido|falda|blusa|americana|blazer|gabardina|plum[ií]fero|c[aá]rdigan|'
        r'\btop\b|\bmono\b|chaleco|sobrecamisa|\bpeto\b|\bbody\b|leggins?|\bmallas?\b|ba[ñn]ador|bikini|biquini|'
        r'lacoste\b|ralph lauren|tommy hilfiger|tommy jeans|pepe jeans|\bguess\b|\bgant\b|g-star|massimo dutti|'
        r'\barmani\b|calvin klein|hugo boss|\bboss\b|\bdior\b|\bchanel\b|ysl\b|saint laurent|'
        r'michael kors|barbour|hackett|fred perry|levi\'?s|\blevis\b|wrangler|replay\b|superdry|stone island|'
        r'gafas.*sol|gafas.*graduada|cintur[oó]n\b|corbata|bufanda|gorra',
        re.I),
}
_TIENDA_CAT = {
    "PcComponentes": "tecnologia",   # solo componentes/periféricos — OK como fallback
    # MediaMarkt y Worten venden tecnología Y electrodomésticos Y belleza:
    # no usar como fallback de categoría — dejar que _CAT_RE decida o asignar "otras"
    "Decathlon":      "deportes",
    "Mammoth Bikes":  "deportes",
    "ToysRus":        "juguetes",
    "Deporte Outlet": "deportes",
    "Zalando":        "moda",
    "Esdemarca":      "moda",   # tienda de moda de marca (lo que no es calzado → moda)
    "Desigual":       "moda",
    "Paco Perfumerias": "belleza",   # perfumería
    "OneBioShop":     "belleza",     # cosmética natural/bio
    "Tiendanimal":    "mascotas",    # productos para mascotas
    "ToysRus":        "juguetes",    # juguetería (feed TD histórico)
}
# Tiendas 100% deporte: todo su material va a deportes (o calzado), nunca a moda.
_TIENDAS_DEPORTE = {"Decathlon", "Barrabes", "Mammoth Bikes", "PrivateSportShop",
                    "Deporte Outlet", "Padel Market", "Adidas", "Bikila"}


def _inferir_categoria(p: "Producto") -> str:
    """Asigna una categoría al producto basándose en título y tienda."""
    # Low cost tiene prioridad — precio < PRECIO_MINIMO con descuento exigente
    if PRECIO_MINIMO_LC <= p.precio_actual < PRECIO_MINIMO and p.descuento_pct >= DESCUENTO_LC_MINIMO:
        return "low_cost"

    # Tiendas 100% deporte: su material (incl. ropa deportiva) va a DEPORTES, no a moda;
    # el calzado (más específico) sí va a calzado. Evita que una camiseta/pala de Decathlon,
    # Barrabes, Padel Market… acabe en Moda por la regex de ropa.
    if p.tienda in _TIENDAS_DEPORTE:
        return "calzado" if _CAT_RE["calzado"].search(p.titulo) else "deportes"

    if p.tienda in _TIENDA_CAT:
        # Aun así verificar si el título sugiere otra categoría más específica
        tienda_cat = _TIENDA_CAT[p.tienda]
    else:
        tienda_cat = None

    for cat, rx in _CAT_RE.items():
        if rx.search(p.titulo):
            return cat

    return tienda_cat or "otras"


def _score_local(p: "Producto") -> int:
    """
    Scoring rápido basado en reglas (0-100). Sin IA.
    Determina si un producto va directo (≥70), a Claude (35-69) o se descarta (<35).
    """
    score = 0

    # Descuento real (hasta 40 pts)
    if p.descuento_pct >= 65:
        score += 40
    elif p.descuento_pct >= 55:
        score += 30
    elif p.descuento_pct >= 45:
        score += 20
    elif p.descuento_pct >= 40:
        score += 10

    # Gran electrodoméstico (gasto fuerte): rara vez baja del 40%, pero el ahorro
    # absoluto es alto. Bonus que garantiza que un 30-39% en un electrodoméstico caro
    # entre en zona gris (≥_SCORE_AUTO_DESCARTAR) aunque la marca no sea "conocida".
    if _es_gran_electrodomestico(p.titulo, p.precio_actual) and p.descuento_pct >= GRAN_ELECTRO_DESCUENTO_MIN:
        score += 30

    # Marca reconocida (hasta 30 pts)
    titulo_lower = p.titulo.lower()
    if any(marca in titulo_lower for marca in _MARCAS_CONOCIDAS):
        score += 30

    # Precio en rango óptimo para reventa/consumo (hasta 15 pts)
    if 30 <= p.precio_actual <= 400:
        score += 15
    elif p.precio_actual <= 600:
        score += 8
    elif p.precio_actual <= 4000:
        score += 5  # bicicletas, eBikes y productos premium de precio alto
    elif PRECIO_MINIMO_LC <= p.precio_actual < PRECIO_MINIMO and p.descuento_pct >= DESCUENTO_LC_MINIMO:
        score += 15  # bonus low-cost: garantiza zona gris (40%+15=25 ≥ _SCORE_AUTO_DESCARTAR)

    # Historial de precio CCC (hasta 15 pts, penalización si inflado)
    if p.precio_historico_min > 0:
        ratio = p.precio_actual / p.precio_historico_min
        if ratio <= 1.0:
            score += 15   # precio mínimo histórico
        elif ratio <= 1.10:
            score += 10
        elif ratio <= 1.15:
            score += 5
        else:
            score -= 10   # precio probablemente inflado

    return max(0, min(score, 100))

# Parte estática del prompt — se cachea en la API (cache_control: ephemeral)
PROMPT_SCORING_SYSTEM = """\
Eres un experto en ofertas y arbitraje de productos en España. Evalúas dos dimensiones independientes:

A) ARBITRAJE: ¿Se puede comprar y revender con beneficio en Wallapop/eBay.es?
B) OFERTA PURA: ¿Es una oferta tan buena que merece publicarse aunque no se pueda revender?
   (producto reconocido, buen descuento, alta demanda de compra directa)

COSTES REALES DE REVENTA:
- Wallapop cobra ~13% (comisión + pasarela)
- Envío + embalaje: ~7€ fijos
- beneficio_neto = precio_wallapop_estimado × 0.87 − precio_actual − 7

Para cada producto devuelve un JSON con EXACTAMENTE estas claves:
- "asin": string (igual que en el input)
- "score_reventa": integer 0-100
- "score_liquidez": integer 0-100 (velocidad venta Wallapop: 100=horas, 50=semanas)
- "score_oferta": integer 0-100 (valor como oferta directa para el consumidor)
- "precio_wallapop_estimado": float (€ en Wallapop segunda mano — MUY CONSERVADOR.
  Reglas obligatorias: marcas premium conocidas (Apple/Sony/Nike...): 60-70% del precio Amazon.
  Marcas chinas o desconocidas (Lefant/Dreame/Cecotec/genéricas): 40-55% del precio Amazon.
  Si no hay mercado claro de segunda mano en España para ese producto, usa 0.
  NUNCA estimes cerca del precio Amazon — Wallapop siempre es bastante más barato.)
- "tipo": string — una de estas tres opciones:
    "ARBITRAJE"  → score_reventa >= 60 Y beneficio_neto >= 20
    "OFERTA"     → score_oferta >= 58 Y descuento >= 40 (aunque reventa sea baja)
    "DESCARTAR"  → ninguna condición cumplida

Score ARBITRAJE alto (>70): smartphones Apple/Samsung, portátiles gaming, PS5/Xbox/Switch,
  sneakers Nike/Adidas/Jordan/New Balance, perfumes Dior/Chanel/YSL/Armani,
  LEGO sets, cámaras mirrorless, relojes G-Shock/Seiko/Citizen,
  auriculares Sony WH/Bose QC/AirPods, herramientas Bosch Pro/DeWalt/Makita.

Score OFERTA alto (>70): cualquier producto de marca reconocida con ≥40% descuento real,
  alta demanda de compra (no solo reventa), buenas reseñas implícitas por la marca.
  Ejemplos: smart TV de marca, robot aspirador Roomba/Roborock, cafetera Nespresso,
  consola gaming, tablet iPad/Samsung, zapatillas de deporte, mochila The North Face.

NOTA IMPORTANTE sobre "descuento_pct" y "precio_original":
  Estos valores ya han sido validados y corregidos contra el historial de CamelCamelCamel antes
  de llegar aquí. Si el precio de referencia original estaba inflado artificialmente, ya se ha
  recalculado usando el precio promedio histórico real. Confía en "descuento_pct" como el
  descuento genuino del producto.

  Aun así: si "precio_historico_min" > 0 y "precio_actual" > "precio_historico_min" × 1.10,
  el producto está por encima de su mínimo histórico — penaliza ambos scores en 10 puntos.

Responde ÚNICAMENTE con un array JSON válido. Sin markdown, sin texto adicional."""


async def score_con_claude(productos: list[Producto]) -> list[Producto]:
    """
    Scoring en dos etapas para minimizar coste de API:
    1. Pre-scorer local (sin IA): auto-aprueba ≥70 pts, descarta <30 pts
    2. Claude Haiku (solo zona gris 30-69 pts): prompt cacheado, output mínimo
    """
    if not productos:
        return []

    # ── Etapa 1: pre-scorer local ─────────────────────────────────
    candidatos: list[Producto] = []
    zona_gris: list[Producto] = []

    for p in productos:
        s = _score_local(p)
        if s >= _SCORE_AUTO_APROBAR:
            titulo_lower = p.titulo.lower()
            if any(m in titulo_lower for m in _MARCAS_ARBITRAJE) and not _es_gran_electrodomestico(p.titulo, p.precio_actual):
                p.tipo         = "ARBITRAJE"
                p.score_ai     = s
                p.razonamiento = "marca premium + descuento alto → reventa viable"
            else:
                p.tipo         = "OFERTA"
                p.score_oferta = s
                p.razonamiento = ""
            p.copy      = _copy_template(p)
            p.categoria = _inferir_categoria(p)
            # Pros básicos para deals auto-aprobados (sin llamada IA)
            p.pros = [f"−{p.descuento_pct}% de descuento real"]
            if any(m in titulo_lower for m in _MARCAS_CONOCIDAS):
                p.pros.append("Marca con garantía oficial")
            if p.precio_historico_min > 0 and p.precio_actual <= p.precio_historico_min:
                p.pros.append("En mínimo histórico de precio")
            candidatos.append(p)
        elif s >= _SCORE_AUTO_DESCARTAR:
            zona_gris.append(p)
        # < _SCORE_AUTO_DESCARTAR → descartado silenciosamente

    descartados = len(productos) - len(candidatos) - len(zona_gris)
    print(f"   🏎️  Auto-aprobados: {len(candidatos)} | Zona gris→Claude: {len(zona_gris)} | Descartados: {descartados}")

    if not zona_gris:
        return candidatos

    # ── Etapa 2: Heurística local (zona gris, sin Claude) ──────────
    # Reglas: marca de arbitraje + desc ≥45% → ARBITRAJE; marca conocida → OFERTA; resto → DESCARTAR
    aprobados_gris = 0
    for p in zona_gris:
        titulo_lower = p.titulo.lower()
        tiene_marca     = any(m in titulo_lower for m in _MARCAS_CONOCIDAS)
        tiene_arbitraje = any(m in titulo_lower for m in _MARCAS_ARBITRAJE)

        es_gran_electro = _es_gran_electrodomestico(p.titulo, p.precio_actual)

        if tiene_arbitraje and p.descuento_pct >= 45 and not es_gran_electro:
            p.tipo     = "ARBITRAJE"
            p.score_ai = _score_local(p)
            p.razonamiento = "marca con mercado de reventa + descuento sólido"
        elif tiene_marca:
            p.tipo         = "OFERTA"
            p.score_oferta = _score_local(p)
            p.razonamiento = ""
        elif es_gran_electro and p.descuento_pct >= GRAN_ELECTRO_DESCUENTO_MIN:
            # Gran electrodoméstico al umbral reducido (30%): se publica como OFERTA
            # aunque la marca no esté en _MARCAS_CONOCIDAS (Balay, Haier, Teka, Candy…).
            p.tipo         = "OFERTA"
            p.score_oferta = _score_local(p)
            p.razonamiento = ""
        elif PRECIO_MINIMO_LC <= p.precio_actual < PRECIO_MINIMO and p.descuento_pct >= DESCUENTO_LC_MINIMO:
            p.tipo         = "OFERTA"
            p.score_oferta = _score_local(p)
            p.categoria    = "low_cost"
            p.razonamiento = f"low cost -{p.descuento_pct}%"
        elif p.tienda in _TIENDAS_FEED_CONFIABLE:
            # Feed curado con historial propio: bajada real ≥40% ya verificada
            p.tipo         = "OFERTA"
            p.score_oferta = _score_local(p)
            p.razonamiento = ""
        else:
            continue  # DESCARTAR silenciosamente

        p.copy      = _copy_template(p)
        p.categoria = _inferir_categoria(p)
        p.pros      = [f"−{p.descuento_pct}% de descuento real"]
        if tiene_marca:
            p.pros.append("Marca con garantía oficial")
        if p.precio_historico_min > 0 and p.precio_actual <= p.precio_historico_min:
            p.pros.append("En mínimo histórico de precio")
        p.contras = []
        candidatos.append(p)
        aprobados_gris += 1

    print(f"   🔧 Zona gris: {aprobados_gris}/{len(zona_gris)} aprobados por heurística local")
    return candidatos

# ════════════════════════════════════════════════════════════════
# FASE 4 — WALLAPOP PRICER (precio de mercado real)
# ════════════════════════════════════════════════════════════════

_WALLAPOP_NOISE_RE = re.compile(
    r'\s+(con\s|para\s|sin\s|color\s|incluye\s|pack\s|set\s|kit\s|y\s[a-záéíóúñ])',
    re.IGNORECASE
)

def _build_wallapop_query(titulo: str) -> str:
    """Query específica para Wallapop: primer segmento + hasta 6 tokens relevantes.
    Ej: "Disco duro SSD interno 2TB PS5" → "Disco duro SSD interno 2TB PS5"
        "Bosch Taladro GSB 21-2 RE con Maletín" → "Bosch Taladro GSB 21-2 RE"
    """
    segment = re.split(r'\s*[·|,–]\s*', titulo.replace(",", ""))[0].strip()
    segment = _WALLAPOP_NOISE_RE.split(segment)[0].strip()
    tokens = segment.split()
    return " ".join(tokens[:6])


def _estimar_precio_wallapop(p: Producto) -> float:
    """Estimación conservadora del precio de reventa (Wallapop bloqueado o sin datos).
    Usa 62% del precio original — por debajo del valor real de mercado para branded items.
    Solo rentable con descuentos reales ≥50% en productos >300€.
    """
    ref = p.precio_original if p.precio_original > p.precio_actual * 1.1 else 0.0
    if ref <= 0:
        return 0.0
    return round(ref * 0.62, 2)


async def obtener_precio_wallapop(p: Producto, context: BrowserContext) -> float:
    """Scrape Wallapop para obtener precio medio de mercado."""
    query = urllib.parse.quote(_build_wallapop_query(p.titulo))
    url = f"https://es.wallapop.com/app/search?keywords={query}&order_by=price_low_to_high"

    page = await context.new_page()
    try:
        await page.goto(url, timeout=35000)
        await _aceptar_cookies(page)
        await asyncio.sleep(3)

        # Verificar bloqueo antes de intentar scraping
        title = await page.title()
        if "error" in title.lower() or "403" in title or "blocked" in title.lower():
            return 0.0

        precios: list[float] = []
        # Wallapop usa web components; buscar precio en múltiples selectores
        for sel in [
            'span[class*="ItemCard__price"]',
            '[class*="price--"]',
            '[data-testid*="price"]',
        ]:
            elementos = await page.locator(sel).all()
            for elem in elementos[:10]:
                try:
                    txt = await elem.inner_text()
                    precio = float(re.sub(r'[^\d,]', '', txt).replace(',', '.'))
                    if precio > 20:
                        precios.append(precio)
                except Exception:
                    pass
            if len(precios) >= 3:
                break

        if len(precios) < 2:
            return 0.0

        # Percentil 25-75 para excluir outliers
        precios.sort()
        n = len(precios)
        muestra = precios[n // 4 : max(n // 4 + 1, 3 * n // 4)] or precios
        return round(sum(muestra) / len(muestra), 2)

    except Exception:
        return 0.0
    finally:
        await page.close()

# ════════════════════════════════════════════════════════════════
# LÍMITE DE PRODUCTOS DEL MISMO TIPO
# ════════════════════════════════════════════════════════════════

_TIPO_PRODUCTO_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(bicicleta|mtb|gravel|e-bike|ebike|bici)\b',        re.I), 'bicicleta'),
    (re.compile(r'\b(auricular|headphone|earbud|earphone|casco\s+audio)',re.I), 'auriculares'),
    (re.compile(r'\b(cafetera|espresso|nespresso|dolce\s*gusto|tassimo)',re.I), 'cafetera'),
    (re.compile(r'\b(maillot|culotte)',                                  re.I), 'maillot'),
    (re.compile(r'\b(robot\s*aspirador|roomba|roborock|dreame|ecovacs|lefant|eufy)', re.I), 'aspirador'),
    (re.compile(r'\b(smartwatch|smart\s+watch|galaxy\s+watch|apple\s+watch|fenix)',  re.I), 'smartwatch'),
    (re.compile(r'\b(port[aá]til|laptop|notebook)\b',                   re.I), 'portatil'),
    (re.compile(r'\btablet\b|ipad',                                     re.I), 'tablet'),
    (re.compile(r'\b(televisor|smart\s*tv|qled|oled)',                  re.I), 'tv'),
    (re.compile(r'\b(afeitadora|rasuradora|recortadora)',                re.I), 'afeitadora'),
    (re.compile(r'\b(plancha|alisador|rizador|secador\s+de?\s+pelo)',    re.I), 'peluqueria'),
    (re.compile(r'\b(freidora|air\s*fryer)',                            re.I), 'freidora'),
    (re.compile(r'\b(mochila|backpack)\b',                              re.I), 'mochila'),
    (re.compile(r'\b(perfume|eau\s+de|colonia)\b',                      re.I), 'perfume'),
    (re.compile(r'\b(casco\s+(?:bici|moto|ciclismo|ski|senderismo))',   re.I), 'casco'),
    # Software/antivirus: distintas licencias del mismo producto deben contarse juntas
    (re.compile(r'\b(antivirus|internet\s+security|total\s+protection|mcafee|norton|kaspersky|bitdefender|avast|avg\b|eset\b|panda\s+dome|trend\s+micro)', re.I), 'antivirus'),
    (re.compile(r'\b(office\s+\d{4}|microsoft\s+365|adobe\s+(?:creative|acrobat)|autocad)', re.I), 'software'),
]


def _detectar_tipo_producto(titulo: str) -> str | None:
    """Detecta la categoría de producto a partir del título para limitar duplicados."""
    for pattern, tipo in _TIPO_PRODUCTO_PATTERNS:
        if pattern.search(titulo):
            return tipo
    return None


_FAMILIA_OEM = {"krups", "delonghi", "philips", "bosch", "siemens", "braun", "rowenta", "magimix", "cecotec", "taurus"}
_FAMILIA_MODEL_RE = re.compile(r'^[a-z]{0,3}[0-9]{2,}[a-z0-9]*$')  # XN9204, ENV90, XN920110DES…
_FAMILIA_STOP = {"de","la","el","los","las","para","con","sin","y","o","en","un","una","del","al","por","sus","wat","ghz","mhz"}


def _clave_familia(titulo: str) -> str:
    """Clave de familia de producto normalizada.

    Normaliza tres casos problemáticos para agrupar variantes del mismo artículo:
    1. Prefijo "Categoría - Marca Modelo" (feeds TD: "Cafetera de cápsulas - Nespresso…")
    2. Marcas OEM intercambiables (Krups/De'Longhi + Nespresso Vertuo Pop)
    3. Sufijos de modelo/color (XN9204, XN9201, ENV90.B, ENV90.A)

    Heurística para " - ": si la parte anterior tiene ≤2 palabras significativas,
    es un prefijo de categoría (TD feed) → descartar y tomar la parte posterior.
    Si tiene ≥3 palabras, es la descripción del producto (Amazon) → procesar todo.
    """

    def _palabras_sig(texto: str) -> list[str]:
        resultado = []
        for w in texto.lower().split():
            w = re.sub(r'[^a-z0-9áéíóúüñ]', '', w)
            if len(w) < 3 or w in _FAMILIA_STOP or w in _FAMILIA_OEM:
                continue
            if _FAMILIA_MODEL_RE.match(w):
                continue
            resultado.append(w)
        return resultado

    if " - " in titulo:
        antes, despues = titulo.split(" - ", 1)
        # ≤2 palabras significativas antes → es prefijo de categoría (TD style)
        if len(_palabras_sig(antes)) <= 2:
            titulo = despues

    palabras = _palabras_sig(titulo)
    return " ".join(palabras[:4])


def _dedup_variantes(deals: list["Producto"]) -> list["Producto"]:
    """Dentro de un mismo ciclo, si hay varias variantes del mismo producto
    (misma familia de título + misma tienda), conserva solo la de mayor score/descuento."""
    from collections import defaultdict
    grupos: dict[str, list["Producto"]] = defaultdict(list)
    sin_familia: list["Producto"] = []

    for p in deals:
        familia = _clave_familia(p.titulo)
        # Solo agrupar si el nombre tiene al menos 2 palabras relevantes
        if len(familia.split()) >= 2:
            grupos[f"{p.tienda}||{familia}"].append(p)
        else:
            sin_familia.append(p)

    resultado: list["Producto"] = list(sin_familia)
    for key, grupo in grupos.items():
        if len(grupo) == 1:
            resultado.extend(grupo)
        else:
            mejor = max(grupo, key=lambda p: (p.score_ai, p.descuento_pct))
            resultado.append(mejor)
            omitidos = [p.titulo[:45] for p in grupo if p is not mejor]
            print(f"   🔁 Dedup variantes '{key.split('||')[1][:35]}': {len(grupo)} → 1 (omitidos: {omitidos})")
    return resultado


def _limitar_por_tipo(deals: list["Producto"]) -> list["Producto"]:
    """Si hay más de MAX_MISMO_TIPO del mismo tipo, conserva solo los MAX_PUBLICAR_POR_TIPO mejores
    (ordenados por score_ai desc, luego descuento_pct desc)."""
    from collections import defaultdict
    por_tipo: dict[str, list["Producto"]] = defaultdict(list)
    sin_tipo: list["Producto"] = []

    for p in deals:
        tipo = _detectar_tipo_producto(p.titulo)
        if tipo:
            por_tipo[tipo].append(p)
        else:
            sin_tipo.append(p)

    resultado: list["Producto"] = list(sin_tipo)
    for tipo, grupo in por_tipo.items():
        if len(grupo) > MAX_MISMO_TIPO:
            grupo_ord = sorted(grupo, key=lambda p: (p.score_ai, p.descuento_pct), reverse=True)
            resultado.extend(grupo_ord[:MAX_PUBLICAR_POR_TIPO])
            print(f"   ✂️  Límite tipo '{tipo}': {len(grupo)} → {MAX_PUBLICAR_POR_TIPO} (omitidos {len(grupo) - MAX_PUBLICAR_POR_TIPO})")
        else:
            resultado.extend(grupo)
    return resultado


# DEDUPLICACIÓN PERSISTENTE (SQLite)
# ════════════════════════════════════════════════════════════════

def _deal_hash(p: "Producto") -> str:
    """Hash MD5 estable que identifica de forma única un deal (ASIN o título+tienda)."""
    clave = f"{p.tienda}:{p.asin or p.titulo[:40].lower()}"
    return hashlib.md5(clave.encode()).hexdigest()


def redirect_url(p: "Producto", canal: str = "telegram") -> str:
    """URL de tracking propio que luego redirige al link de afiliado."""
    return f"{REDIRECT_BASE_URL}/r/{_deal_hash(p)}?canal={canal}"


class DeduplicacionDB:
    """Evita republicar el mismo deal dentro de la ventana TTL y registra clicks."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute("PRAGMA journal_mode=WAL")   # soporta lecturas concurrentes
            con.execute("PRAGMA synchronous=NORMAL") # más rápido, sigue siendo seguro
            con.execute("""
                CREATE TABLE IF NOT EXISTS deals_publicados (
                    deal_id      TEXT PRIMARY KEY,
                    titulo       TEXT,
                    tienda       TEXT,
                    precio       REAL,
                    tipo         TEXT,
                    url_afiliado TEXT,
                    publicado_en TEXT
                )
            """)
            # Migraciones suaves — añadir columnas nuevas sin romper instalaciones existentes
            for col_sql in [
                "ALTER TABLE deals_publicados ADD COLUMN url_afiliado TEXT",
                "ALTER TABLE deals_publicados ADD COLUMN precio_original REAL",
                "ALTER TABLE deals_publicados ADD COLUMN descuento_pct  INTEGER",
                "ALTER TABLE deals_publicados ADD COLUMN imagen_url      TEXT",
                "ALTER TABLE deals_publicados ADD COLUMN precio_wallapop REAL",
                "ALTER TABLE deals_publicados ADD COLUMN beneficio_neto  REAL",
                "ALTER TABLE deals_publicados ADD COLUMN razonamiento    TEXT",
                "ALTER TABLE deals_publicados ADD COLUMN categoria       TEXT DEFAULT ''",
                "ALTER TABLE deals_publicados ADD COLUMN pros            TEXT DEFAULT '[]'",
                "ALTER TABLE deals_publicados ADD COLUMN contras         TEXT DEFAULT '[]'",
                # Discovery layer
                "ALTER TABLE deals_publicados ADD COLUMN deal_score      INTEGER DEFAULT 0",
                "ALTER TABLE deals_publicados ADD COLUMN hook            TEXT    DEFAULT ''",
                "ALTER TABLE deals_publicados ADD COLUMN social_context  TEXT    DEFAULT ''",
                "ALTER TABLE deals_publicados ADD COLUMN emotional_tags  TEXT    DEFAULT '[]'",
                "ALTER TABLE deals_publicados ADD COLUMN stock_qty       INTEGER DEFAULT 0",
                "ALTER TABLE deals_publicados ADD COLUMN pocas_unidades  TEXT    DEFAULT ''",
                "ALTER TABLE deals_publicados ADD COLUMN precio_actualizado_en TEXT DEFAULT NULL",
                "ALTER TABLE deals_publicados ADD COLUMN familia_key     TEXT    DEFAULT ''",
                # Verificación automática de precio a 3/7 días
                "ALTER TABLE deals_publicados ADD COLUMN precio_publicado     REAL",          # precio del 1er descuento (inmutable)
                "ALTER TABLE deals_publicados ADD COLUMN precio_verificado    REAL",          # último precio re-consultado
                "ALTER TABLE deals_publicados ADD COLUMN precio_verificado_en TEXT DEFAULT NULL",
                "ALTER TABLE deals_publicados ADD COLUMN mas_rebajado         INTEGER DEFAULT 0",
                "ALTER TABLE deals_publicados ADD COLUMN verif_3d             INTEGER DEFAULT 0",
                "ALTER TABLE deals_publicados ADD COLUMN verif_7d             INTEGER DEFAULT 0",
                "ALTER TABLE deals_publicados ADD COLUMN tallas               TEXT    DEFAULT ''",  # tallas disponibles (Esdemarca/Desigual)
                # Ficha de producto generada bajo demanda (Haiku) al abrir el detalle
                "ALTER TABLE deals_publicados ADD COLUMN ficha_ia             TEXT    DEFAULT ''",
                "ALTER TABLE deals_publicados ADD COLUMN ficha_generada_en    TEXT    DEFAULT NULL",
                # Clave del producto en price_history (tiendas sin "precio antes" en el feed).
                # Sin ella no podemos volver a preguntarle al histórico si el descuento que
                # anunciamos sigue siendo cierto — y deja de serlo: la referencia envejece.
                "ALTER TABLE deals_publicados ADD COLUMN hist_pid             TEXT    DEFAULT ''",
            ]:
                try:
                    con.execute(col_sql)
                except sqlite3.OperationalError:
                    pass  # columna ya existe
            con.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    asin            TEXT NOT NULL,
                    tienda          TEXT NOT NULL DEFAULT 'Amazon',
                    precio          REAL NOT NULL,
                    precio_original REAL,
                    fecha           TEXT NOT NULL,
                    PRIMARY KEY (asin, tienda, fecha)
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_ph_asin ON price_history(asin, tienda)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS clicks (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id TEXT NOT NULL,
                    canal   TEXT NOT NULL DEFAULT 'desconocido',
                    ip      TEXT,
                    ts      TEXT NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_clicks_deal ON clicks(deal_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_familia_key ON deals_publicados(tienda, familia_key, publicado_en)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS deals_borrados (
                    deal_id    TEXT PRIMARY KEY,
                    titulo     TEXT,
                    tienda     TEXT,
                    precio     REAL,
                    borrado_en TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS wa_suscriptores (
                    telefono   TEXT PRIMARY KEY,  -- formato internacional sin + (ej. 34612345678)
                    nombre     TEXT DEFAULT '',
                    activo     INTEGER DEFAULT 1,
                    alta_en    TEXT NOT NULL,
                    baja_en    TEXT
                )
            """)
            # Promociones/cupones de AWIN (Promotions API) — contenido tipo "promo de tienda"
            con.execute("""
                CREATE TABLE IF NOT EXISTS promociones (
                    promo_id     TEXT PRIMARY KEY,
                    tienda       TEXT,
                    titulo       TEXT,
                    descripcion  TEXT DEFAULT '',
                    codigo       TEXT DEFAULT '',
                    url          TEXT,
                    start_date   TEXT DEFAULT '',
                    end_date     TEXT DEFAULT '',
                    estado       TEXT DEFAULT 'active',
                    capturada_en TEXT,
                    publicada_tg INTEGER DEFAULT 0
                )
            """)
            # Backfill familia_key para registros existentes (migración única)
            sin_familia = con.execute(
                "SELECT deal_id, titulo FROM deals_publicados WHERE familia_key IS NULL OR familia_key = ''"
            ).fetchall()
            if sin_familia:
                for did, tit in sin_familia:
                    con.execute("UPDATE deals_publicados SET familia_key = ? WHERE deal_id = ?",
                                (_clave_familia(tit or ""), did))
                print(f"   🔄 familia_key backfill: {len(sin_familia)} registros")
            # Backfill precio_publicado (migración única): deals históricos toman su
            # precio actual como "primer descuento" base para la verificación 3/7d.
            con.execute(
                "UPDATE deals_publicados SET precio_publicado = precio "
                "WHERE precio_publicado IS NULL AND precio IS NOT NULL"
            )
            con.commit()

    def ya_publicado(self, p: "Producto") -> bool:
        deal_id = _deal_hash(p)
        limite = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_TTL_HORAS)).isoformat()
        with sqlite3.connect(self.db_path) as con:
            # Borrado manual → nunca se republica
            if con.execute(
                "SELECT 1 FROM deals_borrados WHERE deal_id = ?", (deal_id,)
            ).fetchone():
                return True
            # Mismo producto exacto (deal_id): no se republica DENTRO de la ventana TTL
            # (DEDUP_TTL_HORAS = 7 días). Pasado ese plazo se permite reaparecer, para que
            # los buenos chollos vuelvan a la superficie en vez de silenciar el canal.
            # (El historial se conserva siempre; al republicar, marcar_publicado refresca la fila.)
            if con.execute(
                "SELECT 1 FROM deals_publicados WHERE deal_id = ? AND publicado_en > ?",
                (deal_id, limite),
            ).fetchone():
                return True
            # Secondary: mismo título exacto + tienda (Playwright vs feed TD misma tienda).
            if con.execute(
                "SELECT 1 FROM deals_publicados WHERE titulo = ? AND tienda = ? AND publicado_en > ?",
                (p.titulo, p.tienda, limite),
            ).fetchone():
                return True
            # Tertiary: familia normalizada + tienda + precio ±20%.
            # Evita publicar variantes de color/modelo del mismo producto en ciclos distintos.
            familia = _clave_familia(p.titulo)
            if len(familia.split()) >= 2 and p.precio_actual:
                precio_min = p.precio_actual * 0.80
                precio_max = p.precio_actual * 1.20
                if con.execute(
                    """SELECT 1 FROM deals_publicados
                       WHERE tienda = ? AND publicado_en > ?
                       AND precio BETWEEN ? AND ?
                       AND familia_key = ? AND familia_key != ''""",
                    (p.tienda, limite, precio_min, precio_max, familia),
                ).fetchone():
                    return True
            return False

    def actualizar_precio_si_bajo(self, p: "Producto") -> bool:
        """
        Si el deal está en la ventana TTL y el precio bajó ≥1€ o ≥2%,
        actualiza precio, descuento_pct y precio_original en la BD.
        Retorna True si se actualizó algo.
        """
        deal_id = _deal_hash(p)
        limite  = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_TTL_HORAS)).isoformat()
        with sqlite3.connect(self.db_path) as con:
            row = con.execute(
                "SELECT precio, precio_original FROM deals_publicados WHERE deal_id = ? AND publicado_en > ?",
                (deal_id, limite),
            ).fetchone()
            if not row or not row[0]:
                return False
            precio_guardado = float(row[0])
            bajada = precio_guardado - p.precio_actual
            if bajada <= 0:
                return False
            if bajada < 1.0 and (bajada / precio_guardado) < 0.02:
                return False  # bajada insignificante (< 1€ y < 2%)
            # Mantener siempre el precio_original de la primera publicación —
            # cuando el precio baja, la tienda puede mostrar el precio anterior
            # como "tachado", lo que inflaría el descuento artificial (ej. 199→89→79:
            # la tienda tacha 89 en vez de 199, apareciendo solo 11% en lugar de 60%).
            precio_ref = float(row[1]) if row[1] else p.precio_original
            descuento_real = round((1 - p.precio_actual / precio_ref) * 100) if precio_ref else p.descuento_pct
            con.execute(
                """UPDATE deals_publicados
                   SET precio               = ?,
                       descuento_pct        = ?,
                       precio_original      = ?,
                       precio_actualizado_en = ?
                   WHERE deal_id = ?""",
                (p.precio_actual, descuento_real, precio_ref,
                 datetime.now(timezone.utc).isoformat(), deal_id),
            )
            con.commit()
            return True

    def marcar_publicado(self, p: "Producto"):
        cat = p.categoria or _inferir_categoria(p)
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """INSERT OR REPLACE INTO deals_publicados
                       (deal_id, titulo, tienda, precio, tipo, url_afiliado, publicado_en,
                        precio_original, descuento_pct, imagen_url,
                        precio_wallapop, beneficio_neto, razonamiento,
                        categoria, pros, contras,
                        deal_score, hook, social_context, emotional_tags,
                        stock_qty, pocas_unidades, tallas, familia_key, hist_pid, precio_publicado)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        COALESCE((SELECT precio_publicado FROM deals_publicados WHERE deal_id = ?), ?))""",
                (
                    _deal_hash(p), p.titulo, p.tienda, p.precio_actual, p.tipo,
                    p.url_affiliate, datetime.now(timezone.utc).isoformat(),
                    p.precio_original, p.descuento_pct, p.imagen_url or "",
                    p.precio_wallapop, p.beneficio_neto, p.razonamiento or "",
                    cat,
                    json.dumps(p.pros or [], ensure_ascii=False),
                    json.dumps(p.contras or [], ensure_ascii=False),
                    int(p.deal_score or 0),
                    p.hook or "",
                    p.social_context or "",
                    json.dumps(p.emotional_tags or [], ensure_ascii=False),
                    int(p.stock_qty or 0),
                    p.pocas_unidades or "",
                    p.tallas or "",
                    _clave_familia(p.titulo),
                    p.hist_pid or "",
                    # precio_publicado: preserva el 1er descuento si el deal ya existía,
                    # si no usa el precio actual de esta publicación.
                    _deal_hash(p), p.precio_actual,
                ),
            )
            # Registrar precio en historial propio (un registro por día y tienda)
            try:
                fecha_hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                asin_key  = p.asin or p.titulo[:40].lower()
                con.execute(
                    """INSERT OR REPLACE INTO price_history (asin, tienda, precio, precio_original, fecha)
                       VALUES (?, ?, ?, ?, ?)""",
                    (asin_key, p.tienda, p.precio_actual, p.precio_original, fecha_hoy),
                )
            except Exception as e:
                print(f"   ⚠️  price_history insert error: {e}")
            con.commit()

    def limpiar_expirados(self):
        pass  # Conservamos todo el historial — no hay límite de almacenamiento


# ════════════════════════════════════════════════════════════════
# FASE 5 — TELEGRAM PUBLISHER
# ════════════════════════════════════════════════════════════════

def formatear_mensaje(p: Producto) -> str:
    if p.tipo == "ARBITRAJE":
        return _msg_arbitraje(p)
    return _msg_oferta(p)


def _msg_arbitraje(p: Producto) -> str:
    """Formato HTML para deals de arbitraje/reventa."""
    walla_url = f"https://es.wallapop.com/app/search?keywords={urllib.parse.quote(_build_wallapop_query(p.titulo))}"

    ccc_line = ""
    if p.precio_historico_min > 0:
        if p.precio_actual <= p.precio_historico_min:
            ccc_line = "\n🟢 <b>Precio mínimo histórico</b>"
        else:
            diff = round(((p.precio_actual / p.precio_historico_min) - 1) * 100)
            ccc_line = f"\n🟡 Solo un {diff}% sobre el mínimo histórico"

    reventa = ""
    if p.precio_wallapop > 0 and p.beneficio_neto > 0:
        reventa = (
            f"\n\n💰 Precio en Wallapop: ~<b>{p.precio_wallapop:.0f} €</b>"
            f"\n    Puedes ganar hasta <b>+{p.beneficio_neto:.0f} €</b>"
        )

    copy_line = f"\n\n<i>{html.escape(p.copy)}</i>" if p.copy else ""
    links = f'<a href="{p.url_affiliate}">🛒 Comprar en {p.tienda}</a>'
    links += f'  ·  <a href="{walla_url}">🔍 Ver en Wallapop</a>'

    return (
        f"♻️ <b>{html.escape(p.titulo[:80])}</b>\n"
        f"<i>{html.escape(p.tienda)}</i>\n\n"
        f"<s>{p.precio_original} €</s>  →  <b>{p.precio_actual} €</b>  ·  <b>−{p.descuento_pct}%</b>"
        f"{ccc_line}"
        f"{reventa}"
        f"{copy_line}\n\n"
        f"{links}"
    )


def _msg_oferta(p: Producto) -> str:
    """Formato HTML para ofertas puras."""
    ccc_line = ""
    if p.precio_historico_min > 0:
        if p.precio_actual <= p.precio_historico_min:
            ccc_line = "\n🟢 <b>Precio mínimo histórico</b>"
        else:
            diff = round(((p.precio_actual / p.precio_historico_min) - 1) * 100)
            ccc_line = f"\n🟡 Solo un {diff}% sobre el mínimo histórico"

    copy_line = f"\n\n<i>{html.escape(p.copy)}</i>" if p.copy else ""
    links = f'<a href="{p.url_affiliate}">🛒 Comprar en {p.tienda}</a>'

    return (
        f"⚡ <b>{html.escape(p.titulo[:80])}</b>\n"
        f"<i>{html.escape(p.tienda)}</i>\n\n"
        f"<s>{p.precio_original} €</s>  →  <b>{p.precio_actual} €</b>  ·  <b>−{p.descuento_pct}%</b>"
        f"{ccc_line}"
        f"{copy_line}\n\n"
        f"{links}"
    )


def enviar_telegram(mensaje: str, imagen_url: str = "") -> bool:
    try:
        base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
        if imagen_url:
            resp = requests.post(
                f"{base}/sendPhoto",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": imagen_url,
                    "caption": mensaje[:1024],
                    "parse_mode": "HTML",
                },
                timeout=15,
            )
            if resp.ok:
                return True
            # Si la imagen falla (URL inválida, bloqueada, etc.), caer a texto
        resp = requests.post(
            f"{base}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": mensaje,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False


def _msg_promo(tienda: str, titulo: str, codigo: str, url: str) -> str:
    """Mensaje HTML de Telegram para una promo/cupón de tienda."""
    cod = f"\n🏷️ Código: <code>{html.escape(codigo)}</code>" if codigo else ""
    return (
        f"🎟️ <b>{html.escape(tienda)}</b>\n"
        f"{html.escape(titulo)}{cod}\n\n"
        f'👉 <a href="{url}">Ver promoción</a>'
    )


def actualizar_promociones():
    """Refresca la tabla `promociones` desde la AWIN Promotions API y postea en Telegram
    las promos nuevas accionables (con código o % de descuento). La web las sirve todas."""
    try:
        promos = await_safe_fetch_promos()
    except Exception as e:
        print(f"   ⚠️  promos fetch error: {e}")
        return
    if not promos:
        return
    ahora = datetime.now(timezone.utc).isoformat()
    ids = [p["promo_id"] for p in promos if p.get("promo_id")]
    with sqlite3.connect(DB_PATH) as con:
        for p in promos:
            if not p.get("promo_id"):
                continue
            con.execute(
                "INSERT OR IGNORE INTO promociones "
                "(promo_id, tienda, titulo, descripcion, codigo, url, start_date, end_date, estado, capturada_en, publicada_tg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (p["promo_id"], p["tienda"], p["titulo"], p["descripcion"], p["codigo"],
                 p["url"], p["start_date"], p["end_date"], p["estado"], ahora),
            )
            con.execute(
                "UPDATE promociones SET tienda=?, titulo=?, descripcion=?, codigo=?, url=?, "
                "start_date=?, end_date=?, estado=?, capturada_en=? WHERE promo_id=?",
                (p["tienda"], p["titulo"], p["descripcion"], p["codigo"], p["url"],
                 p["start_date"], p["end_date"], p["estado"], ahora, p["promo_id"]),
            )
        # Quitar de la tabla las promos que ya no están activas en la API
        if ids:
            ph = ",".join("?" * len(ids))
            con.execute(f"DELETE FROM promociones WHERE promo_id NOT IN ({ph})", ids)
        # Postear en Telegram las NUEVAS accionables (con código o % en el título), cap 5/ciclo
        nuevas = con.execute(
            "SELECT promo_id, tienda, titulo, codigo, url FROM promociones "
            "WHERE publicada_tg = 0 ORDER BY capturada_en LIMIT 20"
        ).fetchall()
        posteadas = 0
        for pid, tienda, titulo, codigo, url in nuevas:
            accionable = bool(codigo) or bool(re.search(r'\d+\s*%|descuento|gratis|rebajas', titulo, re.I))
            if accionable and posteadas < 5:
                if enviar_telegram(_msg_promo(tienda, titulo, codigo, url)):
                    posteadas += 1
                    time.sleep(1.5)
            # marcar como procesada (accionable posteada, o no-accionable que solo va a web)
            con.execute("UPDATE promociones SET publicada_tg = 1 WHERE promo_id = ?", (pid,))
        con.commit()
    print(f"   🎟️  Promos: {len(promos)} activas en BD · {posteadas} nuevas posteadas en Telegram")


def await_safe_fetch_promos():
    """Promos/cupones combinados: AWIN Promotions + Tradedoubler Vouchers. Cada fetcher
    es resiliente (si falla devuelve []); se combinan para la tabla `promociones`."""
    promos: list[dict] = []
    try:
        promos += fetch_awin_promociones()
    except Exception as e:
        print(f"   ⚠️  AWIN promos fetch: {e}")
    try:
        promos += fetch_td_vouchers()
    except Exception as e:
        print(f"   ⚠️  TD vouchers fetch: {e}")
    return promos


# Tiendas de moda pura: títulos poco fiables (marca+código), ~todo ropa → fuera de Threads.
_TIENDAS_MODA = {
    "Esdemarca", "Desigual", "Billabong", "Cole Haan", "Element Brand",
    "Elliotti", "PrivateSportShop",
}

# Threads es un canal premium: solo los mejores deals para no saturar.
# El score local combina descuento + marca reconocida + rango de precio (relación marca/precio/%).
_THREADS_SCORE_MIN = 70

def _threads_elegible(p: Producto) -> bool:
    """Threads curado: ≥50% descuento, sin ropa, y solo deals de score alto (marca/precio/%)."""
    if (p.descuento_pct or 0) < 50:
        return False
    if getattr(p, "tienda", "") in _TIENDAS_MODA:
        return False
    if _ROPA_RE.search(p.titulo or "") or _TALLA_RE.search(p.titulo or ""):
        return False
    # Solo los mejores: marca top + buen descuento + precio razonable
    if _score_local(p) < _THREADS_SCORE_MIN:
        return False
    return True


def _link_threads(p: Producto) -> str:
    """Link para el post: redirect propio (flipazo.es/r/) cuando el afiliado es un
    tracking link feo (Tradedoubler/Awin); link directo a tienda para el resto (Amazon, etc.)."""
    url = p.url_affiliate or ""
    if "tradedoubler.com" in url or "awin1.com" in url or not url:
        return redirect_url(p, canal="threads")
    return url


def _msg_threads(p: Producto) -> str:
    """Texto plano para Threads. Sin HTML — solo emojis y saltos de línea. Máx 500 chars."""
    icono = "♻️" if p.tipo == "ARBITRAJE" else "⚡"
    ahorro = p.precio_original - p.precio_actual
    lineas = [
        f"{icono} {p.titulo[:80]}",
        f"{p.tienda}",
        "",
        f"{p.precio_original} € → {p.precio_actual} € · -{p.descuento_pct}% (ahorras {ahorro:.0f} €)",
    ]
    if p.hook:
        lineas.append("")
        lineas.append(p.hook[:120])
    lineas += ["", f"🛒 Ver oferta → {_link_threads(p)}", "", "#chollos #ofertas #flipazo"]
    return "\n".join(lineas)[:500]


def _msg_whatsapp(p: Producto) -> str:
    """Formato WhatsApp: negrita con *, cursiva con _, tachado con ~. Máx 4096 chars."""
    icono = "♻️" if p.tipo == "ARBITRAJE" else "⚡"
    ahorro = p.precio_original - p.precio_actual
    lineas = [
        f"{icono} *{p.titulo[:80]}*",
        f"_{p.tienda}_",
        "",
        f"~{p.precio_original} €~  →  *{p.precio_actual} €*  ·  *-{p.descuento_pct}%*",
        f"Ahorras *{ahorro:.0f} €*",
    ]
    if p.hook:
        lineas += ["", p.hook[:150]]
    lineas += ["", f"🛒 Ver oferta: {p.url_affiliate}"]
    if p.tipo == "ARBITRAJE" and p.beneficio_neto > 0:
        lineas += ["", f"💰 Reventa en Wallapop: hasta +{p.beneficio_neto:.0f} € de beneficio"]
    return "\n".join(lineas)


def publicar_en_threads(p: Producto) -> bool:
    """Publica un deal en Threads vía Meta Graph API. Desactivado si no hay credenciales.
    Usa post tipo IMAGE con la foto del producto (la imagen no depende del link preview,
    que falla con los redirects de Tradedoubler). Fallback a TEXT si la imagen falla."""
    if not THREADS_USER_ID or not THREADS_TOKEN:
        return False
    base  = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}"
    texto = _msg_threads(p)

    def _crear(payload: dict) -> str:
        r = requests.post(
            f"{base}/threads",
            params={"access_token": THREADS_TOKEN},
            json=payload,
            timeout=20,
        )
        cid = r.json().get("id") if r.ok else None
        if not cid:
            print(f"⚠️ Threads contenedor ({payload.get('media_type')}): {r.text[:150]}")
        return cid

    try:
        # Paso 1: contenedor IMAGE (con foto del producto) o TEXT como fallback
        creation_id = None
        if p.imagen_url:
            creation_id = _crear({"media_type": "IMAGE", "image_url": p.imagen_url, "text": texto})
        if not creation_id:
            creation_id = _crear({"media_type": "TEXT", "text": texto})
        if not creation_id:
            return False

        # Threads recomienda esperar unos segundos a que procese la imagen antes de publicar
        if p.imagen_url:
            time.sleep(3)

        # Paso 2: publicar
        r2 = requests.post(
            f"{base}/threads_publish",
            params={"access_token": THREADS_TOKEN},
            json={"creation_id": creation_id},
            timeout=20,
        )
        ok = r2.status_code == 200
        if not ok:
            print(f"❌ Threads publicar: {r2.text[:200]}")
        return ok
    except Exception as e:
        print(f"❌ Threads error: {e}")
        return False


def _refresh_threads_token() -> bool:
    """Renueva el long-lived token de Threads (válido 60d) para que no expire.
    El endpoint exige que el token tenga >24h; si es más nuevo, falla en silencio."""
    global THREADS_TOKEN
    if not THREADS_TOKEN:
        return False
    try:
        r = requests.get(
            "https://graph.threads.net/v1.0/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": THREADS_TOKEN},
            timeout=15,
        )
        data  = r.json()
        nuevo = data.get("access_token", "")
        if not nuevo:
            return False  # token <24h o error transitorio → no crítico
        THREADS_TOKEN = nuevo
        # Persistir en .env para que sobreviva reinicios del servicio
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        try:
            with open(env_path) as f:
                lineas = f.readlines()
            for i, ln in enumerate(lineas):
                if ln.startswith("THREADS_TOKEN="):
                    lineas[i] = f"THREADS_TOKEN={nuevo}\n"
                    break
            else:
                lineas.append(f"THREADS_TOKEN={nuevo}\n")
            with open(env_path, "w") as f:
                f.writelines(lineas)
        except Exception as e:
            print(f"⚠️ Threads token renovado en memoria pero no se pudo escribir .env: {e}")
        exp = data.get("expires_in", 0)
        dias = round(int(exp) / 86400) if str(exp).isdigit() else "?"
        print(f"🔄 Threads token renovado (válido {dias} días)")
        return True
    except Exception as e:
        print(f"⚠️ Error renovando Threads token: {e}")
        return False


def _wa_suscriptores() -> list[str]:
    """Devuelve la lista de números suscritos a alertas WA (formato internacional, sin +)."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(
                "SELECT telefono FROM wa_suscriptores WHERE activo=1"
            ).fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []


def _enviar_whatsapp_individual(telefono: str, mensaje: str) -> bool:
    """Envía un mensaje de texto libre a un número vía WhatsApp Cloud API.

    NOTA: los mensajes de negocio iniciados fuera de una ventana de 24h
    requieren plantillas aprobadas por Meta. Para la fase de lanzamiento usamos
    texto libre asumiendo ventana activa (usuario nos ha escrito antes).
    Cuando tengamos plantillas aprobadas, usar message_type='template' aquí.
    """
    try:
        r = requests.post(
            f"https://graph.facebook.com/v20.0/{WA_PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": telefono,
                "type": "text",
                "text": {"body": mensaje},
            },
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"❌ WhatsApp [{telefono}]: {e}")
        return False


def broadcast_whatsapp(p: Producto) -> int:
    """Envía el deal a todos los suscriptores de WhatsApp. Devuelve número de envíos ok."""
    if not WA_PHONE_NUMBER_ID or not WA_TOKEN:
        return 0
    suscriptores = _wa_suscriptores()
    if not suscriptores:
        return 0
    msg = _msg_whatsapp(p)
    ok = 0
    for telefono in suscriptores:
        if _enviar_whatsapp_individual(telefono, msg):
            ok += 1
    if ok:
        print(f"   📱 WhatsApp: {ok}/{len(suscriptores)} enviados")
    return ok


def alertar_admin(titulo: str, detalle: str = ""):
    """
    Envía una alerta de error/aviso al chat personal del admin (TELEGRAM_ADMIN_CHAT_ID).
    Si no está configurado, solo imprime en los logs.
    """
    if not TELEGRAM_ADMIN_CHAT_ID:
        return
    ts = datetime.now().strftime("%d/%m %H:%M")
    texto = f"🚨 <b>Flipazo — {titulo}</b>\n<i>{ts}</i>"
    if detalle:
        # Truncar para no exceder límite de Telegram
        texto += f"\n\n<code>{detalle[:800]}</code>"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception:
        pass  # nunca bloquear el pipeline por fallo de alerta


# ── Vigilante de frescura de feeds ────────────────────────────────
# El 20-jul-2026 El Corte Inglés dejó de registrarse en price_history (entró
# Carrefour en el feed AWIN y empujó a ECI detrás del punto donde la descarga se
# cortaba). Estuvo 2 SEMANAS sin publicar y el pipeline seguía "en verde": nadie
# comprobaba que cada tienda siguiera escribiendo su histórico. Esto lo vigila.
FRESCURA_MAX_DIAS = int(os.getenv("FRESCURA_MAX_DIAS", "3"))
# Días sin que cambie NI UN precio para considerar el feed congelado. Un catálogo
# grande mueve precios cada pocos días; 14 sin un solo cambio es un feed muerto.
ESTANCADO_MAX_DIAS = int(os.getenv("ESTANCADO_MAX_DIAS", "14"))


def _tiendas_con_precios_estancados(con, hoy) -> list[tuple]:
    """
    Tiendas que siguen escribiendo pero cuyos precios no se mueven.

    Se mira la ventana de ESTANCADO_MAX_DIAS: si en ese tiempo el catálogo entero
    tuvo un único precio por producto, el feed está sirviendo una foto fija. Solo
    se evalúan catálogos grandes (≥500 productos), donde tener cero cambios en dos
    semanas es estadísticamente imposible.
    """
    fuera = []
    tablas = [("Decathlon", "decathlon_precios", "model_id"),
              ("ToysRus",   "toysrus_precios",   "ean")]
    for tienda, tabla, clave in tablas:
        try:
            # Días DISTINTOS de datos en la ventana. Sin esto, un histórico recién
            # empezado (1 día) tiene cero cambios por definición y se marcaba como
            # congelado: le pasó a Decathlon al migrar de feed, y un vigilante que
            # da falsos positivos deja de mirarse.
            dias = con.execute(
                f"SELECT COUNT(DISTINCT fecha) FROM {tabla} "
                f"WHERE fecha >= date('now','-{ESTANCADO_MAX_DIAS} day')"
            ).fetchone()[0]
            if (dias or 0) < 7:
                continue                  # aún no hay ventana suficiente para juzgar
            total, cambian = con.execute(
                f"SELECT COUNT(*), SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END) FROM ("
                f"SELECT COUNT(DISTINCT precio) n FROM {tabla} "
                f"WHERE fecha >= date('now','-{ESTANCADO_MAX_DIAS} day') GROUP BY {clave})"
            ).fetchone()
        except sqlite3.Error:
            continue                      # la tabla puede no existir
        if (total or 0) >= 500 and not (cambian or 0):
            fuera.append((f"{tienda} (precios congelados)", hoy.isoformat(), ESTANCADO_MAX_DIAS))
    return fuera

def vigilar_frescura_feeds(db_path: str = DB_PATH) -> list[tuple]:
    """
    Avisa (Telegram admin) de las tiendas que llevan >FRESCURA_MAX_DIAS sin
    registrar precios, señal de que su feed dejó de leerse.

    Solo avisa de tiendas que YA tenían histórico: una tienda nueva sin datos aún
    no es un fallo. Y como máximo un aviso por tienda y día, para no ser ruido.
    """
    try:
        with sqlite3.connect(db_path, timeout=60) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS feed_watchdog (
                    tienda        TEXT PRIMARY KEY,
                    ultimo_aviso  TEXT NOT NULL
                )
            """)
            filas = con.execute(
                "SELECT tienda, MAX(fecha) FROM price_history GROUP BY tienda"
            ).fetchall()
            # Decathlon y ToysRus NO escriben en price_history, tienen sus propias
            # tablas. Sin esto el vigilante los daba por muertos estando sanos —
            # y una alarma que miente es peor que no tenerla: se deja de mirar.
            for tienda, tabla in (("Decathlon", "decathlon_precios"),
                                  ("ToysRus",   "toysrus_precios")):
                try:
                    ult = con.execute(f"SELECT MAX(fecha) FROM {tabla}").fetchone()[0]
                    if ult:
                        filas = [f for f in filas if f[0] != tienda] + [(tienda, ult)]
                except sqlite3.Error:
                    pass   # la tabla puede no existir aún
            hoy    = datetime.now().date()
            hoy_iso = hoy.isoformat()
            avisados = dict(con.execute("SELECT tienda, ultimo_aviso FROM feed_watchdog").fetchall())

            rancias = []
            for tienda, ult in filas:
                if not tienda or not ult:
                    continue
                try:
                    dias = (hoy - datetime.fromisoformat(str(ult)[:10]).date()).days
                except ValueError:
                    continue
                if dias > FRESCURA_MAX_DIAS:
                    rancias.append((tienda, str(ult)[:10], dias))

            # ── Precios ESTANCADOS ───────────────────────────────────────
            # Escribir a diario no basta: el feed de Decathlon (id=98) siguió
            # respondiendo con 364.605 productos durante 57 días, pero con los
            # precios congelados del 14-jun. Guardábamos la misma foto una y otra
            # vez, la tienda dejó de publicar y este vigilante decía que todo
            # bien, porque solo miraba que hubiera datos nuevos.
            rancias += _tiendas_con_precios_estancados(con, hoy)

            nuevas = [r for r in rancias if avisados.get(r[0]) != hoy_iso]
            if nuevas:
                detalle = "\n".join(f"{t}: sin datos desde {f} ({d} días)" for t, f, d in sorted(nuevas, key=lambda x: -x[2]))
                print(f"   🚨 Feeds sin registrar precios ({len(nuevas)}):\n      " + detalle.replace("\n", "\n      "))
                alertar_admin(f"{len(nuevas)} tienda(s) han dejado de registrar precios", detalle)
                con.executemany(
                    "INSERT INTO feed_watchdog (tienda, ultimo_aviso) VALUES (?,?) "
                    "ON CONFLICT(tienda) DO UPDATE SET ultimo_aviso=excluded.ultimo_aviso",
                    [(t, hoy_iso) for t, _, _ in nuevas],
                )
                con.commit()
            elif rancias:
                # Ya avisadas hoy: NO decir "OK". Un falso verde es justo lo que hizo
                # que ECI pasara 2 semanas fuera sin que nadie mirara.
                print(f"   🔇 {len(rancias)} tienda(s) siguen sin registrar precios (ya avisadas hoy): "
                      + ", ".join(f"{t} ({d}d)" for t, _, d in sorted(rancias, key=lambda x: -x[2])))
            else:
                print(f"   ✅ Frescura de feeds OK ({len(filas)} tiendas, ninguna >{FRESCURA_MAX_DIAS} días sin datos)")
            return rancias
    except Exception as e:
        print(f"   ⚠️  Vigilante de frescura falló: {e}")
        return []


# ── IndexNow: notifica a Bing/Yandex (y, vía Bing, a ChatGPT/Copilot) al instante ──
INDEXNOW_KEY  = "b18d9ae0f353b8f3b3b91546ee319570"
INDEXNOW_HOST = "www.flipazo.es"

def _ping_indexnow(urls: list[str]) -> None:
    """Avisa a los buscadores (IndexNow) que estas URLs han cambiado, para re-rastreo inmediato."""
    if not urls:
        return
    try:
        resp = requests.post(
            "https://api.indexnow.org/indexnow",
            json={
                "host":        INDEXNOW_HOST,
                "key":         INDEXNOW_KEY,
                "keyLocation": f"https://{INDEXNOW_HOST}/{INDEXNOW_KEY}.txt",
                "urlList":     urls,
            },
            timeout=15,
        )
        print(f"   🔔 IndexNow: HTTP {resp.status_code} para {len(urls)} URL(s)")
    except Exception as e:
        print(f"   ⚠️  IndexNow error: {e}")  # nunca bloquear el pipeline

# ════════════════════════════════════════════════════════════════
# PIPELINE ORQUESTADOR
# ════════════════════════════════════════════════════════════════

async def run_pipeline(modo: str = "completo"):
    """
    modo="flash"    → solo Amazon /deals, sin CCC, sin Wallapop (rápido)
    modo="completo" → todas las tiendas + CCC + Wallapop (profundo)
    """
    etiqueta = "⚡ FLASH" if modo == "flash" else "🔍 COMPLETO"
    print(f"\n{'═'*55}")
    print(f"  🚀 Flipazo [{etiqueta}]  —  {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"{'═'*55}")

    async with async_playwright() as pw:
        # En servidor Linux: HEADLESS=true (sin pantalla)
        # En local para debug: HEADLESS=false (ver el browser)
        headless = os.getenv("HEADLESS", "true").lower() != "false"

        # UA de Chrome real — evita el "HeadlessChrome" que Cloudflare detecta inmediatamente
        _STEALTH_UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=f"./sesion_flipazo_{modo}",
            headless=headless,
            **({"channel": "chrome"} if not headless else {}),
            user_agent=_STEALTH_UA,
            viewport={"width": 1440, "height": 900},
            locale="es-ES",
            timezone_id="Europe/Madrid",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--disable-http2",
                "--window-size=1440,900",
            ],
        )
        # playwright-stealth parcha ~30 propiedades JS que delatan el headless
        # (webdriver, plugins, chrome runtime, WebGL vendor, canvas fingerprint, etc.)
        await Stealth().apply_stealth_async(browser)

        try:
            # ── Fase 1: Scraping ──────────────────────────────────
            if modo == "flash":
                productos = await scrape_amazon_deals(browser)
            else:
                productos = await scrape_todas_las_tiendas(browser)

            if not productos:
                print("⚠️  Sin productos en este ciclo.")
                return

            # ── Fase 1.5: Mejor precio Amazon para deals no-Amazon ───
            # Para cada deal de otra tienda que tenga un número de modelo claro,
            # comprueba si Amazon tiene el mismo producto más barato.
            # Si sí → actualiza tienda/ASIN/precio para usar el link Amazon (mejor afiliado + precio).
            # El precio_original del deal origen (strike_price regulado EU) se conserva como referencia.
            if modo == "completo":
                # Solo tiendas de electrónica: el cross-check busca en Amazon i=electronics
                # anclado en nº de modelo. En moda/calzado/deportes los códigos SKU colisionan
                # entre productos sin relación → falsos emparejamientos (título de uno, URL de otro).
                no_amazon_raw = [p for p in productos if p.tienda in _CROSSCHECK_AMAZON_TIENDAS]
                if no_amazon_raw:
                    print(f"\n💸 Comprobando precio Amazon para {len(no_amazon_raw)} deals de electrónica...")
                    mejorados = 0
                    for p in no_amazon_raw:
                        try:
                            datos = await _buscar_precio_amazon_mas_barato(
                                p.titulo, p.precio_actual, browser
                            )
                            if not datos and p.tienda in _TIENDAS_MARKETPLACE:
                                # Sin confirmación externa no publicamos: su precio de
                                # lista lo pone el vendedor y puede ser cualquier cosa.
                                print(f"   🚫 {p.tienda} sin verificar en Amazon → descartado: {p.titulo[:40]}")
                                p.descuento_pct = 0
                            if datos:
                                tienda_orig = p.tienda
                                if datos.get("es_mas_barato", True):
                                    # ── Amazon más barato → sustituir deal ──────────
                                    ahorro = round(p.precio_actual - datos["precio_actual"], 2)
                                    p.asin          = datos["asin"]
                                    p.precio_actual = datos["precio_actual"]
                                    if tienda_orig in _TIENDAS_MARKETPLACE:
                                        # NO arrastrar la referencia del marketplace: es
                                        # justo el dato del que desconfiamos. Con el
                                        # max() de abajo, el MX Master habría acabado
                                        # como "Amazon 127€, antes 303,63€, −58%".
                                        p.precio_original = datos["precio_original_amazon"]
                                    elif datos["precio_original_amazon"] > p.precio_actual:
                                        p.precio_original = max(p.precio_original, datos["precio_original_amazon"])
                                    if p.precio_original > 0:
                                        p.descuento_pct = max(0, round((1 - p.precio_actual / p.precio_original) * 100))
                                    if datos["imagen_url"]:
                                        p.imagen_url = datos["imagen_url"]
                                    p.tienda = "Amazon"
                                    mejorados += 1
                                    print(f"   💸 {tienda_orig}→Amazon  −{ahorro}€  {p.titulo[:45]}")
                                else:
                                    # ── Amazon al mismo precio que la «oferta» ───────
                                    # El precio de Amazon ES el precio real de mercado.
                                    # Recalcular descuento usando Amazon como referencia.
                                    amazon_px  = datos["precio_actual"]
                                    amazon_ref = datos["precio_original_amazon"]
                                    ref_real   = amazon_ref if amazon_ref > amazon_px else amazon_px
                                    if ref_real > 0:
                                        desc_real = max(0, round((1 - p.precio_actual / ref_real) * 100))
                                        desc_min  = _descuento_minimo_para(p.titulo, p.precio_actual)
                                        if desc_real < desc_min:
                                            print(
                                                f"   ❌ MSRP inflado {p.tienda} — Amazon {amazon_px:.2f}€"
                                                f" (ref. real {ref_real:.2f}€)"
                                                f" → desc. real {desc_real}%"
                                                f" < {desc_min}%: {p.titulo[:35]}"
                                            )
                                            p.descuento_pct   = 0
                                            p.precio_original = ref_real
                                        else:
                                            print(
                                                f"   ⚠️  Ref. corregida {tienda_orig}"
                                                f" {p.precio_original:.2f}€→{ref_real:.2f}€"
                                                f" (desc. real {desc_real}%): {p.titulo[:35]}"
                                            )
                                            p.precio_original = ref_real
                                            p.descuento_pct   = desc_real
                            await asyncio.sleep(1.2)
                        except Exception as e:
                            print(f"   ⚠️  Amazon price-check skip ({p.titulo[:30]}): {e}")
                    print(f"   ✅ {mejorados}/{len(no_amazon_raw)} deals actualizados a precio Amazon")

                # Descartar deals cuyo descuento real quedó < mínimo tras corrección de referencia MSRP
                # (umbral por producto: 30% para gran electrodoméstico caro, 40% el resto)
                n_msrp = sum(1 for p in productos if p.descuento_pct < _descuento_minimo_para(p.titulo, p.precio_actual))
                if n_msrp:
                    print(f"   🚫 {n_msrp} deal(s) descartados: MSRP inflado (desc. real bajo el mínimo)")
                    productos = [p for p in productos if p.descuento_pct >= _descuento_minimo_para(p.titulo, p.precio_actual)]

                # Re-validar precio mínimo por categoría tras reclasificación a Amazon
                n_antes = len(productos)
                productos = [p for p in productos if _precio_valido_para_categoria(p.titulo, p.precio_actual)]
                descartados = n_antes - len(productos)
                if descartados:
                    print(f"   🚫 {descartados} deal(s) descartados: precio Amazon demasiado bajo para la categoría")

            # ── Fase 2: verificación historial Amazon (Keepa > CCC) ──
            amazon_prods = [p for p in productos if p.tienda == "Amazon"]
            otros_prods  = [p for p in productos if p.tienda != "Amazon"]

            if amazon_prods:
                if KEEPA_API_KEY:
                    # Keepa: HTTP puro, sin Playwright, funciona en FLASH y COMPLETO
                    amazon_verificados = await verificar_con_keepa(amazon_prods)
                elif modo == "completo":
                    # Fallback CCC — solo en COMPLETO (Playwright necesario)
                    amazon_verificados = await verificar_con_ccc(amazon_prods, browser)
                else:
                    amazon_verificados = amazon_prods  # Flash sin Keepa: sin verificar
            else:
                amazon_verificados = amazon_prods

            # ── Fase 2b: Ratio cap para PCBox ──────────────────────
            pcbox_prods     = [p for p in otros_prods if p.tienda == "PCBox"]
            otros_sin_pcbox = [p for p in otros_prods if p.tienda != "PCBox"]
            pcbox_verificados = _filtrar_pcbox_por_ratio(pcbox_prods)

            productos = amazon_verificados + pcbox_verificados + otros_sin_pcbox
            if not productos:
                print("⚠️  Sin productos tras verificación de precios.")
                return

            # ── Fase 3: Scoring con Claude (dual track) ───────────
            print(f"\n🤖 Scoring Claude ({len(productos)} productos)...")
            candidatos = await score_con_claude(productos)

            arbitraje = [p for p in candidatos if p.tipo == "ARBITRAJE"]
            ofertas   = [p for p in candidatos if p.tipo == "OFERTA"]
            print(f"   ♻️  Arbitraje: {len(arbitraje)} | ⚡ Ofertas puras: {len(ofertas)}")

            if not candidatos:
                print("ℹ️  Ningún producto superó los umbrales.")
                return

            # ── Fase 4: Wallapop (solo para track ARBITRAJE) ──────
            deals_finales: list[Producto] = []

            if arbitraje:
                print(f"\n🔍 Wallapop para {len(arbitraje)} candidatos de arbitraje...")
                # El badge de reventa SOLO se muestra con precio real de Wallapop.
                # Si Wallapop está bloqueado (IP datacenter), el deal se publica como
                # OFERTA pura (sin badge ni beneficio inventado). Nada de estimaciones.
                wallapop_disponible = True  # se pone False al primer 403
                for p in arbitraje:
                    precio_w = 0.0
                    if wallapop_disponible:
                        precio_w = await obtener_precio_wallapop(p, browser)
                        if precio_w <= 0:
                            wallapop_disponible = False
                            print("   ⚠️  Wallapop bloqueado — arbitrajes se publican como OFERTA (sin badge)")

                    if precio_w > 0:
                        # DATOS REALES de Wallapop → evaluar margen de reventa
                        p.precio_wallapop = precio_w
                        neto = p.beneficio_neto
                        if neto >= BENEFICIO_NETO_MINIMO or p.score_ai >= 88:
                            deals_finales.append(p)
                            print(f"   🎯 {p.tienda:<12} {p.titulo[:40]:<40} | neto +{neto:.0f}€ (Wallapop real)")
                        else:
                            # Sin margen real → publicar como OFERTA pura (sin badge)
                            p.tipo = "OFERTA"; p.precio_wallapop = 0.0
                            deals_finales.append(p)
                            print(f"   ⚡ {p.tienda:<12} {p.titulo[:40]:<40} | margen {neto:.0f}€ insuf → OFERTA")
                        await asyncio.sleep(2)
                    else:
                        # SIN datos reales de Wallapop → OFERTA pura (sin badge de reventa)
                        p.tipo = "OFERTA"; p.precio_wallapop = 0.0
                        deals_finales.append(p)
                        print(f"   ⚡ {p.tienda:<12} {p.titulo[:40]:<40} | sin Wallapop → OFERTA")

            # Track OFERTA: publicar directamente (no necesitan Wallapop)
            for p in ofertas:
                deals_finales.append(p)
                print(f"   ⚡ OFERTA  {p.tienda:<12} {p.titulo[:40]:<40} | score {p.score_oferta}/100")

            # ── Fase 4.5: Dedup variantes + limitar flood por tipo ──
            antes = len(deals_finales)
            deals_finales = _dedup_variantes(deals_finales)
            deals_finales = _limitar_por_tipo(deals_finales)
            if len(deals_finales) < antes:
                print(f"   ✂️  Flood control: {antes} → {len(deals_finales)} deals")

            # ── Fase 5: Publicar en Telegram ──────────────────────
            dedup = DeduplicacionDB()
            dedup.limpiar_expirados()
            deals_nuevos: list[Producto] = []
            actualizados_precio = 0
            for p in deals_finales:
                if dedup.ya_publicado(p):
                    if dedup.actualizar_precio_si_bajo(p):
                        actualizados_precio += 1
                else:
                    deals_nuevos.append(p)
            omitidos = len(deals_finales) - len(deals_nuevos)
            if omitidos:
                print(f"   ⏭️  {omitidos} deal(s) ya publicados anteriormente — omitidos (sin republicar)")
            if actualizados_precio:
                print(f"   📉 {actualizados_precio} deal(s) con precio actualizado (bajó desde publicación)")

            # ── Marca al frente del título ────────────────────────
            # La marca debe ser lo primero que se lee en Telegram, Threads y web.
            # Se aplica aquí (sobre los que se van a publicar) para cubrir todos los
            # canales a la vez. El dedup usa deal_id (URL/ASIN), no el título, así que
            # reescribir el título no afecta la deduplicación.
            for p in deals_nuevos:
                p.titulo = _marca_al_frente(p.titulo)

            # ── Fase 4.7: Discovery enrichment (Deal Score + Tags) ──
            # Solo enriquecemos deals que se van a publicar — evita gasto Haiku innecesario.
            if deals_nuevos:
                for p in deals_nuevos:
                    p.deal_score     = calcular_deal_score(p, age_hours=0.0)  # frescura máx, recién publicado
                    p.emotional_tags = asignar_tags(p, p.deal_score)
                # generar_hooks_batch() está desactivado a propósito: producía `hook`
                # y `social_context`, el copy que salía en la ficha ("a mitad de su
                # valor", "volvió a bajar tras semanas"). Repetía el descuento que
                # ya está en el badge y en el precio, así que no se muestra ninguno
                # de los dos y la llamada a Haiku sería gasto sin destino.
                # Para reactivarlo: descomentar y volver a pintar los campos en
                # renderDeal() de index.html.
                #
                # try:
                #     await generar_hooks_batch(deals_nuevos)
                # except Exception as e:
                #     print(f"⚠️  Discovery enrichment falló: {e}")

            print(f"\n📢 Publicando {len(deals_nuevos)} deals nuevos en Telegram...")
            publicados = 0
            for p in deals_nuevos:
                # Zona gris: descuento alto pero por debajo del tope de 90%.
                # Puede ser deal real (outlet, liquidación) o error de dato no detectado.
                # Alertamos al admin para revisión manual sin bloquear la publicación.
                if 80 <= p.descuento_pct < 90:
                    alertar_admin(
                        f"⚠️ Descuento inusual {p.descuento_pct}% — revisar",
                        f"{p.titulo}\n"
                        f"Precio: {p.precio_actual:.2f}€ (antes {p.precio_original:.2f}€)\n"
                        f"Tienda: {p.tienda}\nURL: {p.asin}"
                    )
                msg = formatear_mensaje(p)
                ok = enviar_telegram(msg, imagen_url=p.imagen_url)
                print(f"   {'✅' if ok else '❌'} [{p.tipo}] {p.titulo[:50]}")
                if ok:
                    dedup.marcar_publicado(p)
                    publicados += 1
                    # Threads queda reservado SOLO para tuiteratura (hilos narrativos).
                    # Los deals ya NO se publican en Threads (desactivado a propósito).
                    broadcast_whatsapp(p)
                await asyncio.sleep(1.5)

            # IndexNow: si hubo deals nuevos, la home cambió → avisar a Bing/Yandex para re-rastreo
            if publicados > 0:
                _ping_indexnow([f"https://{INDEXNOW_HOST}/"])

            # Promociones/cupones AWIN (solo ciclo completo) — refresca BD + Telegram
            if modo == "completo":
                await asyncio.to_thread(actualizar_promociones)

            print(f"\n🏁 Ciclo {modo}: {publicados}/{len(deals_nuevos)} publicados ({omitidos} omitidos por dedup)")

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"❌ Error fatal [{modo}]: {e}\n{tb}")
            alertar_admin(f"Error fatal en ciclo {modo.upper()}", f"{e}\n\n{tb[:600]}")
            raise
        finally:
            await browser.close()


async def main():
    """
    Dos loops concurrentes:
    - Flash (cada 30 min): solo Amazon deals, para pillar lightning deals
    - Completo (cada 2h): todas las tiendas con pipeline completo
    """
    print("🚀 Flipazo iniciado. Ctrl+C para detener.")
    await asyncio.gather(
        _loop_flash(),
        _loop_completo(),
        _loop_refresh_threads(),
    )


async def _loop_flash():
    """Ciclo rápido: solo Amazon /deals, sin CCC ni Wallapop."""
    while True:
        try:
            await run_pipeline(modo="flash")
        except Exception as e:
            print(f"❌ [FLASH] Error: {e}")
        print(f"\n⚡ Próximo ciclo flash en {CICLO_FLASH_MIN} min...")
        await asyncio.sleep(CICLO_FLASH_MIN * 60)


async def _loop_completo():
    """Ciclo completo: todas las tiendas + CCC + Wallapop."""
    await asyncio.sleep(60)  # Arrancar 1 min después del flash para no solapar browser
    while True:
        try:
            await run_pipeline(modo="completo")
        except Exception as e:
            print(f"❌ [COMPLETO] Error: {e}")
        print(f"\n💤 Próximo ciclo completo en {CICLO_COMPLETO_MIN} min...")
        await asyncio.sleep(CICLO_COMPLETO_MIN * 60)


async def _loop_refresh_threads():
    """Renueva el token de Threads cada 24h para que nunca expire (válido 60d).
    El token recién creado necesita >24h, por eso el primer intento es a las 24h."""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            _refresh_threads_token()
        except Exception as e:
            print(f"⚠️ [THREADS REFRESH] Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
