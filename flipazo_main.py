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
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

from affiliate.link_builder import build_affiliate_url
from scrapers.pss_email import get_pss_event_urls
from scrapers.tradedoubler_feed import fetch_tradedoubler_productos

load_dotenv()

# ── Credenciales (desde .env) ────────────────────────────────────
TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY")
REDIRECT_BASE_URL    = os.getenv("REDIRECT_BASE_URL", "https://flipazo.es")  # dominio propio para /r/{id}
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")  # chat personal para alertas de error

# ── Umbrales Track A: ARBITRAJE (reventa) ────────────────────────
DESCUENTO_MINIMO        = 37    # % mínimo (bajado de 40→37 para capturar deals como POC 39%)
PRECIO_MINIMO           = 25.0  # € mínimo producto
PRECIO_MAXIMO           = 800.0 # € máximo — permite PS5, MacBook, TV OLED, etc.
SCORE_ARBITRAJE_MINIMO  = 60    # Score reventa mínimo para ir a Wallapop
BENEFICIO_NETO_MINIMO   = 20.0  # € margen neto real mínimo para publicar
RATIO_HISTORICO_MAX     = 1.20  # Precio actual <= 120% del mínimo histórico CCC (era 1.15)
RATIO_PRECIO_REF_INFLADO = 1.25 # Si precio_original > 125% del promedio histórico → referencia inflada artificialmente

# ── Umbrales Track B: OFERTA PURA (sin reventa) ──────────────────
SCORE_OFERTA_MINIMO     = 58    # Score calidad/valor mínimo
DESCUENTO_OFERTA_MINIMO = 37    # % mínimo para ofertas puras (sincronizado con DESCUENTO_MINIMO)

# ── Pipeline ─────────────────────────────────────────────────────
BATCH_SIZE_CLAUDE       = 15    # Productos por llamada a la API
DEBUG_SCREENSHOTS       = os.getenv("DEBUG_SCREENSHOTS", "false").lower() == "true"

# ── Deduplicación ────────────────────────────────────────────────
DB_PATH         = "flipazo_deals.db"
DEDUP_TTL_HORAS = 96   # No republica el mismo deal hasta pasadas 96h (4 días)

# ── Scheduling ───────────────────────────────────────────────────
# Loop rápido: Amazon flash deals cada 60 min
# Loop completo: Todas las tiendas cada 2h (análisis profundo)
CICLO_FLASH_MIN         = 60
CICLO_COMPLETO_MIN      = 120

# ── Costes reales de reventa en Wallapop ─────────────────────────
WALLAPOP_COMISION       = 0.13  # 13% (10% comisión + ~3% pasarela de pago)
WALLAPOP_ENVIO          = 5.0   # € envío medio (Correos/MRW)

# ── Límite de productos del mismo tipo por ciclo ─────────────────
MAX_MISMO_TIPO          = 3     # Si hay más de X del mismo tipo → limitar
MAX_PUBLICAR_POR_TIPO   = 2     # Cuántos publicar cuando se supera el límite
WALLAPOP_EMBALAJE       = 2.0   # € materiales embalaje
WALLAPOP_COSTES_FIJOS   = WALLAPOP_ENVIO + WALLAPOP_EMBALAJE  # 7€

# ── URLs de categorías con mayor potencial de reventa ────────────
AMAZON_SEARCH_URLS = [
    # Electrónica general — Samsung, LG, Sony, Xiaomi (keyword requerido para ≥40% filter)
    "https://www.amazon.es/s?i=electronics&k=samsung+lg+sony+xiaomi+philips+panasonic&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Informática — portátiles y accesorios de marca
    "https://www.amazon.es/s?i=computers&k=lenovo+hp+dell+asus+acer+microsoft+surface&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Videojuegos y consolas
    "https://www.amazon.es/s?i=videogames&k=nintendo+switch+playstation+xbox+ps5+juego&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Zapatillas de marca — alta reventa (Nike, Adidas, New Balance, Jordan)
    "https://www.amazon.es/s?i=shoes&k=jordan+nike+air+max+adidas+ultraboost+new+balance+990+550+asics+gel&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Perfumes — alta reventa y liquidez
    "https://www.amazon.es/s?i=beauty&k=perfume+eau+de+parfum+eau+de+toilette&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # LEGO — sube de precio tras descatalogación
    "https://www.amazon.es/s?k=LEGO&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Herramientas de marca premium (Bosch, DeWalt, Makita, Milwaukee)
    "https://www.amazon.es/s?i=diy&k=bosch+dewalt+makita+milwaukee+karcher+stanley&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Auriculares premium y gaming (AirPods, Sony, Bose, HyperX, JBL, Jabra)
    "https://www.amazon.es/s?i=electronics&k=airpods+sony+wh-1000+bose+quietcomfort+hyperx+jbl+jabra+sennheiser&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Relojes de marca — Casio, Seiko, G-Shock, Citizen, Fossil
    "https://www.amazon.es/s?i=watches&k=casio+seiko+g-shock+citizen+fossil+garmin+polar&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Electrodomésticos de cocina (Nespresso, DeLonghi, Tefal, Kenwood)
    "https://www.amazon.es/s?i=kitchen&k=nespresso+delonghi+kenwood+kitchenaid+krups&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Hogar y electrodomésticos grandes (Dyson, Samsung, LG, Bosch)
    "https://www.amazon.es/s?i=appliances&k=dyson+samsung+lg+bosch+siemens+whirlpool&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Juguetes (Playmobil, Hasbro, Mattel)
    "https://www.amazon.es/s?i=toys&k=playmobil+hasbro+mattel+hot+wheels&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Cámaras y fotografía — Canon, Nikon, Sony, GoPro
    "https://www.amazon.es/s?i=photo&k=canon+nikon+sony+gopro+fujifilm+olympus&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Deporte y fitness — Garmin, Fitbit, Polar, relojes deportivos
    "https://www.amazon.es/s?i=sports&k=garmin+fitbit+polar+xiaomi+amazfit+suunto&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Salud y cuidado personal — afeitadoras Braun/Philips, cepillos Oral-B, depiladores
    "https://www.amazon.es/s?i=hpc&k=braun+philips+oral-b+remington+wahl+panasonic&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Pequeño electrodoméstico — aspiradoras, planchas, freidoras
    "https://www.amazon.es/s?i=kitchen&k=rowenta+shark+bissell+tefal+delonghi+cecotec&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Altavoces Bluetooth portátiles — Sony SRS, JBL Charge/Flip, Bose SoundLink, Marshall, Anker Soundcore
    # Keyword corto (tipo producto + marcas) para evitar que Amazon lo trate como AND estricto
    "https://www.amazon.es/s?i=electronics&k=altavoz+bluetooth+sony+jbl+bose+marshall+anker&rh=p_n_pct-off-with-tax%3A2388626011&s=exact-aware-popularity-rank",
    # Amazon Deals — página principal de ofertas por tiempo limitado
    "https://www.amazon.es/deals?ref=nav_cs_gb",
]

