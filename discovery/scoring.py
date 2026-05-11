"""
discovery/scoring.py — Deal Score determinista + asignación de tags emocionales.

Sin IA, sin coste. Llamado para cada deal después del scoring normal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flipazo_main import Producto


# ── Brands con "buzz" cultural — usadas para ⚡ Internet Favorite ──────────────
# Subset reducido del whitelist completo, solo marcas con peso emocional fuerte.
_BUZZ_BRANDS = frozenset({
    "apple", "iphone", "ipad", "macbook", "airpods", "airtag",
    "playstation", "ps5", "xbox", "nintendo", "switch",
    "lego",
    "dyson", "kindle", "gopro",
    "sony wh-1000", "sony wh1000", "bose qc", "bose quietcomfort",
    "jordan", "yeezy",
    "rolex", "g-shock",
    "roborock", "roomba",
})

# ── Brands premium — usadas para detectar referencia de calidad ──────────────
_PREMIUM_BRANDS = frozenset({
    "apple", "samsung galaxy", "sony", "lg", "philips", "bose",
    "bosch", "dyson", "kärcher", "karcher", "miele",
    "nike", "adidas", "jordan", "new balance", "asics", "puma",
    "lego", "playmobil",
    "nespresso", "delonghi", "tefal", "rowenta", "braun", "siemens",
    "north face", "the north face", "patagonia",
    "garmin", "fitbit", "polar",
    "makita", "dewalt", "milwaukee",
    "canon", "nikon",
    "hp", "dell", "lenovo", "asus", "acer",
    "logitech", "razer",
    "casio", "seiko", "citizen", "g-shock",
    "breville", "sage",
})


def _es_buzz_brand(titulo: str) -> bool:
    t = titulo.lower()
    return any(b in t for b in _BUZZ_BRANDS)


def _es_premium_brand(titulo: str) -> bool:
    t = titulo.lower()
    return any(b in t for b in _PREMIUM_BRANDS)


def calcular_deal_score(p: "Producto", age_hours: float = 0.0) -> int:
    """
    Devuelve un score 0-100 para ranking en el feed de descubrimiento.

    Componentes (total max 100):
      descuento (35)  +  marca (20)  +  histórico (20)  +  precio sweet spot (10)
      tipo (10)  +  frescura (5)
    """
    score = 0

    # Descuento — el factor más importante
    desc = p.descuento_pct or 0
    if   desc >= 70: score += 35
    elif desc >= 55: score += 28
    elif desc >= 45: score += 22
    elif desc >= 40: score += 15

    # Marca — premium + buzz se suman para reconocimiento total
    if _es_buzz_brand(p.titulo):       score += 20
    elif _es_premium_brand(p.titulo):  score += 12

    # Precio histórico mínimo (CCC o equivalente)
    hist_min = p.precio_historico_min or 0
    if hist_min > 0 and p.precio_actual > 0:
        ratio = p.precio_actual / hist_min
        if   ratio <= 1.0:  score += 20  # mínimo histórico
        elif ratio <= 1.05: score += 15  # muy cerca
        elif ratio <= 1.15: score += 8

    # Sweet spot de precio — 30-300€ es el rango de mayor engagement
    pa = p.precio_actual or 0
    if   30 <= pa <= 300:  score += 10
    elif 300 < pa <= 500:  score += 5

    # Tipo — arbitraje tiene math de Wallapop que despierta más curiosidad
    if   p.tipo == "ARBITRAJE": score += 10
    elif p.tipo == "OFERTA":    score += 5

    # Frescura — los deals recientes se sienten más "en vivo"
    if   age_hours <= 6:  score += 5
    elif age_hours <= 24: score += 3
    elif age_hours <= 48: score += 1

    return max(0, min(100, score))


# ── Tags emocionales ──────────────────────────────────────────────────────────
#
# Set fijo de 7 tags. Cada uno se asigna por heurística determinista.
# Devolvemos máximo 3 por deal — más tags diluye el mensaje.

TAG_TRENDING         = "🔥 Trending"
TAG_HIDDEN_GEM       = "👀 Hidden Gem"
TAG_SMART_BUY        = "🧠 Smart Buy"
TAG_INTERNET_FAV     = "⚡ Internet Favorite"
TAG_VIRAL_PICK       = "📈 Viral Pick"
TAG_PARECE_MAS_CARO  = "💸 Parece más caro"
TAG_WORTH_WATCHING   = "🎯 Worth Watching"

ALL_TAGS = [
    TAG_TRENDING, TAG_HIDDEN_GEM, TAG_SMART_BUY, TAG_INTERNET_FAV,
    TAG_VIRAL_PICK, TAG_PARECE_MAS_CARO, TAG_WORTH_WATCHING,
]


def asignar_tags(p: "Producto", deal_score: int, engagement: dict | None = None) -> list[str]:
    """
    Asigna 1-3 tags emocionales al deal.

    Parámetros:
      deal_score : 0-100 calculado por calcular_deal_score
      engagement : dict opcional con {ctr, clicks, saves} para tags de tendencia
                   (en el slice piloto puede ser None)
    """
    tags: list[str] = []
    eng = engagement or {}

    # 🔥 Trending — top tier del scoring
    if deal_score >= 80:
        tags.append(TAG_TRENDING)

    # 👀 Hidden Gem — descuento alto pero marca poco conocida
    if (p.descuento_pct or 0) >= 55 and not _es_premium_brand(p.titulo):
        tags.append(TAG_HIDDEN_GEM)

    # 🧠 Smart Buy — arbitraje con margen real
    beneficio = getattr(p, "beneficio_neto", 0) or 0
    if p.tipo == "ARBITRAJE" and beneficio >= 25:
        tags.append(TAG_SMART_BUY)

    # ⚡ Internet Favorite — marca con buzz cultural fuerte
    if _es_buzz_brand(p.titulo):
        tags.append(TAG_INTERNET_FAV)

    # 📈 Viral Pick — CTR alto en últimas 72h (necesita engagement data)
    ctr = eng.get("ctr", 0)
    if ctr >= 0.12:  # 12%+ click-through es excepcional
        tags.append(TAG_VIRAL_PICK)

    # 💸 Parece más caro — precio actual por DEBAJO del mínimo histórico
    hist_min = p.precio_historico_min or 0
    if hist_min > 0 and p.precio_actual > 0 and p.precio_actual < hist_min * 0.97:
        tags.append(TAG_PARECE_MAS_CARO)

    # 🎯 Worth Watching — fallback. Todos los deals que llegan aquí han
    # superado los thresholds de publicación, así que merecen al menos un tag.
    if not tags:
        tags.append(TAG_WORTH_WATCHING)

    # Limitar a 3 — más diluye
    return tags[:3]


def edad_en_horas(timestamp_iso: str | None) -> float:
    """Devuelve la edad en horas desde un timestamp ISO. 0 si no se puede parsear."""
    if not timestamp_iso:
        return 0.0
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - ts
        return diff.total_seconds() / 3600
    except Exception:
        return 0.0
