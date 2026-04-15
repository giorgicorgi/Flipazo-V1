"""
affiliate/link_builder.py — Generador de URLs de afiliado por tienda.

Soporta:
  - Amazon.es        → Amazon Associates (tag directo en la URL)
  - MediaMarkt.es    → Awin deep link  (MEDIAMARKT_AWIN_MID en .env)
  - PcComponentes    → Awin deep link  (PCCOMPONENTES_AWIN_MID en .env)

Si no hay credenciales Awin configuradas devuelve la URL directa (sin tracking),
así el pipeline sigue funcionando mientras se espera el alta en Awin.

Cómo obtener los Merchant IDs de Awin
──────────────────────────────────────
1. Entra en https://ui.awin.com → Publisher → My Publishers → tu cuenta
2. Ve a "Programmes" y busca "MediaMarkt" / "PcComponentes"
3. Únete al programa (suele tardar 24-48h en aprobarse)
4. El Merchant ID (awinmid) aparece en la URL del programa o en "Links"
5. Tu Publisher ID (awinaffid) está en el menú superior de tu cuenta Awin

Valores de referencia (confirmar en tu cuenta Awin):
  MediaMarkt ES:   awinmid ≈ 6907
  PcComponentes:   awinmid ≈ 16440  (verificar — pueden variar por región/cuenta)
"""

import os
import urllib.parse

from dotenv import load_dotenv

load_dotenv()

AMAZON_AFFILIATE_TAG   = os.getenv("AMAZON_AFFILIATE_TAG", "flipazo-21")
AWIN_PUBLISHER_ID      = os.getenv("AWIN_PUBLISHER_ID", "")

# Merchant IDs: los valores por defecto son de referencia.
# Confírmalos en tu panel Awin antes de activar en producción.
MEDIAMARKT_AWIN_MID       = os.getenv("MEDIAMARKT_AWIN_MID", "6907")
PCCOMPONENTES_AWIN_MID    = os.getenv("PCCOMPONENTES_AWIN_MID", "")
ELCORTEINGLES_AWIN_MID    = os.getenv("ELCORTEINGLES_AWIN_MID", "")
PRIVATESPORTSHOP_AWIN_MID = os.getenv("PRIVATESPORTSHOP_AWIN_MID", "")
MAMMOTH_AWIN_MID          = os.getenv("MAMMOTH_AWIN_MID", "")  # programa Awin si se aprueba


# ── Helpers internos ──────────────────────────────────────────────────────────

def _awin_deep_link(merchant_id: str, product_url: str) -> str:
    """
    Genera el deep link de Awin para un producto.
    Si falta AWIN_PUBLISHER_ID o merchant_id, devuelve la URL directa.
    """
    if not AWIN_PUBLISHER_ID or not merchant_id:
        return product_url
    encoded = urllib.parse.quote(product_url, safe="")
    return (
        f"https://www.awin1.com/cread.php"
        f"?awinmid={merchant_id}"
        f"&awinaffid={AWIN_PUBLISHER_ID}"
        f"&ued={encoded}"
    )


# ── API pública ───────────────────────────────────────────────────────────────

def build_affiliate_url(tienda: str, asin_or_url: str) -> str:
    """
    Devuelve la URL de afiliado correcta según la tienda.

    Args:
        tienda:       "Amazon" | "MediaMarkt" | "PcComponentes" | otra
        asin_or_url:  ASIN de 10 chars para Amazon; URL directa de producto para el resto.

    Returns:
        URL de afiliado con tracking, o URL directa si no hay credenciales configuradas.
    """
    if not asin_or_url:
        return ""

    if tienda == "Amazon":
        return f"https://www.amazon.es/dp/{asin_or_url}?tag={AMAZON_AFFILIATE_TAG}"

    if tienda == "MediaMarkt":
        return _awin_deep_link(MEDIAMARKT_AWIN_MID, asin_or_url)

    if tienda == "PcComponentes":
        return _awin_deep_link(PCCOMPONENTES_AWIN_MID, asin_or_url)

    if tienda == "ElCorteIngles":
        return _awin_deep_link(ELCORTEINGLES_AWIN_MID, asin_or_url)

    if tienda == "PrivateSportShop":
        return _awin_deep_link(PRIVATESPORTSHOP_AWIN_MID, asin_or_url)

    if tienda == "Mammoth Bikes":
        # Si se configura MAMMOTH_AWIN_MID → deep link Awin; si no → URL directa
        return _awin_deep_link(MAMMOTH_AWIN_MID, asin_or_url)

    # Tienda desconocida → devolver URL directa (sin perder el deal)
    return asin_or_url


def affiliate_status() -> dict:
    """Devuelve el estado de configuración de cada red de afiliados (útil para debug)."""
    return {
        "amazon": bool(AMAZON_AFFILIATE_TAG),
        "awin_publisher_configurado": bool(AWIN_PUBLISHER_ID),
        "mediamarkt_mid": MEDIAMARKT_AWIN_MID or "❌ no configurado",
        "pccomponentes_mid": PCCOMPONENTES_AWIN_MID or "❌ no configurado",
        "mammoth_mid": MAMMOTH_AWIN_MID or "URL directa (sin Awin)",
    }