# Página principal de deals (fuente extra, JS-heavy)
AMAZON_DEALS_URL = "https://www.amazon.es/deals"

# ── El Corte Inglés — categorías ordenadas por % descuento ───────
ECI_URLS = [
    "https://www.elcorteingles.es/ofertas/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/ofertas-tecnoprecios-1/electronica/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/exclusivo-online-nike/deportes/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/deportes/montana/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/informatica-videojuegos/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/electrodomesticos/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/belleza/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/ninos/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/juguetes/?sorting=discountPerDesc",
    "https://www.elcorteingles.es/hogar/?sorting=discountPerDesc",
]

# ── MediaMarkt — búsquedas ordenadas por descuento (más estables que category IDs) ───
MEDIAMARKT_URLS = [
    "https://www.mediamarkt.es/es/search.html?query=televisor&sort=discountPercentage_desc",
    "https://www.mediamarkt.es/es/search.html?query=portatil&sort=discountPercentage_desc",
    "https://www.mediamarkt.es/es/search.html?query=smartphone&sort=discountPercentage_desc",
    "https://www.mediamarkt.es/es/search.html?query=auriculares&sort=discountPercentage_desc",
    "https://www.mediamarkt.es/es/search.html?query=tablet&sort=discountPercentage_desc",
    "https://www.mediamarkt.es/es/search.html?query=robot+aspirador&sort=discountPercentage_desc",
]

# ── PcComponentes — ofertas especiales ordenadas por % descuento ──
# La página usa React (SPA): esperar networkidle antes de evaluar el DOM
PCCOMPONENTES_URLS = [
    "https://www.pccomponentes.com/ofertas-especiales?sort=discount",
    "https://www.pccomponentes.com/ofertas-especiales?sort=discount&page=2",
    "https://www.pccomponentes.com/ofertas-especiales?sort=discount&page=3",
    "https://www.pccomponentes.com/componentes?sort=discount",
    "https://www.pccomponentes.com/portatiles?sort=discount",
]

# ── Decathlon — todas las secciones de deals, ordenadas por mayor descuento ──
DECATHLON_URLS = [
    "https://www.decathlon.es/es/deals/descuentos-hombre?Ns=discountRateDescending",
    "https://www.decathlon.es/es/deals/descuentos-mujer?Ns=discountRateDescending",
    "https://www.decathlon.es/es/deals/descuentos-infantil?Ns=discountRateDescending",
    "https://www.decathlon.es/es/deals/descuentos-esqui?Ns=discountRateDescending",
    "https://www.decathlon.es/es/deals/descuentos-ciclismo?Ns=discountRateDescending",
    "https://www.decathlon.es/es/deals/descuentos-running?Ns=discountRateDescending",
    "https://www.decathlon.es/es/deals/descuentos-deportes-agua?Ns=discountRateDescending",
    "https://www.decathlon.es/es/deals?Ns=discountRateDescending",
]

# ── Fnac — secciones de oferta ────────────────────────────────────
FNAC_URLS = [
    "https://www.fnac.es/Ofertas-Especiales/s",
    "https://www.fnac.es/c/Informatica/s",
    "https://www.fnac.es/c/Videojuegos/s",
    "https://www.fnac.es/c/Telefonia-Tablets/s",
    "https://www.fnac.es/c/Imagen-Sonido/s",
]

# ── Worten — secciones de oferta ─────────────────────────────────
WORTEN_URLS = [
    "https://www.worten.es/ofertas",
    "https://www.worten.es/televisores",
    "https://www.worten.es/audio",
    "https://www.worten.es/smartphones-y-telefonia",
    "https://www.worten.es/informatica",
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
    "https://www.mammothbikes.com/outlet/shimano/o-3190",
    "https://www.mammothbikes.com/outlet/alpinestars/o-3191",
    "https://www.mammothbikes.com/outlet/giro/o-3192",
    "https://www.mammothbikes.com/outlet/bicis-iniciacion/o-3193",
    # Accesorios y componentes — mayor rotación y precio en rango 25-800€
    "https://www.mammothbikes.com/outlet/cascos/o-2858",
    "https://www.mammothbikes.com/outlet/componentes/o-2860",
    "https://www.mammothbikes.com/outlet/accesorios/o-2861",
    "https://www.mammothbikes.com/outlet/nutricion/o-2862",
]
# Bicicletas pueden superar los 800€ del PRECIO_MAXIMO general
PRECIO_MAXIMO_BICI = 5000.0

