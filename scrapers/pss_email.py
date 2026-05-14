"""
scrapers/pss_email.py — Extractor de productos de Private Sport Shop via newsletters Gmail.

PSS usa AWS WAF que bloquea todas las peticiones HTTP incluyendo cloudscraper.
En lugar de visitar la web, extraemos los datos directamente del HTML del newsletter:
título, marca, precio actual, descuento y URL del producto.

Flujo:
  1. Conectar a Gmail IMAP con App Password
  2. Buscar emails de PSS de los últimos N días (SEEN o UNSEEN)
  3. Por cada email, extraer productos directamente del HTML del newsletter
  4. Devolver lista de dicts listos para Producto(**d)

Requisitos en .env:
  EMAIL_ADDRESS=flipazo.newsletter@gmail.com
  EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
  PSS_EMAIL_SENDER=thomas@ese.privatesportshop.com
"""

import base64
import email
import imaplib
import os
import re
import urllib.parse
from datetime import datetime, timedelta
from email.header import decode_header

from dotenv import load_dotenv

load_dotenv()

EMAIL_ADDRESS      = os.getenv("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
PSS_EMAIL_SENDER   = os.getenv("PSS_EMAIL_SENDER", "privatesportshop")

PSS_DIAS_HACIA_ATRAS = 7  # buscar emails de los últimos N días


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode_header_str(header_value: str) -> str:
    parts = decode_header(header_value)
    decoded = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded)


def _get_html_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html" and "attachment" not in str(part.get("Content-Disposition", "")):
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    elif msg.get_content_type() == "text/html":
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def _decode_pss_url(tracking_url: str) -> str:
    """
    Decodifica la URL real de producto del link de tracking de PSS.
    Formato: https://eli.privatesportshop.com/u/nrd.php?...&d=BASE64|...
    """
    try:
        parsed = urllib.parse.urlparse(tracking_url)
        params = urllib.parse.parse_qs(parsed.query)
        d_values = params.get("d", [])
        if not d_values:
            return tracking_url
        d_encoded = d_values[0].split("|")[0]
        padding = 4 - len(d_encoded) % 4
        if padding != 4:
            d_encoded += "=" * padding
        return base64.b64decode(d_encoded).decode("utf-8", errors="replace").split("?")[0]
    except Exception:
        return tracking_url


def extraer_productos_newsletter(html_content: str) -> list[dict]:
    """
    Extrae productos directamente del HTML del newsletter de PSS.

    Estructura del bloque por producto:
    - <img alt="Nombre producto"> → título
    - <span style="...uppercase...color:#999999">MARCA</span> → marca
    - <span style="font-size:18px">99,99 €</span> → precio actual
    - <a href="https://eli.privatesportshop.com/...">-20%* | Entrar</a> → descuento + URL

    CRÍTICO: el tracking URL en href tiene 400-500 chars — el texto "-20%" viene
    DESPUÉS de la URL dentro del mismo <a>, requiere ventana de 1200 chars.
    """
    products = []

    for img_m in re.finditer(r'<img[^>]+src="([^"]+)"[^>]+alt="([^"]{10,120})"', html_content):
        imagen_url = img_m.group(1)
        titulo = img_m.group(2).strip()

        if any(x in titulo for x in ("Private Sport Shop", "Logo", "logo")):
            continue

        ctx = html_content[img_m.end():img_m.end() + 2500]

        brand_m = re.search(r'text-transform:\s*uppercase[^>]*>([^<]{2,30})</span>', ctx, re.IGNORECASE)
        if not brand_m:
            brand_m = re.search(r'color:#999999[^>]*>([^<]{2,30})</span>', ctx, re.IGNORECASE)
        marca = brand_m.group(1).strip() if brand_m else ""

        price_m = re.search(r'font-size:\s*18px[^>]*>([0-9]+,[0-9]{2})\s*€', ctx)
        if not price_m:
            continue
        precio_actual = float(price_m.group(1).replace(',', '.'))

        # 1200 char window: URL in href is ~400-500 chars, discount text comes after it inside <a>
        after_price = ctx[price_m.start():price_m.start() + 1200]

        disc_m = re.search(r'[>"](-\s*([0-9]+)\s*%)', after_price)
        descuento = int(disc_m.group(2)) if disc_m else 0

        precio_original = round(precio_actual / (1 - descuento / 100), 2) if descuento > 0 else 0.0

        url_m = re.search(r'href="(https://eli\.privatesportshop[^"]+)"', after_price)
        product_url = _decode_pss_url(url_m.group(1)) if url_m else ""

        if not product_url or precio_actual <= 0:
            continue

        titulo_completo = f"{marca} {titulo}".strip() if marca and marca.lower() not in titulo.lower() else titulo

        products.append({
            "titulo": titulo_completo,
            "asin": product_url,
            "precio_actual": precio_actual,
            "precio_original": precio_original,
            "descuento_pct": descuento,
            "imagen_url": imagen_url,
            "tienda": "PrivateSportShop",
        })

    return products


