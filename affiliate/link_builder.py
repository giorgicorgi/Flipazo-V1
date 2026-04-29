"""
affiliate/link_builder.py — Generador de URLs de afiliado por tienda.

Soporta:
  - Amazon.es           → Amazon Associates (tag directo en la URL)
  - MediaMarkt.es       → Tradedoubler (prioritario) / Awin (fallback)
  - PcComponentes       → UTM params propios (?utm_medium=recomendadores&utm_source=ID)
  - Beep ES             → Tradedoubler deep link
  - Billabong ES        → Tradedoubler deep link
  - Cole Haan España    → Tradedoubler deep link
  - Element Brand ES    → Tradedoubler deep link
  - Elliotti            → Tradedoubler deep link
  - The Beauty Corner   → Tradedoubler deep link
  - ToysRus ES          → Tradedoubler deep link

Si faltan credenciales devuelve la URL directa (el pipeline sigue funcionando).
"""

import os
import urllib.parse

from dotenv import load_dotenv

load_dotenv()

# ── Amazon ────────────────────────────────────────────────────────────────────
AMAZON_AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "flipazo-21")

# ── PcComponentes — UTM afiliado propio ───────────────────────────────────────
# utm_source = ID de publisher en el programa de recomendadores de PcComponentes
PCCOMPONENTES_UTM_SOURCE = os.getenv("PCCOMPONENTES_UTM_SOURCE", "")

# ── Awin ──────────────────────────────────────────────────────────────────────
AWIN_PUBLISHER_ID         = os.getenv("AWIN_PUBLISHER_ID", "")
MEDIAMARKT_AWIN_MID       = os.getenv("MEDIAMARKT_AWIN_MID", "6907")
ELCORTEINGLES_AWIN_MID    = os.getenv("ELCORTEINGLES_AWIN_MID", "")
PRIVATESPORTSHOP_AWIN_MID = os.getenv("PRIVATESPORTSHOP_AWIN_MID", "")
MAMMOTH_AWIN_MID          = os.getenv("MAMMOTH_AWIN_MID", "")
BARRABES_AWIN_MID         = os.getenv("BARRABES_AWIN_MID", "")

# ── Tradedoubler ──────────────────────────────────────────────────────────────
TD_PUBLISHER_ID     = os.getenv("TD_PUBLISHER_ID", "")

BEEP_TD_PID         = os.getenv("BEEP_TD_PID",         "347347")
BILLABONG_TD_PID    = os.getenv("BILLABONG_TD_PID",     "324694")
COLEHAAN_TD_PID     = os.getenv("COLEHAAN_TD_PID",      "364994")
ELEMENT_TD_PID      = os.getenv("ELEMENT_TD_PID",       "324735")
ELLIOTTI_TD_PID     = os.getenv("ELLIOTTI_TD_PID",      "385916")
MEDIAMARKT_TD_PID   = os.getenv("MEDIAMARKT_TD_PID",    "270504")
BEAUTYCORNER_TD_PID = os.getenv("BEAUTYCORNER_TD_PID",  "311896")
TOYSRUS_TD_PID      = os.getenv("TOYSRUS_TD_PID",       "211811")


# ── Helpers internos ──────────────────────────────────────────────────────────