# ── Palabras prohibidas — productos sin potencial de reventa ─────
PALABRAS_PROHIBIDAS = [
    # Accesorios de bajo valor
    "funda", "case", "carcasa", "cristal", "correa", "cable", "tóner",
    "repuesto", "adhesivo", "soporte", "cargador", "adaptador",
    "stylus", "pellicola", "protector",
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
    # Recambios y repuestos (ya está "repuesto" — añadir plurales y variantes)
    "recambio", "recambios",
    # Consumibles de impresora
    "cartucho de tinta", "cartucho de tóner", "kit de tinta",
    # Pilas sueltas (baterías como producto principal, no accesorios de otro artículo)
    "pack de pilas", "pilas alcalinas", "pilas recargables",
    # Periféricos de bajo valor
    "hub usb", "ladrón usb",
    # Bases y docks de carga sueltos
    "base de carga", "estación de carga",
    # Organizadores (no son producto de valor)
    "organizador de cables", "organizador de escritorio",
    # Consumibles de limpieza/jardín
    "manguera",
]

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
    if any(r in t for r in _MAMMOTH_ROPA) and descuento < 55:
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
    for i, url in enumerate(AMAZON_SEARCH_URLS):
        es_deals = "/deals" in url
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        categoria = "deals" if es_deals else parsed.get("i", parsed.get("k", [f"cat{i}"]))[0]
        print(f"\n📡 Categoría: {categoria} ...")
        try:
            ok = await _cargar_con_reintento(page, url, f"Amazon/{categoria}")
            if not ok:
                continue

            if es_deals:
                await asyncio.sleep(6)
                await _scroll_pagina(page, veces=5)
                await asyncio.sleep(3)
                nuevos = await _extraer_de_deals(page, vistos)
            else:
                await _scroll_pagina(page, veces=5)
                nuevos = await _extraer_de_busqueda(page, vistos)

            if DEBUG_SCREENSHOTS and i == 0:
                await page.screenshot(path=f"debug_{categoria}.png")
                print(f"   📸 Screenshot: debug_{categoria}.png")

            productos.extend(nuevos)
            print(f"   ✅ {len(nuevos)} productos nuevos | Total: {len(productos)}")

        except Exception as e:
            print(f"   ❌ Error en {categoria}: {e}")
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
_CIRCUIT_BREAKER_MINUTOS = 60  # Skip la tienda 60 min tras 3 fallos consecutivos
_store_fail_count: dict[str, int] = {}


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
    alertar_admin(f"Scraper bloqueado: {store}", f"No accesible tras {max_intentos} intentos.\nURL: {url}")

    # Circuit breaker: acumular fallos y bloquear si supera el umbral
    _store_fail_count[store_key] = _store_fail_count.get(store_key, 0) + 1
    if _store_fail_count[store_key] >= 3:
        _store_block_until[store_key] = datetime.now() + timedelta(minutes=_CIRCUIT_BREAKER_MINUTOS)
        print(f"   🔴 [{store_key}] Circuit breaker activado — pausando {_CIRCUIT_BREAKER_MINUTOS}min")

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

            # Tope de descuento: >90% sin badge externo es siempre un error de precio por unidad/kg.
            # Los descuentos reales de Amazon raramente superan el 80-85%.
            if descuento > 90:
                continue

            if descuento < DESCUENTO_MINIMO or not (PRECIO_MINIMO <= precio_actual <= PRECIO_MAXIMO):
                continue

            # Título
            titulo = ""
            for sel in ['h2 span', 'h2 a span', '.a-text-normal']:
                loc = card.locator(sel)
                if await loc.count() > 0:
                    titulo = (await loc.first.inner_text()).strip()
                    if len(titulo) > 10:
                        break

            if not titulo or not _es_producto_valido(titulo, descuento):
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

        # Precio original: tachado
        original_loc = card.locator('span.a-price.a-text-strike span.a-offscreen, span.a-text-price span.a-offscreen')
        if await original_loc.count() > 0:
            txt = await original_loc.first.inner_text()
            precio_original = float(re.sub(r'[^\d,]', '', txt).replace(',', '.'))

        # Sanity check: Amazon a veces pone precio por kg/litro/unidad en span.a-text-price.
        # Si precio_original es >10x el precio actual, es casi seguro un precio por unidad de medida
        # y no un precio de referencia real. Lo descartamos para que el badge % sea la fuente de verdad.
        if precio_actual > 0 and precio_original > precio_actual * 10:
            precio_original = 0.0

    except Exception:
        pass
    return precio_actual, precio_original


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
            if not match_desc or int(match_desc.group(1)) < DESCUENTO_MINIMO:
                continue
            descuento = int(match_desc.group(1))

            precios = re.findall(r'(\d+[.,]\d{2})\s*€', texto)
            if not precios:
                continue
            precio_actual = float(precios[0].replace(',', '.'))
            if not (PRECIO_MINIMO <= precio_actual <= PRECIO_MAXIMO):
                continue
            precio_original = (
                float(precios[1].replace(',', '.')) if len(precios) > 1
                else round(precio_actual / (1 - descuento / 100), 2)
            )

            # Título desde primer texto largo del elemento
            titulo = next(
                (t.strip() for t in texto.split('\n') if len(t.strip()) > 20),
                ""
            )
            if not titulo or not _es_producto_valido(titulo, descuento):
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
])


def _es_producto_valido(titulo: str, descuento_pct: int = 0) -> bool:
    t = titulo.lower()
    if any(p in t for p in PALABRAS_PROHIBIDAS):
        return False
    if _TALLA_RE.search(titulo):
        return False
    # Ropa de moda/deporte: solo si marca conocida + descuento real ≥50%
    if any(r in t for r in _PALABRAS_ROPA):
        if descuento_pct < 50 or not any(m in t for m in _MARCAS_ROPA):
            return False
    # Cecotec: marca de gama baja con precios de referencia inflados — solo descuentos fuertes
    if "cecotec" in t and descuento_pct < 60:
        return False
    return True


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
# SCRAPERS ADICIONALES — MediaMarkt y PcComponentes
# ════════════════════════════════════════════════════════════════

