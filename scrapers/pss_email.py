"""
scrapers/pss_email.py — Extractor de URLs de evento de Private Sport Shop via Gmail IMAP.

El newsletter de PSS no contiene productos individuales con precios.
Es un catálogo de eventos de marca ("Adidas Terrex hasta -71%") con links de tracking
que redirigen a páginas de venta en privatesportshop.es/event/*.

Este módulo:
  1. Conecta a Gmail con IMAP + App Password
  2. Busca emails no leídos del remitente PSS
  3. Extrae las URLs de evento decodificando los links de tracking (base64)
  4. Devuelve lista de URLs limpias para que Playwright las visite

Requisitos en .env:
  EMAIL_ADDRESS=tu@gmail.com
  EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
  PSS_EMAIL_SENDER=thomas@ese.privatesportshop.com
"""

import base64
import email
import imaplib
import os
import re
import urllib.parse
from email.header import decode_header

from dotenv import load_dotenv

load_dotenv()

EMAIL_ADDRESS      = os.getenv("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
PSS_EMAIL_SENDER   = os.getenv("PSS_EMAIL_SENDER", "privatesportshop")


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


def _extraer_urls_evento(html_body: str) -> list[str]:
    """
    Extrae URLs de evento de PSS de los links de tracking del newsletter.

    Los links tienen este formato:
      https://eli.privatesportshop.com/u/nrd.php?...&d=<base64_url_encoded>&...

    El parámetro `d` es la URL real en base64 + URL-encoding.
    Ejemplo decodificado: https://www.privatesportshop.es/event/adidas-terrex?sp_nav=...
    """
    urls = []
    vistos: set[str] = set()

    # Buscar todos los hrefs del newsletter
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html_body)

    for href in hrefs:
        # Solo links de tracking de PSS con parámetro `d=`
        if "privatesportshop.com/u/" not in href and "privatesportshop.es/u/" not in href:
            continue
        if "d=" not in href:
            continue

        try:
            # Extraer el parámetro `d` de la query string
            parsed = urllib.parse.urlparse(href)
            params = urllib.parse.parse_qs(parsed.query)
            d_values = params.get("d", [])
            if not d_values:
                continue

            # El parámetro `d` tiene varios segmentos separados por `|` —
            # solo el primero es la URL en base64.
            d_encoded = d_values[0].split("|")[0]
            # Añadir padding si falta
            padding = 4 - len(d_encoded) % 4
            if padding != 4:
                d_encoded += "=" * padding

            real_url = base64.b64decode(d_encoded).decode("utf-8", errors="replace")

            # Solo nos interesan páginas de venta/evento (no navegación general)
            es_evento = "/event/" in real_url or "/venta/" in real_url or "/sale/" in real_url
            es_productos = "/products?" in real_url or "/productos?" in real_url
            if not es_evento and not es_productos:
                continue

            # Para /event/ limpiar toda la query; para /products? conservar
            # filtros de categoría/marca y eliminar solo parámetros de tracking
            if es_productos and "?" in real_url:
                parsed_real = urllib.parse.urlparse(real_url)
                params_limpios = {
                    k: v for k, v in urllib.parse.parse_qs(parsed_real.query).items()
                    if not k.startswith("sp_") and not k.startswith("utm_")
                }
                if params_limpios:
                    qs = urllib.parse.urlencode({k: v[0] for k, v in params_limpios.items()})
                    url_limpia = f"{parsed_real.scheme}://{parsed_real.netloc}{parsed_real.path}?{qs}"
                else:
                    url_limpia = real_url.split("?")[0]
            else:
                url_limpia = real_url.split("?")[0]

            if url_limpia and url_limpia not in vistos:
                vistos.add(url_limpia)
                urls.append(url_limpia)

        except Exception:
            continue

    return urls


# ── API pública ────────────────────────────────────────────────────────────────

def get_pss_event_urls() -> list[str]:
    """
    Conecta a Gmail, busca newsletters no leídas de PSS y extrae
    las URLs de evento para que Playwright las visite.

    Returns:
        Lista de URLs limpias tipo: https://www.privatesportshop.es/event/adidas-terrex
        Lista vacía si no hay credenciales o no hay emails nuevos.
    """
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        print("   ⚠️  [PSS Email] EMAIL_ADDRESS o EMAIL_APP_PASSWORD no configurados — omitiendo")
        return []

    todas_las_urls: list[str] = []

    try:
        print(f"   🔍 [PSS Email] Conectando a Gmail ({EMAIL_ADDRESS})...")
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        imap.select("INBOX")

        search_criteria = f'(UNSEEN FROM "{PSS_EMAIL_SENDER}")'
        status, message_ids = imap.search(None, search_criteria)

        if status != "OK" or not message_ids[0]:
            print("   ℹ️  [PSS Email] No hay newsletters nuevas de PSS")
            imap.logout()
            return []

        ids = message_ids[0].split()
        print(f"   📧 [PSS Email] {len(ids)} newsletter(s) sin leer de PSS")

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

                urls = _extraer_urls_evento(html_body)
                print(f"   ✅ [PSS Email] {len(urls)} evento(s) encontrados: {[u.split('/')[-1] for u in urls]}")
                todas_las_urls.extend(urls)

                # Marcar como leído
                imap.store(msg_id, "+FLAGS", "\\Seen")

            except Exception as e:
                print(f"   ❌ [PSS Email] Error procesando email {msg_id}: {e}")
                continue

        imap.logout()

    except imaplib.IMAP4.error as e:
        print(f"   ❌ [PSS Email] Error IMAP: {e}")
    except Exception as e:
        print(f"   ❌ [PSS Email] Error inesperado: {e}")

    # Deduplicar preservando orden
    seen: set[str] = set()
    resultado = []
    for u in todas_las_urls:
        if u not in seen:
            seen.add(u)
            resultado.append(u)

    return resultado


# ── Test rápido ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🧪 Test extractor URLs PSS Email\n")
    urls = get_pss_event_urls()
    if urls:
        print(f"\n{len(urls)} URLs de evento extraídas:")
        for u in urls:
            print(f"  • {u}")
    else:
        print("  Sin URLs (verifica .env y que haya newsletters sin leer)")