def _pccomponentes_affiliate_url(product_url: str) -> str:
    """Añade utm_medium=recomendadores&utm_source=ID a cualquier URL de PcComponentes."""
    if not PCCOMPONENTES_UTM_SOURCE or not product_url:
        return product_url
    parsed = urllib.parse.urlparse(product_url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    # Limpiar UTMs previos que pudiera traer la URL del scraper
    for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"):
        params.pop(k, None)
    params["utm_medium"] = ["recomendadores"]
    params["utm_source"] = [PCCOMPONENTES_UTM_SOURCE]
    new_query = urllib.parse.urlencode({k: v[0] for k, v in params.items()})
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def _awin_deep_link(merchant_id: str, product_url: str) -> str:
    if not AWIN_PUBLISHER_ID or not merchant_id:
        return product_url
    encoded = urllib.parse.quote(product_url, safe="")
    return (
        f"https://www.awin1.com/cread.php"
        f"?awinmid={merchant_id}"
        f"&awinaffid={AWIN_PUBLISHER_ID}"
        f"&ued={encoded}"
    )


def _tradedoubler_deep_link(program_id: str, product_url: str) -> str:
    if not TD_PUBLISHER_ID or not program_id:
        return product_url
    encoded = urllib.parse.quote(product_url, safe="")
    return (
        f"https://clk.tradedoubler.com/click"
        f"?p={program_id}"
        f"&a={TD_PUBLISHER_ID}"
        f"&url={encoded}"
    )


# ── API pública ───────────────────────────────────────────────────────────────

def build_affiliate_url(tienda: str, asin_or_url: str) -> str:
    """
    Devuelve la URL de afiliado correcta según la tienda.

    Args:
        tienda:       nombre de la tienda (ver lista de soportadas arriba)
        asin_or_url:  ASIN de 10 chars para Amazon; URL directa de producto para el resto.

    Returns:
        URL de afiliado con tracking, o URL directa si no hay credenciales configuradas.
    """
    if not asin_or_url:
        return ""

    # Amazon
    if tienda == "Amazon":
        return f"https://www.amazon.es/dp/{asin_or_url}?tag={AMAZON_AFFILIATE_TAG}"

    # MediaMarkt — Tradedoubler prioritario, Awin como fallback
    if tienda == "MediaMarkt":
        if TD_PUBLISHER_ID:
            return _tradedoubler_deep_link(MEDIAMARKT_TD_PID, asin_or_url)
        return _awin_deep_link(MEDIAMARKT_AWIN_MID, asin_or_url)

    # Tradedoubler
    if tienda == "Beep":
        return _tradedoubler_deep_link(BEEP_TD_PID, asin_or_url)

    if tienda == "Billabong":
        return _tradedoubler_deep_link(BILLABONG_TD_PID, asin_or_url)

    if tienda == "Cole Haan":
        return _tradedoubler_deep_link(COLEHAAN_TD_PID, asin_or_url)

    if tienda == "Element Brand":
        return _tradedoubler_deep_link(ELEMENT_TD_PID, asin_or_url)

    if tienda == "Elliotti":
        return _tradedoubler_deep_link(ELLIOTTI_TD_PID, asin_or_url)

    if tienda == "The Beauty Corner":
        return _tradedoubler_deep_link(BEAUTYCORNER_TD_PID, asin_or_url)

    if tienda == "ToysRus":
        return _tradedoubler_deep_link(TOYSRUS_TD_PID, asin_or_url)

    # PcComponentes — UTM propio (sin Awin)
    if tienda == "PcComponentes":
        return _pccomponentes_affiliate_url(asin_or_url)

    if tienda == "ElCorteIngles":
        return _awin_deep_link(ELCORTEINGLES_AWIN_MID, asin_or_url)

    if tienda == "PrivateSportShop":
        return _awin_deep_link(PRIVATESPORTSHOP_AWIN_MID, asin_or_url)

    if tienda == "Mammoth Bikes":
        return _awin_deep_link(MAMMOTH_AWIN_MID, asin_or_url)

    if tienda == "Barrabes":
        return _awin_deep_link(BARRABES_AWIN_MID, asin_or_url)

    # Tienda desconocida → URL directa (sin perder el deal)
    return asin_or_url


def affiliate_status() -> dict:
    """Devuelve el estado de configuración de cada red de afiliados (útil para debug)."""
    return {
        "amazon":                         bool(AMAZON_AFFILIATE_TAG),
        "pccomponentes_utm_source":        PCCOMPONENTES_UTM_SOURCE or "❌ no configurado",
        "awin_publisher":                  AWIN_PUBLISHER_ID or "❌ no configurado",
        "mediamarkt_awin_mid":             MEDIAMARKT_AWIN_MID or "❌ no configurado",
        "td_publisher":                    TD_PUBLISHER_ID or "❌ no configurado",
        "mediamarkt_td_pid":         MEDIAMARKT_TD_PID,
        "beep_td_pid":               BEEP_TD_PID,
        "billabong_td_pid":          BILLABONG_TD_PID,
        "colehaan_td_pid":           COLEHAAN_TD_PID,
        "element_td_pid":            ELEMENT_TD_PID,
        "elliotti_td_pid":           ELLIOTTI_TD_PID,
        "beautycorner_td_pid":       BEAUTYCORNER_TD_PID,
        "toysrus_td_pid":            TOYSRUS_TD_PID,
    }