async def scrape_mediamarkt(context: BrowserContext) -> list[Producto]:
    """
    Scrape de MediaMarkt.es — campañas y categorías clave.
    MediaMarkt usa styled-components con clases ofuscadas que cambian frecuentemente.
    Estrategia: anclar en a[href*="/product/"] (URL estable) y extraer
    datos via JS DOM traversal en lugar de depender de class names.
    """
    print(f"\n📡 MediaMarkt: {len(MEDIAMARKT_URLS)} URLs")
    page = await context.new_page()
    productos: list[Producto] = []
    hrefs_vistos: set[str] = set()
    try:
        for url in MEDIAMARKT_URLS:
            try:
                ok = await _cargar_con_reintento(page, url, "MediaMarkt")
                if not ok:
                    continue

                await asyncio.sleep(random.uniform(3, 5))

                # Aceptar cookies específicas de MediaMarkt (OneTrust / banner propio)
                for cookie_sel in [
                    '#onetrust-accept-btn-handler',
                    'button[id*="accept"]',
                    'button[class*="accept"]',
                    'button:has-text("Aceptar todo")',
                    'button:has-text("Aceptar")',
                    'button:has-text("Accept")',
                ]:
                    try:
                        if await page.locator(cookie_sel).first.is_visible(timeout=1500):
                            await page.locator(cookie_sel).first.click(timeout=2000)
                            await asyncio.sleep(1.0)
                            break
                    except Exception:
                        pass

                # Esperar a que carguen los productos — MediaMarkt usa React SSR async
                # Probamos dos selectores: el clásico /es/product/ y el alternativo /product/
                _product_loaded = False
                for sel in ['a[href*="/es/product/"]', 'a[href*="/product/"]']:
                    try:
                        await page.wait_for_selector(sel, timeout=20000)
                        _product_loaded = True
                        break
                    except Exception:
                        pass
                if not _product_loaded:
                    # Último recurso: espera fija extra por si el JS tarda
                    await asyncio.sleep(8)

                await _scroll_pagina(page, veces=6)
                await asyncio.sleep(2.5)

                if DEBUG_SCREENSHOTS:
                    await page.screenshot(path=f"debug_mediamarkt_{url.split('/')[-1]}.png")

                items = await page.evaluate("""
                    () => {
                        const BASE = 'https://www.mediamarkt.es';
                        const resultados = [];
                        const vistos = new Set();

                        // Intentar selector específico primero, luego genérico
                        const links = document.querySelectorAll('a[href*="/es/product/"]').length > 0
                            ? document.querySelectorAll('a[href*="/es/product/"]')
                            : document.querySelectorAll('a[href*="/product/"]');

                        links.forEach(link => {
                            const href = link.href || (BASE + link.getAttribute('href'));
                            if (vistos.has(href)) return;
                            vistos.add(href);

                            let el = link;
                            for (let i = 0; i < 8; i++) {
                                el = el.parentElement;
                                if (!el) break;
                                const txt = el.innerText || '';
                                if (txt.includes('€') && txt.length < 800) {
                                    const title = link.getAttribute('title')
                                        || link.getAttribute('aria-label')
                                        || link.innerText.trim().split('\\n')[0];
                                    const img = el.querySelector('img[src]');
                                    const imagen = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
                                    resultados.push({ href, title: title.trim(), text: txt, imagen });
                                    break;
                                }
                            }
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

                        if descuento < DESCUENTO_MINIMO or not (PRECIO_MINIMO <= precio_actual <= PRECIO_MAXIMO):
                            continue

                        productos.append(Producto(
                            titulo=titulo[:120],
                            asin=href,
                            precio_actual=precio_actual,
                            precio_original=precio_original if precio_original > 0 else round(precio_actual / (1 - descuento / 100), 2),
                            descuento_pct=descuento,
                            tienda="MediaMarkt",
                            imagen_url=item.get("imagen", ""),
                        ))
                    except Exception:
                        continue
            except Exception as e:
                print(f"   ⚠️ Error en {url}: {e}")
                continue

        print(f"   ✅ {len(productos)} ofertas de MediaMarkt ({len(MEDIAMARKT_URLS)} URLs)")
    except Exception as e:
        print(f"   ❌ Error MediaMarkt: {e}")
    finally:
        await page.close()
    return productos


async def scrape_pccomponentes(context: BrowserContext) -> list[Producto]:
    """
    Scrape de PcComponentes.com — campañas y categorías clave.
    PcComponentes bloquea fetches directos (403) pero Playwright con sesión
    persistente suele funcionar. Usa JS traversal igual que MediaMarkt
    para resistir cambios de HTML.
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

                items = await page.evaluate("""
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

                        if descuento < DESCUENTO_MINIMO or not (PRECIO_MINIMO <= precio_actual <= PRECIO_MAXIMO):
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
            except Exception as e:
                print(f"   ⚠️ Error en {url}: {e}")
                continue

        print(f"   ✅ {len(productos)} ofertas de PcComponentes ({len(PCCOMPONENTES_URLS)} URLs)")
    except Exception as e:
        print(f"   ❌ Error PcComponentes: {e}")
    finally:
        await page.close()
    return productos


async def _scrape_tienda_generica(
    context: BrowserContext,
    urls: list[str],
    nombre: str,
    patron_url: str,
    max_niveles: int = 8,
) -> list[Producto]:
    """
    Scraper genérico basado en JS DOM traversal.
    Itera sobre todas las URLs de la tienda, acumulando productos.
    Ancla en links de producto que contengan `patron_url` y sube hasta encontrar precios.
    """
    page = await context.new_page()
    productos: list[Producto] = []
    hrefs_vistos: set[str] = set()  # dedup global entre URLs
    try:
        for url in urls:
            try:
                ok = await _cargar_con_reintento(page, url, nombre)
                if not ok:
                    continue

                await asyncio.sleep(random.uniform(2, 4))
                await _scroll_pagina(page, veces=5)
                await asyncio.sleep(1.5)

                if DEBUG_SCREENSHOTS:
                    slug = nombre.lower().replace(" ", "_")
                    await page.screenshot(path=f"debug_{slug}.png")

                items = await page.evaluate(f"""
                    () => {{
                        const resultados = [];
                        const vistos = new Set();
                        document.querySelectorAll('a[href*="{patron_url}"]').forEach(link => {{
                            const href = link.href;
                            if (!href || vistos.has(href) || href.length < 30) return;
                            vistos.add(href);
                            let el = link;
                            for (let i = 0; i < {max_niveles}; i++) {{
                                el = el.parentElement;
                                if (!el) break;
                                const txt = el.innerText || '';
                                if (txt.includes('€') && txt.length < 1000) {{
                                    const title = link.getAttribute('title')
                                        || link.getAttribute('aria-label')
                                        || link.innerText.trim().split('\\n')[0];
                                    const img = el.querySelector('img[src]');
                                    const imagen = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
                                    if (title && title.length > 5)
                                        resultados.push({{ href, title: title.trim(), text: txt, imagen }});
                                    break;
                                }}
                            }}
                        }});
                        return resultados;
                    }}
                """)

                slug_url = url.split('/')[-1] or url.split('/')[-2]
                if len(items) == 0:
                    titulo_pag = await page.title()
                    print(f"   📦 0 productos en {slug_url} (título pág: '{titulo_pag[:80]}')")
                else:
                    print(f"   📦 {len(items)} productos en {slug_url}")

                for item in items:
                    try:
                        href  = item.get("href", "")
                        if href in hrefs_vistos:
                            continue
                        hrefs_vistos.add(href)
                        txt   = item.get("text", "")
                        titulo = item.get("title", "")[:120]
                        if not titulo or not _es_producto_valido(titulo):
                            continue
                        precios = re.findall(r'(\d+[.,]\d{2})\s*€', txt)
                        if len(precios) < 2:
                            continue
                        precio_actual   = min(float(p.replace(',', '.')) for p in precios)
                        precio_original = max(float(p.replace(',', '.')) for p in precios)
                        if precio_actual <= 0 or precio_original <= precio_actual:
                            continue
                        descuento = round((1 - precio_actual / precio_original) * 100)
                        if descuento < DESCUENTO_MINIMO or not (PRECIO_MINIMO <= precio_actual <= PRECIO_MAXIMO):
                            continue
                        productos.append(Producto(
                            titulo=titulo,
                            asin=href,
                            precio_actual=precio_actual,
                            precio_original=precio_original,
                            descuento_pct=descuento,
                            tienda=nombre,
                            imagen_url=item.get("imagen", ""),
                        ))
                    except Exception:
                        continue
            except Exception as e:
                print(f"   ⚠️ Error en {url}: {e}")
                continue

        print(f"   ✅ {len(productos)} ofertas de {nombre} ({len(urls)} URLs)")
    except Exception as e:
        print(f"   ❌ Error {nombre}: {e}")
    finally:
        await page.close()
    return productos


async def scrape_decathlon(context: BrowserContext) -> list[Producto]:
    print(f"\n📡 Decathlon: {len(DECATHLON_URLS)} URLs")
    return await _scrape_tienda_generica(
        context,
        urls=DECATHLON_URLS,
        nombre="Decathlon",
        patron_url="/es/p/",
    )


async def scrape_fnac(context: BrowserContext) -> list[Producto]:
    print(f"\n📡 Fnac: {len(FNAC_URLS)} URLs")
    return await _scrape_tienda_generica(
        context,
        urls=FNAC_URLS,
        nombre="Fnac",
        patron_url="/a",
    )


async def scrape_worten(context: BrowserContext) -> list[Producto]:
    print(f"\n📡 Worten: {len(WORTEN_URLS)} URLs")
    return await _scrape_tienda_generica(
        context,
        urls=WORTEN_URLS,
        nombre="Worten",
        patron_url="/products/",
    )


async def scrape_elcorteingles(context: BrowserContext) -> list[Producto]:
    """
    Scrape de ofertas de El Corte Inglés — múltiples categorías ordenadas por descuento.
    ECI usa React con SSR parcial. Anclamos en a[href*="/p/"] (patrón estable de producto).
    """
    print(f"\n📡 El Corte Inglés: {len(ECI_URLS)} URLs")
    return await _scrape_tienda_generica(
        context,
        urls=ECI_URLS,
        nombre="ElCorteIngles",
        patron_url="/p/",
        max_niveles=10,
    )


async def _pss_warm_up(context: BrowserContext) -> bool:
    """
    Visita la homepage de PSS para obtener la cookie cf_clearance de Cloudflare.
    Espera hasta 45s a que el JS challenge se resuelva.
    Con sesión persistente, si la cookie no ha expirado esta función termina en ~2s.
    """
    page = await context.new_page()
    try:
        print("   🔑 [PSS] Calentando sesión Cloudflare en homepage...")
        await page.goto("https://www.privatesportshop.es/", timeout=60000, wait_until="domcontentloaded")
        for _ in range(9):
            await asyncio.sleep(5)
            try:
                titulo = (await page.title()).lower()
                contenido = await page.content()
                cf_resuelto = (
                    len(contenido) > 5000
                    and not any(s in titulo for s in ["just a moment", "cloudflare", "verificación", "verification"])
                    and "#cf-challenge-running" not in contenido
                )
                if cf_resuelto:
                    print("   ✅ [PSS] Cloudflare resuelto — cf_clearance en sesión")
                    return True
            except Exception:
                pass
        print("   ⚠️  [PSS] Cloudflare no resolvió en 45s")
        return False
    except Exception as e:
        print(f"   ❌ [PSS] Error en warm-up: {e}")
        return False
    finally:
        await page.close()


async def scrape_privatesportshop(context: BrowserContext, urls: list[str] | None = None) -> list[Producto]:
    """
    Scrape de Private Sport Shop — páginas de evento extraídas del newsletter.
    Visita cada URL de evento (ej: /event/adidas-terrex) con Playwright.
    Warm-up previo en homepage para obtener cf_clearance de Cloudflare.
    """
    if not urls:
        return []
    print(f"\n📡 Private Sport Shop: {len(urls)} evento(s) del newsletter")

    # Warm-up: obtener cf_clearance antes de visitar páginas de evento
    cf_ok = await _pss_warm_up(context)
    if not cf_ok:
        print("   ⚠️  [PSS] Warm-up fallido — se intentará igualmente (cookie puede seguir válida)")
    await asyncio.sleep(random.uniform(3, 6))
    productos: list[Producto] = []
    vistos_href: set[str] = set()

    for evento_url in urls:
        page = await context.new_page()
        try:
            await page.set_extra_http_headers({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            })

            nombre_evento = evento_url.rstrip("/").split("/")[-1]
            print(f"   📡 PSS evento: {nombre_evento}")

            ok = await _cargar_con_reintento(page, evento_url, f"PSS/{nombre_evento}", max_intentos=2)
            if not ok:
                continue

            await asyncio.sleep(random.uniform(3, 5))
            await _scroll_pagina(page, veces=8)
            await asyncio.sleep(2)

            if DEBUG_SCREENSHOTS:
                await page.screenshot(path=f"debug_pss_{nombre_evento}.png")

            items = await page.evaluate("""
                () => {
                    const resultados = [];
                    const vistos = new Set();
                    const BASE = 'https://www.privatesportshop.es';

                    // PSS event pages: productos con links que contienen /p- o números
                    document.querySelectorAll('a[href]').forEach(link => {
                        const href = link.href || (BASE + link.getAttribute('href'));
                        if (!href || vistos.has(href)) return;
                        // Filtrar solo links de producto (tienen segmento con número al final)
                        if (!/\\/[^/]+-\\d+(?:\\/)?$|\\/p-\\d+|\\/[^/]+\\.html|\\/catalog\\/product\\/view\\/id\\/\\d+/.test(href)) return;
                        if (href.includes('/event/') || href.includes('/category/')) return;
                        vistos.add(href);

                        // Subir hasta encontrar contenedor con precio
                        let el = link;
                        for (let i = 0; i < 8; i++) {
                            el = el.parentElement;
                            if (!el) break;
                            const txt = el.innerText || '';
                            if (txt.includes('€') && txt.length > 10 && txt.length < 600) {
                                const img = el.querySelector('img[src]');
                                const title = link.getAttribute('title')
                                    || link.getAttribute('aria-label')
                                    || (img ? img.getAttribute('alt') : '')
                                    || link.innerText.trim().split('\\n')[0];
                                if (title && title.length > 5) {
                                    resultados.push({
                                        href,
                                        title: title.trim(),
                                        text: txt,
                                        imagen: img ? (img.getAttribute('src') || '') : '',
                                    });
                                }
                                break;
                            }
                        }
                    });
                    return resultados;
                }
            """)

            print(f"   📦 {len(items)} productos encontrados en {nombre_evento}")

            for item in items:
                try:
                    txt   = item.get("text", "")
                    titulo = item.get("title", "")[:120]
                    href  = item.get("href", "")

                    if not titulo or not href or href in vistos_href:
                        continue
                    if not _es_producto_valido(titulo):
                        continue

                    precios = re.findall(r'(\d+[.,]\d{2})\s*€', txt)
                    if len(precios) < 2:
                        continue
                    precio_actual   = min(float(p.replace(',', '.')) for p in precios)
                    precio_original = max(float(p.replace(',', '.')) for p in precios)
                    if precio_actual <= 0 or precio_original <= precio_actual:
                        continue

                    descuento = round((1 - precio_actual / precio_original) * 100)
                    if descuento < DESCUENTO_MINIMO or not (PRECIO_MINIMO <= precio_actual <= PRECIO_MAXIMO):
                        continue

                    vistos_href.add(href)
                    productos.append(Producto(
                        titulo=titulo,
                        asin=href,
                        precio_actual=precio_actual,
                        precio_original=precio_original,
                        descuento_pct=descuento,
                        tienda="PrivateSportShop",
                        imagen_url=item.get("imagen", ""),
                    ))
                except Exception:
                    continue

        except Exception as e:
            print(f"   ❌ Error PSS evento {evento_url}: {e}")
        finally:
            await page.close()
            await asyncio.sleep(2)

    print(f"   ✅ {len(productos)} ofertas totales de Private Sport Shop")
    return productos


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

    def _parse_precio_es(texto_precio: str) -> float:
        """Convierte '2.899,00' o '229,95' → float."""
        return float(texto_precio.replace(".", "").replace(",", "."))

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

                        if descuento < DESCUENTO_MINIMO:
                            continue
                        if not (PRECIO_MINIMO <= precio_actual <= PRECIO_MAXIMO):
                            continue
                        if not _es_producto_valido(titulo, descuento):
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
        scrape_mediamarkt,
        scrape_pccomponentes,
        scrape_decathlon,
        # scrape_fnac,           # ❌ IP bloqueada (39 chars) — pendiente feed Awin
        scrape_worten,
        scrape_elcorteingles,
        scrape_mammoth,
        scrape_barrabes,
        # scrape_privatesportshop,  # ❌ Cloudflare duro — pendiente solución feed
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

    # ── PSS: extraer URLs del newsletter + scrape con Playwright ──
    try:
        pss_urls = get_pss_event_urls()
        if pss_urls:
            pss_productos = await scrape_privatesportshop(context, urls=pss_urls)
            for p in pss_productos:
                clave = f"PrivateSportShop:{p.titulo[:40].lower()}"
                if clave not in vistos:
                    vistos.add(clave)
                    todos.append(p)
    except Exception as e:
        print(f"   ❌ Error en scraper PSS: {e}")
        alertar_admin("Error en scraper PSS", str(e))

    # ── Tradedoubler feeds (MediaMarkt/ToysRus/Beep) — caché 23h ──────────────
    try:
        td_raw = await asyncio.to_thread(
            fetch_tradedoubler_productos, DESCUENTO_MINIMO, PRECIO_MINIMO, PRECIO_MAXIMO
        )
        for d in td_raw:
            if not _es_producto_valido(d["titulo"], d["descuento_pct"]):
                continue
            p = Producto(**d)
            clave = f"{p.tienda}:{p.titulo[:40].lower()}"
            if clave not in vistos:
                vistos.add(clave)
                todos.append(p)
    except Exception as e:
        print(f"   ❌ Error en Tradedoubler feeds: {e}")
        alertar_admin("Error en Tradedoubler feeds", str(e))

    print(f"\n✅ Total: {len(todos)} productos únicos de {len({p.tienda for p in todos})} tiendas")
    return todos


# ════════════════════════════════════════════════════════════════
# FASE 2 — CAMELCAMELCAMEL (verificación precio histórico)
#
#  CCC embebe los datos de la gráfica como JSON (Chart.js) en el HTML.
#  Buscamos el precio mínimo histórico del canal "Amazon" (venta directa).
# ════════════════════════════════════════════════════════════════

async def verificar_con_ccc(
    productos: list[Producto], context: BrowserContext
) -> list[Producto]:
    """
    Filtra y corrige productos usando historial de CamelCamelCamel.
    Dos comprobaciones:
      1. Precio de referencia inflado: si precio_original > promedio_histórico × 1.25,
         recalcula el descuento real. Si es < DESCUENTO_MINIMO → descuento falso → descarta.
      2. Precio actual demasiado caro vs mínimo histórico: si actual > mínimo × 1.15 → descarta.
    """
    print(f"\n📊 Verificando historial en CamelCamelCamel ({len(productos)} productos)...")
    verificados: list[Producto] = []

    for p in productos:
        min_h, avg_h = await _scrape_ccc(p.asin, context)
        p.precio_historico_min = min_h

        if min_h > 0:
            # ── Comprobación 1: referencia de precio inflada artificialmente ──────
            # Usamos el promedio (no el mínimo) como proxy del precio "normal".
            # Si precio_original excede el promedio histórico en >25%, el vendedor
            # infló el precio de referencia para simular un descuento mayor.
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
                # Hay descuento genuino aunque la referencia esté inflada:
                # corregimos precio_original y descuento_pct para mostrar honestamente
                print(
                    f"   ⚠️  Ref. corregida {p.precio_original}€→{ref_normal}€ "
                    f"(desc. real {descuento_real}%): {p.titulo[:38]}"
                )
                p.precio_original = ref_normal
                p.descuento_pct   = max(0, descuento_real)

            # ── Comprobación 2: precio actual vs mínimo histórico ────────────────
            ratio = p.precio_actual / min_h
            if ratio <= RATIO_HISTORICO_MAX:
                print(f"   ✅ {p.titulo[:45]:<45} | {p.precio_actual}€ (hist. mín {min_h}€ / avg {ref_normal}€)")
                verificados.append(p)
            else:
                print(f"   ❌ Precio actual {ratio:.2f}x del mínimo histórico: {p.titulo[:40]}")
        else:
            # Sin historial CCC (producto nuevo o sin datos) → dejar pasar
            print(f"   ⚠️  Sin historial CCC: {p.titulo[:45]}")
            verificados.append(p)

        await asyncio.sleep(1.5)  # Respetar rate limit de CCC

    print(f"✅ {len(verificados)} productos con precio verificado")
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
}

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
    "calzado":      re.compile(
        r'\bnike\b|\badidas\b|\bjordan\b|new balance|asics|puma\b|reebok|converse\b|\bvans\b|'
        r'saucony\b|brooks\b|on running|mizuno\b|skechers\b|'
        r'zapatilla|sneaker|deportiva\b|\bbota\b|sandalia\b|mocas[ií]n|calzado\b|'
        # marcas outdoor/trail (solo si aparece con términos de zapato/bota)
        r'salomon\b|hoka\b|merrell\b',
        re.I),
    "tecnologia":   re.compile(
        r'smartphone|m[oó]vil|iphone|galaxy\b|tablet|ipad|port[aá]til|laptop|macbook|'
        r'pc gaming|monitor\b|televisor|\btv\b|oled|qled|auricular|cascos|airpods|'
        r'wh-?1000|bose\s*q|kindle|c[aá]mara\b|gopro|smartwatch|consola\b|ps5|playstation|'
        r'xbox|nintendo|switch\b|ssd|disco duro|\bram\b|gpu|rtx|procesador|impresora|'
        r'router|logitech|razer|corsair|steelseries|hyperx|teclado\b|rat[oó]n\b|'
        r'altavoz.*bluetooth|echo dot|google home|chromecast|fire\s*tv|'
        r'power\s*bank|bater[ií]a.*externa|usb\s*hub|hub\s*usb',
        re.I),
    "herramientas": re.compile(
        r'dewalt|makita|milwaukee|k[aä]rcher|stanley\b|ryobi\b|bahco\b|knipex\b|'
        r'martillo|taladro|sierra\b|lijadora|compresor|soldad|atornillador|amoladora|'
        r'destornillador|nivel.*l[aá]ser|multim[eé]tro|flex[oó]metro|llave inglesa|'
        r'alicate|bosch.*(taladro|sierra|amoladora|compresor|atornillador|lijadora|gbh|gsr|gks|gws)',
        re.I),
    "deportes":     re.compile(
        r'bicicleta\b|\bbici\b|ciclismo|mountain bike|\bmtb\b|gravel\b|\btrek\b|'
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
        r'plancha\b|plancha.*vapor|vaporeta|vaporizador|cepillo.*vapor|vapor.*cepillo|'
        r'campana\b|campana.*extract|extractor.*humos|extractor.*cocina|\bteka\b|'
        r'batidora|thermomix|olla.*presi[oó]n|robot.*cocina|'
        r'tefal|rowenta|shark\b|hoover\b|dyson|cecotec|bissell\b|kenwood\b|magimix\b|'
        r'calefactor|radiador.*el[eé]ctrico|aire.*acondicionado|\bsplit\b|ventilador\b|'
        r'purificador.*aire|humidificador|deshumidificador|'
        r'placa.*inducci[oó]n|inducci[oó]n\b|vitrocer[aá]mic|\bhorno\b|'
        r'colch[oó]n|l[aá]mpara|sill[oó]n|sof[aá]|escritorio|estanter[ií]a',
        re.I),
    "belleza":      re.compile(
        r'perfume|colonia|eau de|fragancia|m[aá]quillaje|labial|'
        r'crema.*facial|crema.*corporal|crema.*hidratante|s[eé]rum.*facial|'
        r'\bdior\b|\bchanel\b|\barmani\b|ysl\b|calvin klein|hugo boss|'
        r'lanc[oô]me|loreal|l\'or[eé]al|nivea|olay\b|est[eé]e lauder|'
        r'afeitadora|maquinilla.*afeit|rasuradora|cepillo.*dental|irrigador.*bucal|'
        r'depilador|epilador|'
        r'oral.?b|remington\b|wahl\b|babyliss|ghd\b|'
        r'plancha.*pelo|rizador|secador.*pelo|cortapelos|recortadora.*barba|recortadora.*pelo',
        re.I),
    "juguetes":     re.compile(
        r'playmobil|\blego\b|hasbro|mattel|hot wheels|barbie|funko\b|'
        r'juguete|juego de mesa|puzzle|puzle|scalextric|\bnerf\b|'
        r'rc\b.*coche|coche.*teledirigido|coche.*radiocontrol|'
        r'\bdron\b|\bdrone\b',
        re.I),
    "moda":         re.compile(
        r'mochila|bolso\b|cartera\b|maleta\b|lacoste\b|ralph lauren|tommy hilfiger|'
        r'gafas.*sol|gafas.*graduada|cintur[oó]n\b',
        re.I),
}
_TIENDA_CAT = {
    "PcComponentes": "tecnologia",   # solo componentes/periféricos — OK como fallback
    # MediaMarkt, Worten y Beep venden tecnología Y electrodomésticos Y belleza:
    # no usar como fallback de categoría — dejar que _CAT_RE decida o asignar "otras"
    "Decathlon":     "deportes",
    "Mammoth Bikes": "deportes",
    "ToysRus":       "juguetes",
}


def _inferir_categoria(p: "Producto") -> str:
    """Asigna una categoría al producto basándose en título y tienda."""
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
    elif p.descuento_pct >= 35:
        score += 10

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
    "OFERTA"     → score_oferta >= 58 Y descuento >= 35 (aunque reventa sea baja)
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
            if any(m in titulo_lower for m in _MARCAS_ARBITRAJE):
                p.tipo         = "ARBITRAJE"
                p.score_ai     = s
                p.razonamiento = "marca premium + descuento alto → reventa viable"
            else:
                p.tipo         = "OFERTA"
                p.score_oferta = s
                p.razonamiento = "descuento alto + marca reconocida"
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

    # ── Etapa 2: Claude Haiku (solo zona gris) — prompt cacheado ──
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    for i in range(0, len(zona_gris), BATCH_SIZE_CLAUDE):
        batch = zona_gris[i : i + BATCH_SIZE_CLAUDE]

        payload = [
            {
                "asin": p.asin,
                "titulo": p.titulo,
                "precio_actual": p.precio_actual,
                "precio_original": p.precio_original,
                "descuento_pct": p.descuento_pct,
                "precio_historico_min": p.precio_historico_min,
            }
            for p in batch
        ]

        # Retry con backoff exponencial (1s → 2s → 4s)
        response = None
        for intento in range(3):
            try:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4096,
                    system=[{
                        "type": "text",
                        "text": PROMPT_SCORING_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{
                        "role": "user",
                        "content": f"Analiza estos {len(batch)} productos:\n{json.dumps(payload, ensure_ascii=False, indent=2)}",
                    }],
                )
                break
            except Exception as e:
                espera = 2 ** intento
                print(f"   ⚠️  Claude API error (intento {intento+1}/3): {e} — reintentando en {espera}s")
                if intento < 2:
                    await asyncio.sleep(espera)
                else:
                    print(f"   ❌ Claude API: 3 intentos fallidos — batch omitido")

        if response is None:
            continue

        try:
            texto = response.content[0].text.strip()
            texto = re.sub(r'^```json\s*|\s*```$', '', texto, flags=re.MULTILINE).strip()

            # Rescatar JSON truncado: recortar hasta el último objeto completo
            if '}' in texto:
                texto = texto[:texto.rfind('}')+1]
                # Cerrar el array si quedó abierto por truncación
                if texto.lstrip().startswith('[') and not texto.rstrip().endswith(']'):
                    texto += ']'

            scores: list[dict] = json.loads(texto)
            scores_by_asin = {s["asin"]: s for s in scores}

            uso = response.usage
            cache_hit = getattr(uso, 'cache_read_input_tokens', 0) or 0
            cache_new = getattr(uso, 'cache_creation_input_tokens', 0) or 0
            print(f"   💰 Tokens: {uso.input_tokens}in/{uso.output_tokens}out | caché hit={cache_hit} nuevo={cache_new}")

            for p in batch:
                s = scores_by_asin.get(p.asin, {})
                p.score_ai       = int(s.get("score_reventa", 0))
                p.score_liquidez = int(s.get("score_liquidez", 0))
                p.score_oferta   = int(s.get("score_oferta", 0))
                p.tipo           = s.get("tipo", "DESCARTAR")
                p.categoria      = _inferir_categoria(p)
                if p.precio_wallapop == 0.0:
                    p.precio_wallapop = float(s.get("precio_wallapop_estimado", 0.0))

                if p.tipo in ("ARBITRAJE", "OFERTA"):
                    titulo_lower = p.titulo.lower()
                    p.pros = [f"−{p.descuento_pct}% de descuento real"]
                    if any(m in titulo_lower for m in _MARCAS_CONOCIDAS):
                        p.pros.append("Marca con garantía oficial")
                    if p.precio_historico_min > 0 and p.precio_actual <= p.precio_historico_min:
                        p.pros.append("En mínimo histórico de precio")
                    p.contras = []
                    candidatos.append(p)

            print(f"   🤖 Haiku batch {i//BATCH_SIZE_CLAUDE + 1}: {len(candidatos)} candidatos acumulados")

        except (json.JSONDecodeError, KeyError) as e:
            print(f"   ⚠️  Error parseando respuesta Claude: {e}")

    return candidatos

# ════════════════════════════════════════════════════════════════
# FASE 4 — WALLAPOP PRICER (precio de mercado real)
# ════════════════════════════════════════════════════════════════

async def obtener_precio_wallapop(p: Producto, context: BrowserContext) -> float:
    """
    Scrape Wallapop para obtener precio medio de mercado.
    Usa las primeras 3 palabras del título para la búsqueda.
    """
    palabras = p.titulo.replace(",", "").split()[:3]
    query = urllib.parse.quote(" ".join(palabras))
    url = f"https://es.wallapop.com/app/search?keywords={query}&order_by=price_low_to_high"

    page = await context.new_page()
    try:
        await page.goto(url, timeout=35000)
        await _aceptar_cookies(page)
        await asyncio.sleep(3)

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
]


def _detectar_tipo_producto(titulo: str) -> str | None:
    """Detecta la categoría de producto a partir del título para limitar duplicados."""
    for pattern, tipo in _TIPO_PRODUCTO_PATTERNS:
        if pattern.search(titulo):
            return tipo
    return None


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


def redirect_url(p: "Producto") -> str:
    """URL de tracking propio que luego redirige al link de afiliado."""
    return f"{REDIRECT_BASE_URL}/r/{_deal_hash(p)}?canal=telegram"


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
            con.commit()

    def ya_publicado(self, p: "Producto") -> bool:
        deal_id = _deal_hash(p)
        limite = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_TTL_HORAS)).isoformat()
        with sqlite3.connect(self.db_path) as con:
            if con.execute(
                "SELECT 1 FROM deals_publicados WHERE deal_id = ? AND publicado_en > ?",
                (deal_id, limite),
            ).fetchone():
                return True
            # Secondary check: mismo título exacto + tienda en TTL.
            # Evita duplicados entre Playwright y feed TD (misma tienda, URL diferente).
            return bool(con.execute(
                "SELECT 1 FROM deals_publicados WHERE titulo = ? AND tienda = ? AND publicado_en > ?",
                (p.titulo, p.tienda, limite),
            ).fetchone())

    def marcar_publicado(self, p: "Producto"):
        cat = p.categoria or _inferir_categoria(p)
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """INSERT OR REPLACE INTO deals_publicados
                       (deal_id, titulo, tienda, precio, tipo, url_afiliado, publicado_en,
                        precio_original, descuento_pct, imagen_url,
                        precio_wallapop, beneficio_neto, razonamiento,
                        categoria, pros, contras)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _deal_hash(p), p.titulo, p.tienda, p.precio_actual, p.tipo,
                    p.url_affiliate, datetime.now(timezone.utc).isoformat(),
                    p.precio_original, p.descuento_pct, p.imagen_url or "",
                    p.precio_wallapop, p.beneficio_neto, p.razonamiento or "",
                    cat,
                    json.dumps(p.pros or [], ensure_ascii=False),
                    json.dumps(p.contras or [], ensure_ascii=False),
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
    walla_query = urllib.parse.quote(" ".join(p.titulo.split()[:4]))
    walla_url = f"https://es.wallapop.com/app/search?keywords={walla_query}"

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

        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=f"./sesion_flipazo_{modo}",
            headless=headless,
            # channel="chrome" solo en local con Chrome instalado
            # En servidor se usa el Chromium bundled de Playwright
            **({"channel": "chrome"} if not headless else {}),
            viewport={"width": 1440, "height": 900},
            locale="es-ES",
            timezone_id="Europe/Madrid",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",              # Necesario en Linux sin root
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",             # Sin GPU en servidor
                "--disable-http2",           # Fuerza HTTP/1.1 — evita ERR_HTTP2_PROTOCOL_ERROR en ECI
            ],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        # Parche de fingerprint: aplicado al CONTEXTO para que todas las páginas nuevas lo hereden
        await browser.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-ES', 'es', 'en']});
            window.chrome = { runtime: {} };
            const orig = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : orig(p);
        """)

        try:
            # ── Fase 1: Scraping ──────────────────────────────────
            if modo == "flash":
                productos = await scrape_amazon_deals(browser)
            else:
                productos = await scrape_todas_las_tiendas(browser)

            if not productos:
                print("⚠️  Sin productos en este ciclo.")
                return

            # ── Fase 2: CCC solo para productos de Amazon ─────────
            amazon_prods = [p for p in productos if p.tienda == "Amazon"]
            otros_prods  = [p for p in productos if p.tienda != "Amazon"]

            if amazon_prods and modo == "completo":
                amazon_verificados = await verificar_con_ccc(amazon_prods, browser)
            else:
                amazon_verificados = amazon_prods  # Flash: saltar CCC para velocidad

            productos = amazon_verificados + otros_prods
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
                for p in arbitraje:
                    precio_w = await obtener_precio_wallapop(p, browser)
                    if precio_w > 0:
                        p.precio_wallapop = precio_w
                    neto = p.beneficio_neto
                    if neto >= BENEFICIO_NETO_MINIMO or p.score_ai >= 88:
                        deals_finales.append(p)
                        print(f"   🎯 {p.tienda:<12} {p.titulo[:40]:<40} | neto +{neto:.0f}€ ({p.roi:.0f}% ROI)")
                    else:
                        print(f"   📉 {p.tienda:<12} {p.titulo[:40]:<40} | neto {neto:.0f}€ insuf.")
                    await asyncio.sleep(2)

            # Track OFERTA: publicar directamente (no necesitan Wallapop)
            for p in ofertas:
                deals_finales.append(p)
                print(f"   ⚡ OFERTA  {p.tienda:<12} {p.titulo[:40]:<40} | score {p.score_oferta}/100")

            # ── Fase 4.5: Limitar flood del mismo tipo de producto ──
            antes = len(deals_finales)
            deals_finales = _limitar_por_tipo(deals_finales)
            if len(deals_finales) < antes:
                print(f"   ✂️  Flood control: {antes} → {len(deals_finales)} deals")

            # ── Fase 5: Publicar en Telegram ──────────────────────
            dedup = DeduplicacionDB()
            dedup.limpiar_expirados()
            deals_nuevos = [p for p in deals_finales if not dedup.ya_publicado(p)]
            omitidos = len(deals_finales) - len(deals_nuevos)
            if omitidos:
                print(f"   ⏭️  {omitidos} deal(s) ya publicados en las últimas {DEDUP_TTL_HORAS}h — omitidos")

            print(f"\n📢 Publicando {len(deals_nuevos)} deals nuevos en Telegram...")
            publicados = 0
            for p in deals_nuevos:
                msg = formatear_mensaje(p)
                ok = enviar_telegram(msg, imagen_url=p.imagen_url)
                print(f"   {'✅' if ok else '❌'} [{p.tipo}] {p.titulo[:50]}")
                if ok:
                    dedup.marcar_publicado(p)
                    publicados += 1
                await asyncio.sleep(1.5)

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


if __name__ == "__main__":
    asyncio.run(main())