# ── API pública ────────────────────────────────────────────────────────────────

def get_pss_productos(dias: int = PSS_DIAS_HACIA_ATRAS) -> list[dict]:
    """
    Conecta a Gmail, busca newsletters de PSS de los últimos `dias` días
    (independientemente de si están leídos) y extrae los productos directamente
    del HTML del email — sin visitar la web (bloqueada por AWS WAF).

    Returns:
        Lista de dicts con keys: titulo, asin, precio_actual, precio_original,
        descuento_pct, imagen_url, tienda. Listos para Producto(**d).
    """
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        print("   ⚠️  [PSS Email] EMAIL_ADDRESS o EMAIL_APP_PASSWORD no configurados — omitiendo")
        return []

    todos_los_productos: list[dict] = []
    titulos_vistos: set[str] = set()

    try:
        print(f"   🔍 [PSS Email] Conectando a Gmail ({EMAIL_ADDRESS})...")
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        imap.select("INBOX")

        fecha_limite = (datetime.now() - timedelta(days=dias)).strftime("%d-%b-%Y")
        search_criteria = f'(FROM "{PSS_EMAIL_SENDER}" SINCE {fecha_limite})'
        status, message_ids = imap.search(None, search_criteria)

        if status != "OK" or not message_ids[0]:
            print(f"   ℹ️  [PSS Email] No hay newsletters de PSS en los últimos {dias} días")
            imap.logout()
            return []

        ids = message_ids[0].split()
        print(f"   📧 [PSS Email] {len(ids)} newsletter(s) de PSS (últimos {dias} días)")

        for msg_id in ids:
            try:
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                subject = _decode_header_str(msg.get("Subject", ""))
                print(f"   📩 [PSS Email] Procesando: {subject[:60]}")

                html_body = _get_html_body(msg)
                if not html_body:
                    continue

                productos = extraer_productos_newsletter(html_body)
                nuevos = 0
                for p in productos:
                    clave = p["titulo"][:40].lower()
                    if clave not in titulos_vistos:
                        titulos_vistos.add(clave)
                        todos_los_productos.append(p)
                        nuevos += 1

                print(f"   ✅ [PSS Email] {nuevos} producto(s) extraídos de este email")

            except Exception as e:
                print(f"   ❌ [PSS Email] Error procesando email {msg_id}: {e}")
                continue

        imap.logout()

    except imaplib.IMAP4.error as e:
        print(f"   ❌ [PSS Email] Error IMAP: {e}")
    except Exception as e:
        print(f"   ❌ [PSS Email] Error inesperado: {e}")

    print(f"   📦 [PSS Email] Total: {len(todos_los_productos)} productos únicos de PSS")
    return todos_los_productos


# ── Test rápido ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🧪 Test extractor productos PSS Email\n")
    productos = get_pss_productos(dias=30)
    if productos:
        print(f"\n{len(productos)} productos extraídos:")
        for p in productos[:10]:
            print(f"  • {p['titulo'][:60]} | {p['precio_actual']}€ (-{p['descuento_pct']}%) | {p['asin'][:80]}")
    else:
        print("  Sin productos (verifica .env y que haya newsletters recientes)")
