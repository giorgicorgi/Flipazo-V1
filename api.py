"""
api.py — Servidor FastAPI para flipazo.es

Endpoints públicos:
  GET  /api/deals               → lista de deals (JSON)
  GET  /api/deals/count         → total de deals
  GET  /api/price-history/{id}  → historial de precios
  POST /api/deals/{id}/vote     → votar un deal
  GET  /r/{deal_id}             → redirect afiliado con tracking
  GET  /health

Endpoints admin (requieren JWT con role=admin en Authorization header):
  POST   /admin/login              → autenticar admin, recibe JWT
  GET    /admin/deals              → deals con métricas (clicks + votos)
  DELETE /admin/deals/bulk         → eliminar varios deals (body: {deal_ids:[...]})
  DELETE /admin/deals/{deal_id}    → eliminar deal
  GET    /admin/stats              → estadísticas generales

Auth OAuth (usuarios):
  GET  /auth/google                → redirige a Google OAuth
  GET  /auth/google/callback       → callback de Google, devuelve JWT de usuario
  GET  /auth/apple                 → redirige a Apple OAuth
  POST /auth/apple/callback        → callback Apple (form_post), devuelve JWT
  GET  /auth/me                    → perfil del usuario autenticado

Arranque:
  venv/bin/uvicorn api:app --host 0.0.0.0 --port 8080
"""

import base64
import hashlib
import hmac as _hmac
import json
import os
import re as _re
import secrets
import smtplib
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests as _http
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# La regla de 'precio habitual' vive en el pipeline: el panel la importa en vez
# de reimplementarla, para que admin y producción no puedan divergir.
from scrapers.price_drop import referencia_de_serie
from pydantic import BaseModel

load_dotenv()

DB_PATH = "flipazo_deals.db"

# ── Admin & JWT ────────────────────────────────────────────────────────────────
ADMIN_USERNAME  = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD  = os.getenv("ADMIN_PASSWORD", "")   # contraseña en .env (no en git)
JWT_SECRET      = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ADMIN_HOURS = 12    # horas de validez del token admin
JWT_USER_HOURS  = 720   # 30 días para tokens de usuario

# ── Cookie settings ────────────────────────────────────────────────────────────
_COOKIE_SECURE = os.getenv("ENV", "production") != "development"
_COOKIE_DOMAIN = ".flipazo.es" if _COOKIE_SECURE else None

# ── Google OAuth ───────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv(
    "GOOGLE_REDIRECT_URI", "https://api.flipazo.es/auth/google/callback"
)

# ── Apple OAuth ────────────────────────────────────────────────────────────────
APPLE_CLIENT_ID    = os.getenv("APPLE_CLIENT_ID", "")   # Service ID de Apple
APPLE_REDIRECT_URI = os.getenv(
    "APPLE_REDIRECT_URI", "https://api.flipazo.es/auth/apple/callback"
)

# ── Threads OAuth (setup one-time) ─────────────────────────────────────────────
THREADS_APP_ID     = os.getenv("THREADS_APP_ID",     "1472052057551805")
THREADS_APP_SECRET = os.getenv("THREADS_APP_SECRET", "")
THREADS_REDIRECT   = "https://api.flipazo.es/auth/threads/callback"
THREADS_TOKEN       = os.getenv("THREADS_TOKEN", "")
THREADS_USER_ID_ENV = os.getenv("THREADS_USER_ID", "")

# ── Frontend (para redirects post-OAuth) ───────────────────────────────────────
FRONTEND_CUENTA = os.getenv("FRONTEND_CUENTA", "https://flipazo.es/cuenta")
API_URL         = os.getenv("API_URL",          "https://api.flipazo.es")

# ── Email (Gmail SMTP para verificación de cuentas) ────────────────────────────
EMAIL_ADDRESS      = os.getenv("EMAIL_ADDRESS",      "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")
# Proveedor SMTP. Por defecto Gmail en 587 (el 465 lo bloquea Hetzner). Con un
# proveedor de boletines basta cambiar el .env: Brevo → smtp-relay.brevo.com:587
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", "")

# ── WhatsApp Cloud API ─────────────────────────────────────────────────────────
WA_PHONE_NUMBER_ID  = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_TOKEN            = os.getenv("WA_TOKEN", "")
WA_VERIFY_TOKEN     = os.getenv("WA_VERIFY_TOKEN", "flipazo_wa_verify")  # token de verificación webhook

# ── OAuth state store en memoria (anti-CSRF) ───────────────────────────────────
_oauth_states: dict[str, float] = {}

# ── Rate-limit para flags de expiración (IP:deal_id → expiry timestamp) ────────
_flag_rate_limit: dict[str, float] = {}
_FLAG_COOLDOWN = 3600  # 1 h por IP por deal — evita multivoto del mismo usuario
# Protocolo de revisión: cuántos flags independientes hacen falta para expirar
# un deal que NO se pudo verificar automáticamente (resultado None).
# Verificación positiva (resultado True) expira con 1 solo flag; este umbral
# solo aplica al caso "no verificable" para evitar falsos expirados.
_FLAG_CONSENSUS_MIN = 3

# ── Verificación automática de precio a 3/7 días ───────────────────────────────
_VERIFIER_INTERVAL_S = 6 * 3600   # cada cuánto despierta el loop de verificación
_VERIFIER_FIRST_DELAY_S = 90      # espera antes de la 1ª pasada (no competir con el arranque)
_VERIFIER_SUBIDA_TOL = 1.02       # subida > 2% sobre el 1er descuento → expirado
_VERIFIER_BAJADA_TOL = 0.98       # bajada > 2% (y ≥1€) → "¡Aún más rebajado!"
_VERIFIER_MAX_POR_PASADA = 200    # límite de deals por pasada (no saturar)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Flipazo API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://flipazo.es",
        "https://www.flipazo.es",
        "http://localhost",
        "http://127.0.0.1",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)


# ── DB helper ──────────────────────────────────────────────────────────────────

def _get_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


# ── JWT helpers ────────────────────────────────────────────────────────────────

def _jwt_create(payload: dict, expire_hours: int) -> str:
    """Genera un JWT HS256 firmado con JWT_SECRET."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b'=')
    body = {**payload, "exp": int(time.time()) + expire_hours * 3600, "iat": int(time.time())}
    body_enc = base64.urlsafe_b64encode(
        json.dumps(body, separators=(',', ':')).encode()
    ).rstrip(b'=')
    msg = header + b'.' + body_enc
    sig = _hmac.new(JWT_SECRET.encode(), msg, hashlib.sha256).digest()
    return (msg + b'.' + base64.urlsafe_b64encode(sig).rstrip(b'=')).decode()


def _jwt_decode(token: str) -> dict | None:
    """Verifica y decodifica un JWT. Devuelve None si inválido o expirado."""
    try:
        h, p, s = token.split('.')
        expected = _hmac.new(JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        actual   = base64.urlsafe_b64decode(s + '==')
        if not _hmac.compare_digest(expected, actual):
            return None
        payload = json.loads(base64.urlsafe_b64decode(p + '=='))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _set_user_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "flipazo_user_jwt", token,
        httponly=True, secure=_COOKIE_SECURE, samesite="lax",
        max_age=JWT_USER_HOURS * 3600, path="/", domain=_COOKIE_DOMAIN,
    )

def _set_admin_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "flipazo_admin_jwt", token,
        httponly=True, secure=_COOKIE_SECURE, samesite="strict",
        max_age=JWT_ADMIN_HOURS * 3600, path="/", domain=_COOKIE_DOMAIN,
    )

def _clear_user_cookie(response: Response) -> None:
    response.delete_cookie("flipazo_user_jwt", path="/", domain=_COOKIE_DOMAIN)

def _clear_admin_cookie(response: Response) -> None:
    response.delete_cookie("flipazo_admin_jwt", path="/", domain=_COOKIE_DOMAIN)


def _require_admin(request: Request) -> dict | None:
    """Valida JWT admin desde cookie httpOnly (fallback: Authorization header)."""
    token = request.cookies.get("flipazo_admin_jwt", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    payload = _jwt_decode(token)
    return payload if payload and payload.get("role") == "admin" else None


def _require_user(request: Request) -> dict | None:
    """Valida JWT de usuario desde cookie httpOnly (fallback: Authorization header)."""
    token = request.cookies.get("flipazo_user_jwt", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    payload = _jwt_decode(token)
    return payload if payload and payload.get("role") == "user" else None


# ── Password helpers ───────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return salt.hex() + ":" + key.hex()

def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return _hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False

def _send_email(to: str, subject: str, html: str) -> bool:
    """Envía por SMTP. Host, puerto y credenciales salen del .env para poder
    cambiar de proveedor sin tocar código.

    ⚠️ Puerto 587 con STARTTLS, no 465: Hetzner tiene bloqueada la salida por
    465, así que la versión anterior (SMTP_SSL a smtp.gmail.com:465) se quedaba
    esperando hasta el timeout y NINGÚN correo de verificación llegaba nunca.
    El fallo era mudo porque el except solo imprimía en el log del servicio."""
    host = SMTP_HOST or "smtp.gmail.com"
    port = SMTP_PORT or 587
    user = SMTP_USER or EMAIL_ADDRESS
    pwd  = SMTP_PASS or EMAIL_APP_PASSWORD
    if not user or not pwd:
        print("⚠️  Email no configurado — verificación omitida")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = MAIL_FROM or f"Flipazo <{user}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as srv:
                srv.login(user, pwd)
                srv.sendmail(user, to, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as srv:
                srv.starttls()
                srv.login(user, pwd)
                srv.sendmail(user, to, msg.as_string())
        return True
    except Exception as e:
        print(f"❌ Email error ({host}:{port}): {e}")
        return False

# ── OAuth state helpers ────────────────────────────────────────────────────────

def _gen_state() -> str:
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = time.time() + 600  # válido 10 min
    expired = [k for k, v in _oauth_states.items() if v < time.time()]
    for k in expired:
        _oauth_states.pop(k, None)
    return state


def _verify_state(state: str) -> bool:
    exp = _oauth_states.pop(state, None)
    return bool(exp and exp > time.time())


# ── Users helper ───────────────────────────────────────────────────────────────

def _upsert_user(user_id: str, email: str, name: str, avatar_url: str, provider: str):
    """Crea o actualiza un usuario en la BD (upsert)."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_db() as con:
        con.execute("""
            INSERT INTO users (id, email, name, avatar_url, provider, premium, created_at, last_login)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                email      = excluded.email,
                name       = COALESCE(NULLIF(excluded.name,       ''), users.name),
                avatar_url = COALESCE(NULLIF(excluded.avatar_url, ''), users.avatar_url),
                last_login = excluded.last_login
        """, (user_id, email, name, avatar_url, provider, now, now))
        con.commit()


# ── Cache de precios del feed Tradedoubler (para chequeo de subida de precio) ───

_td_price_cache: dict[str, float] = {}   # url_key → precio_actual
_td_price_cache_ts: dict[str, float] = {} # fid → timestamp de última descarga
_TD_PRICE_CACHE_TTL = 6 * 3600           # 6 horas


def _load_td_prices(fid: str) -> None:
    """Descarga el feed completo de un fid y rellena la caché de precios."""
    token = os.getenv("TRADEDOUBLER_TOKEN", "")
    if not token:
        return
    try:
        url = f"https://api.tradedoubler.com/1.0/productsUnlimited.json;fid={fid}?token={token}"
        r = _http.get(url, timeout=60)
        if r.status_code != 200:
            return
        for p in r.json().get("products", []):
            offers = p.get("offers") or []
            if not offers:
                continue
            p_url = offers[0].get("productUrl", "")
            ph    = offers[0].get("priceHistory") or []
            if not p_url or not ph:
                continue
            price_str = (ph[0].get("price") or {}).get("value", "")
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue
            m = _re.search(r'product\((\d+)-(\d+)\)', p_url)
            if m:
                _td_price_cache[f"{m.group(1)}-{m.group(2)}"] = price
        _td_price_cache_ts[fid] = time.time()
        print(f"📦 TD price cache fid={fid}: {len(_td_price_cache)} productos cargados")
    except Exception as e:
        print(f"⚠️ TD price cache fid={fid} error: {e}")


def _td_current_price(click_url: str) -> float | None:
    """
    Devuelve el precio actual en el feed TD para una URL pdt.tradedoubler.com.
    Descarga el feed completo la primera vez (caché 6h).
    Retorna None si el producto no está en el feed (puede haber sido retirado).
    """
    m = _re.search(r'product\((\d+)-(\d+)\)', click_url)
    if not m:
        return None
    fid, pid = m.group(1), m.group(2)
    # Refrescar caché si es necesaria
    ts = _td_price_cache_ts.get(fid, 0)
    if time.time() - ts > _TD_PRICE_CACHE_TTL:
        _load_td_prices(fid)
    return _td_price_cache.get(f"{fid}-{pid}")  # None si producto no en feed


# ── Verificación ligera de precio expirado ─────────────────────────────────────

def _check_price_expired(url_afiliado: str, precio_stored: float = 0, timeout: int = 5) -> bool | None:
    """
    Comprueba si el producto ya no está disponible.
    Retorna True (expirado), False (activo), o None (no se pudo verificar → revisión manual).
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "es-ES,es;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        url = url_afiliado

        # Links Tradedoubler estándar (pdt.tradedoubler.com) — verificación doble:
        # 1) Precio actual via TD API (caché 6h): si subió >15% → expirado
        # 2) URL directa MediaMarkt para check de disponibilidad (404)
        if "tradedoubler.com" in url_afiliado and _re.search(r'product\(\d+-\d+\)', url_afiliado):
            td_current = _td_current_price(url_afiliado)
            if td_current is None:
                # Producto no encontrado en feed → retirado / expirado
                return True
            if precio_stored > 0 and td_current > precio_stored * 1.15:
                # Precio subió más del 15% respecto al precio del deal → expirado
                return True
            # Precio sin cambio significativo → construir URL directa para check disponibilidad
            td_m = _re.search(r'product\(\d+-(\d+)\)', url_afiliado)
            if td_m:
                url = f"https://www.mediamarkt.es/es/product/_{td_m.group(1)}.html"

        elif "clk.tradedoubler.com" in url_afiliado and "&url=" in url_afiliado:
            # Deep links propios (clk.tradedoubler.com/click?p=PID&a=AID&url=DEST_URL)
            # El &url= contiene la URL real del producto (Esdemarca, PCBox, etc.).
            # clk.tradedoubler.com hace redirect JS — requests no lo sigue.
            # Extraemos la URL destino y la verificamos directamente.
            import urllib.parse as _up
            qs = _up.parse_qs(_up.urlparse(url_afiliado).query)
            dest = _up.unquote(qs.get("url", [""])[0])
            if not dest:
                return None
            url = dest

        elif "tdvisit." in url_afiliado:
            # TD white-label (tdvisit.esdemarca.com, etc.) — JS redirect, no verificable via web
            return None

        elif "awin1.com" in url_afiliado or "awin.com/cread" in url_afiliado:
            # Enlaces AWIN (Padel Market): la disponibilidad real está en el feed AWIN
            # (campo in_stock), no en el HTML. La ficha tiene "agotado" en variantes de
            # talla / relacionados y falseaba la expiración de TODO el catálogo → None.
            # La frescura de Padel la da el pipeline (solo republica productos in_stock).
            return None

        resp = _http.get(url, headers=headers, timeout=timeout, allow_redirects=True)

        if resp.status_code in (404, 410):
            return True

        # Cloudflare, WAF o error de servidor — no sabemos el estado real
        if resp.status_code in (403, 503) or "challenge" in resp.url:
            return None

        content = resp.text.lower()

        # Amazon CAPTCHA (opfcaptcha.amazon.es) — el VPS Hetzner recibe esto sistemáticamente
        if "opfcaptcha" in content or "api-services-support@amazon.com" in content:
            return None

        # Cloudflare challenge HTML
        if 'id="challenge-form"' in content or "just a moment" in content:
            return None

        # TD white-label JS redirect (TradeDoublerGUID meta tag)
        if "tradedoublerguid" in content:
            return None

        # Esdemarca: stock 100% JS/AJAX — indetectable en HTML estático.
        # Si la URL de respuesta conserva el mismo ID de producto → activo.
        # Si redirigió a otro ID o a una página sin ID → producto retirado.
        if "esdemarca.com" in resp.url:
            req_id_m  = _re.search(r'-(\d{6,})\.html', url)
            resp_id_m = _re.search(r'-(\d{6,})\.html', resp.url)
            if req_id_m and resp_id_m and req_id_m.group(1) == resp_id_m.group(1):
                return False  # URL conserva el mismo producto → activo
            if req_id_m and resp_id_m and req_id_m.group(1) != resp_id_m.group(1):
                return True   # redirigido a otro producto/categoría
            if req_id_m and not resp_id_m:
                return True   # redirigido a página sin ID → retirado

        signals = [
            "actualmente no disponible", "currently unavailable",
            "no está disponible", "este artículo no está disponible",
            "temporalmente sin existencias", "out of stock",
            "producto no encontrado", "página no encontrada",
            "agotado",
        ]
        return any(s in content for s in signals)

    except Exception:
        return None  # Error de red → revisión manual


def _notify_admin_expiry(deal_id: str, titulo: str, resultado: bool | None) -> None:
    """Envía notificación Telegram al admin con el resultado de la verificación."""
    try:
        token    = os.getenv("TELEGRAM_BOT_TOKEN", "")
        admin_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
        if not token or not admin_id:
            return
        if resultado is True:
            estado = "❌ <b>EXPIRADO</b> — marcado automáticamente en web"
        elif resultado is False:
            estado = "✅ Precio verificado — sigue activo"
        else:
            estado = (
                "⚠️ <b>No verificable automáticamente</b> (Cloudflare/JS redirect)\n"
                "Por favor, revisa manualmente y marca desde el panel admin."
            )
        msg = (
            f"🚩 <b>Alerta de oferta expirada</b>\n\n"
            f"{estado}\n\n"
            f"<b>{titulo[:70]}</b>\n"
            f"🔗 <code>/api/deals/{deal_id}/flag-expired</code>"
        )
        _http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": admin_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"❌ Notify admin error: {e}")


def _background_check_expiry(deal_id: str, url_afiliado: str, titulo: str, precio_stored: float = 0) -> None:
    """
    Hilo background: verifica si la oferta expiró, actualiza BD y notifica al admin.
    Usado cuando la verificación sincrónica no pudo confirmar el estado.
    """
    try:
        resultado = _check_price_expired(url_afiliado, precio_stored=precio_stored)

        with _get_db() as con:
            if resultado is True:
                con.execute(
                    "UPDATE deals_publicados SET expirado = 1 WHERE deal_id = ?",
                    (deal_id,),
                )
                con.commit()
                print(f"🔴 Deal marcado expirado (bg): {titulo[:50]}")

        _notify_admin_expiry(deal_id, titulo, resultado)
    except Exception as e:
        print(f"❌ Background expiry check error: {e}")


# ── Verificación automática de precio a 3/7 días ───────────────────────────────

def _precio_actual_amazon(url_afiliado: str) -> float | None:
    """Precio actual de un deal Amazon leído del propio historial (sin CAPTCHA).

    El pipeline registra precios Amazon en price_history continuamente; usamos la
    observación más reciente (últimos 4 días) del ASIN como precio actual.
    Devuelve None si no hay dato reciente.
    """
    m = _re.search(r'/dp/([A-Z0-9]{10})', url_afiliado or "")
    if not m:
        return None
    asin = m.group(1)
    desde = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%d")
    try:
        with _get_db() as con:
            row = con.execute(
                "SELECT precio FROM price_history "
                "WHERE asin = ? AND tienda = 'Amazon' AND fecha >= ? AND precio > 0 "
                "ORDER BY fecha DESC LIMIT 1",
                (asin, desde),
            ).fetchone()
        if row and row["precio"]:
            return float(row["precio"])
    except Exception:
        pass
    return None


def _precio_actual_deal(url_afiliado: str, tienda: str) -> float | None:
    """Devuelve el precio numérico actual de un deal según su tienda, o None si no se puede.

    - Tiendas TD (pdt.tradedoubler.com): precio del feed (caché 6h).
    - Amazon: última observación de price_history (sin CAPTCHA).
    - Resto: None (solo se podrá hacer check de disponibilidad).
    """
    url = url_afiliado or ""
    if "tradedoubler.com" in url and _re.search(r'product\(\d+-\d+\)', url):
        return _td_current_price(url)
    if tienda == "Amazon" or "amazon.es/dp/" in url or "/dp/" in url:
        return _precio_actual_amazon(url)
    return None


def _verificar_un_deal(con, deal: dict, hito: str) -> None:
    """Verifica el precio de un deal y aplica el efecto (expirado / más rebajado).

    `hito` es 'verif_3d' o 'verif_7d' — el checkpoint que se marca como procesado.
    Reusa _precio_actual_deal (precio numérico) y _check_price_expired (disponibilidad).
    """
    deal_id      = deal["deal_id"]
    titulo       = deal["titulo"] or ""
    tienda       = deal["tienda"] or ""
    url_afiliado = deal["url_afiliado"] or ""
    # Base de comparación: precio del 1er descuento (inmutable). Fallback al precio actual.
    precio_base  = float(deal["precio_publicado"] or deal["precio"] or 0)

    ahora        = datetime.now(timezone.utc).isoformat()
    precio_act   = _precio_actual_deal(url_afiliado, tienda)

    expirado     = False
    mas_rebajado = False
    nuevo_precio = None

    if precio_act is not None and precio_act > 0 and precio_base > 0:
        if precio_act > precio_base * _VERIFIER_SUBIDA_TOL:
            expirado = True            # subió de precio → oferta expirada
        elif precio_act < precio_base * _VERIFIER_BAJADA_TOL and (precio_base - precio_act) >= 1.0:
            mas_rebajado = True        # bajó aún más
            nuevo_precio = precio_act
    else:
        # Sin precio numérico → check de disponibilidad/subida por URL (bool/None).
        # Solo expira con confirmación positiva; None (no verificable) no toca el deal.
        if _check_price_expired(url_afiliado, precio_stored=precio_base, timeout=5) is True:
            expirado = True

    # ── Aplicar resultado ──────────────────────────────────────────────────────
    if expirado:
        con.execute(
            f"UPDATE deals_publicados SET expirado = 1, precio_verificado = ?, "
            f"precio_verificado_en = ?, {hito} = 1 WHERE deal_id = ?",
            (precio_act if precio_act is not None else None, ahora, deal_id),
        )
        print(f"🔴 [verif {hito}] expirado (subió/retirado): {titulo[:50]}")
        threading.Thread(
            target=_notify_admin_expiry, args=(deal_id, titulo, True), daemon=True
        ).start()
    elif mas_rebajado:
        precio_original = float(deal["precio_original"] or 0)
        descuento = (round((1 - nuevo_precio / precio_original) * 100)
                     if precio_original > 0 else deal["descuento_pct"])
        con.execute(
            f"UPDATE deals_publicados SET mas_rebajado = 1, precio = ?, descuento_pct = ?, "
            f"precio_verificado = ?, precio_verificado_en = ?, precio_actualizado_en = ?, "
            f"{hito} = 1 WHERE deal_id = ?",
            (nuevo_precio, descuento, nuevo_precio, ahora, ahora, deal_id),
        )
        print(f"📉 [verif {hito}] más rebajado {precio_base:.2f}→{nuevo_precio:.2f}€: {titulo[:50]}")
    else:
        # Sin cambio relevante (o no verificable): solo registrar la verificación y el hito.
        con.execute(
            f"UPDATE deals_publicados SET precio_verificado = ?, precio_verificado_en = ?, "
            f"{hito} = 1 WHERE deal_id = ?",
            (precio_act if precio_act is not None else None, ahora, deal_id),
        )


def _verificar_precios_pendientes() -> int:
    """Una pasada: verifica deals que cruzaron el hito de 3d o 7d sin procesar.

    Devuelve el número de deals verificados. Cada deal va en su propio try/except
    para que un fallo puntual no detenga la pasada.
    """
    cols = ("deal_id, titulo, tienda, url_afiliado, precio, precio_publicado, "
            "precio_original, descuento_pct")
    procesados = 0
    with _get_db() as con:
        # Hito de 3 días (no procesado y con ≥3 días de antigüedad).
        deals_3d = con.execute(
            f"SELECT {cols} FROM deals_publicados "
            "WHERE COALESCE(expirado,0)=0 AND COALESCE(verif_3d,0)=0 "
            "AND publicado_en <= datetime('now','-3 days') "
            "AND publicado_en > datetime('now','-7 days') "
            "ORDER BY publicado_en ASC LIMIT ?",
            (_VERIFIER_MAX_POR_PASADA,),
        ).fetchall()
        # Hito de 7 días (no procesado y con ≥7 días de antigüedad).
        deals_7d = con.execute(
            f"SELECT {cols} FROM deals_publicados "
            "WHERE COALESCE(expirado,0)=0 AND COALESCE(verif_7d,0)=0 "
            "AND publicado_en <= datetime('now','-7 days') "
            "ORDER BY publicado_en ASC LIMIT ?",
            (_VERIFIER_MAX_POR_PASADA,),
        ).fetchall()

        for hito, deals in (("verif_3d", deals_3d), ("verif_7d", deals_7d)):
            for r in deals:
                try:
                    _verificar_un_deal(con, dict(r), hito)
                    con.commit()
                    procesados += 1
                except Exception as e:
                    print(f"⚠️ verificador deal {r['deal_id'][:8]} error: {e}")
    if procesados:
        print(f"✅ Verificador de precios: {procesados} deals procesados")
    return procesados


def _price_verifier_loop() -> None:
    """Loop daemon: ejecuta una pasada de verificación cada _VERIFIER_INTERVAL_S."""
    time.sleep(_VERIFIER_FIRST_DELAY_S)
    while True:
        try:
            _verificar_precios_pendientes()
        except Exception as e:
            print(f"❌ Price verifier loop error: {e}")
        time.sleep(_VERIFIER_INTERVAL_S)


_verifier_started = False

def _start_price_verifier() -> None:
    """Arranca el thread del verificador una sola vez."""
    global _verifier_started
    if _verifier_started:
        return
    _verifier_started = True
    threading.Thread(target=_price_verifier_loop, daemon=True).start()
    print("🕒 Verificador de precios 3/7d arrancado (cada 6h)")


# ── Startup: migraciones en caliente ─────────────────────────────────────────

@app.on_event("startup")
def _ensure_schema():
    """Migraciones suaves al arrancar: añade columnas y tablas si no existen."""
    with _get_db() as con:
        # Columnas nuevas en deals_publicados
        for col_def in [
            "votes_up        INTEGER DEFAULT 0",
            "votes_down      INTEGER DEFAULT 0",
            "categoria       TEXT    DEFAULT ''",
            "pros            TEXT    DEFAULT '[]'",
            "contras         TEXT    DEFAULT '[]'",
            "flags_expirado  INTEGER DEFAULT 0",
            "expirado        INTEGER DEFAULT 0",
            # Discovery layer
            "deal_score      INTEGER DEFAULT 0",
            "hook            TEXT    DEFAULT ''",
            "social_context  TEXT    DEFAULT ''",
            "emotional_tags  TEXT    DEFAULT '[]'",
            "stock_qty       INTEGER DEFAULT 0",
            "precio_actualizado_en TEXT DEFAULT NULL",
            # Verificación automática de precio a 3/7 días
            "precio_publicado     REAL",
            "precio_verificado    REAL",
            "precio_verificado_en TEXT DEFAULT NULL",
            "mas_rebajado         INTEGER DEFAULT 0",
            "verif_3d             INTEGER DEFAULT 0",
            "verif_7d             INTEGER DEFAULT 0",
            "pocas_unidades  TEXT DEFAULT ''",
            "tallas          TEXT DEFAULT ''",
            # Ficha de producto generada bajo demanda (Haiku) al abrir el detalle
            "ficha_ia          TEXT DEFAULT ''",
            "ficha_generada_en TEXT DEFAULT NULL",
        ]:
            try:
                con.execute(f"ALTER TABLE deals_publicados ADD COLUMN {col_def}")
            except Exception:
                pass  # columna ya existe

        # Historial de precios
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

        # Clicks
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

        # Usuarios (OAuth + email)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id                 TEXT PRIMARY KEY,
                email              TEXT,
                name               TEXT,
                avatar_url         TEXT DEFAULT '',
                provider           TEXT DEFAULT 'google',
                premium            INTEGER DEFAULT 0,
                stripe_customer_id TEXT DEFAULT '',
                created_at         TEXT NOT NULL,
                last_login         TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")

        # Columnas nuevas en users (email auth + newsletter)
        for col_def in [
            "password_hash       TEXT    DEFAULT ''",
            "email_verified      INTEGER DEFAULT 0",
            "verification_token  TEXT    DEFAULT ''",
            "newsletter          INTEGER DEFAULT 0",
        ]:
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
            except Exception:
                pass

        # Favoritos
        con.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id    TEXT NOT NULL,
                deal_id    TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, deal_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_fav_user ON favorites(user_id)")

        # Preferencias "Para ti" + vinculación con el bot de Telegram.
        # cats/subcats/stores van como JSON: la forma la define el frontend
        # (mismas categorías y subcategorías que los filtros de la web).
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id      TEXT PRIMARY KEY,
                cats         TEXT    DEFAULT '[]',
                subcats      TEXT    DEFAULT '{}',
                stores       TEXT    DEFAULT '[]',
                precio_min   REAL    DEFAULT 0,
                precio_max   REAL,
                tg_chat_id   TEXT    DEFAULT '',
                tg_code      TEXT    DEFAULT '',
                tg_enabled   INTEGER DEFAULT 1,
                tg_last_sent TEXT    DEFAULT '',
                updated_at   TEXT    NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_prefs_tg   ON user_prefs(tg_chat_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_prefs_code ON user_prefs(tg_code)")

        # Boletín "Para ti" por correo. La frecuencia la elige el usuario:
        #   diario | semanal | alternos | dias   (dias → email_dias, 0=lunes … 6=domingo)
        # 'semanal' usa el primer día de email_dias como día de envío.
        for col_def in [
            "email_enabled   INTEGER DEFAULT 0",
            "email_freq      TEXT    DEFAULT 'semanal'",
            "email_dias      TEXT    DEFAULT '[0]'",
            "email_last_sent TEXT    DEFAULT ''",
        ]:
            try:
                con.execute(f"ALTER TABLE user_prefs ADD COLUMN {col_def}")
            except Exception:
                pass

        # Blog posts
        con.execute("""
            CREATE TABLE IF NOT EXISTS blog_posts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                slug             TEXT    UNIQUE NOT NULL,
                titulo           TEXT    NOT NULL,
                resumen          TEXT    DEFAULT '',
                contenido        TEXT    DEFAULT '',
                imagen_url       TEXT    DEFAULT '',
                publicado        INTEGER DEFAULT 0,
                created_at       TEXT    NOT NULL,
                updated_at       TEXT    NOT NULL,
                meta_description TEXT    DEFAULT '',
                tags             TEXT    DEFAULT '',
                og_title         TEXT    DEFAULT '',
                schema_type      TEXT    DEFAULT 'Article'
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_blog_slug ON blog_posts(slug)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_blog_pub  ON blog_posts(publicado, created_at)")

        # Columnas SEO/AEO en blog_posts (migracion suave)
        for col_def in [
            "meta_description TEXT DEFAULT ''",
            "tags             TEXT DEFAULT ''",
            "og_title         TEXT DEFAULT ''",
            "schema_type      TEXT DEFAULT 'Article'",
        ]:
            try:
                con.execute(f"ALTER TABLE blog_posts ADD COLUMN {col_def}")
            except Exception:
                pass

        # Páginas estáticas editables
        con.execute("""
            CREATE TABLE IF NOT EXISTS paginas (
                slug       TEXT PRIMARY KEY,
                content    TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            )
        """)

        # Comentarios de deals
        con.execute("""
            CREATE TABLE IF NOT EXISTS deal_comments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id     TEXT    NOT NULL,
                user_id     TEXT    NOT NULL,
                user_name   TEXT    NOT NULL DEFAULT '',
                user_avatar TEXT    NOT NULL DEFAULT '',
                content     TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_dc_deal ON deal_comments(deal_id, created_at)")

        # Deals borrados manualmente — nunca se vuelven a publicar
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
                telefono   TEXT PRIMARY KEY,
                nombre     TEXT DEFAULT '',
                activo     INTEGER DEFAULT 1,
                alta_en    TEXT NOT NULL,
                baja_en    TEXT
            )
        """)
        # Promociones/cupones AWIN (las escribe el pipeline; la API solo las lee)
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

        # Backfill precio_publicado (migración única): deals históricos toman su precio
        # actual como "primer descuento" base para la verificación a 3/7 días.
        try:
            con.execute(
                "UPDATE deals_publicados SET precio_publicado = precio "
                "WHERE precio_publicado IS NULL AND precio IS NOT NULL"
            )
        except Exception:
            pass

        con.commit()

    # Arrancar el verificador automático de precios (3/7 días) en background.
    _start_price_verifier()


# ── Modelos ────────────────────────────────────────────────────────────────────

class VoteBody(BaseModel):
    direction: str  # "up" | "down"


class AdminLoginBody(BaseModel):
    username: str
    password: str


class PatchDealBody(BaseModel):
    titulo:       Optional[str]  = None
    url_afiliado: Optional[str]  = None
    expirado:     Optional[bool] = None

class BulkDeleteDealsBody(BaseModel):
    deal_ids: list[str]

class RegisterBody(BaseModel):
    email:    str
    password: str
    name:     str = ""

class EmailLoginBody(BaseModel):
    email:    str
    password: str

class NewsletterBody(BaseModel):
    subscribed: bool


class CommentBody(BaseModel):
    content: str


class BlogPostBody(BaseModel):
    slug:             str
    titulo:           str
    resumen:          str = ""
    contenido:        str = ""
    imagen_url:       str = ""
    publicado:        bool = False
    meta_description: str = ""
    tags:             str = ""
    og_title:         str = ""
    schema_type:      str = "Article"


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS PÚBLICOS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/deals")
def get_deals(
    limit:     int = Query(default=50, ge=1, le=500),
    offset:    int = Query(default=0,  ge=0),
    tipo:      Optional[str]   = Query(default=None, description="OFERTA | ARBITRAJE"),
    tienda:    Optional[str]   = Query(default=None),
    categoria: Optional[str]   = Query(default=None),
    max_price: Optional[float] = Query(default=None, ge=0, description="Filtra deals con precio <= max_price (€)"),
):
    """Devuelve deals publicados ordenados del más reciente al más antiguo."""
    where_clauses, params = [], []
    # Catálogo COMPLETO, sin ventana temporal: el número que anunciamos ("N ofertas
    # verificadas") tiene que corresponderse con lo que se puede ver de verdad.
    # Antes se cortaba a 30 días, así que la web decía 3.302 mientras el histórico
    # era 5.621 y esas 2.319 no había forma de alcanzarlas. Van ordenadas de más
    # reciente a más antigua, así que lo viejo queda al final del scroll.
    if tipo:
        where_clauses.append("tipo = ?"); params.append(tipo.upper())
    if tienda:
        where_clauses.append("tienda = ?"); params.append(tienda)
    if categoria:
        where_clauses.append("categoria = ?"); params.append(categoria.lower())
    if max_price is not None:
        where_clauses.append("precio <= ?"); params.append(max_price)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            rowid,
            deal_id         AS id,
            titulo,
            tienda,
            tipo,
            precio          AS precio_actual,
            precio_original,
            descuento_pct,
            imagen_url,
            url_afiliado    AS url_affiliate,
            precio_wallapop,
            beneficio_neto,
            razonamiento,
            COALESCE(votes_up,       0) AS votes_up,
            COALESCE(votes_down,     0) AS votes_down,
            COALESCE(categoria,     '') AS categoria,
            COALESCE(pros,        '[]') AS pros,
            COALESCE(contras,     '[]') AS contras,
            COALESCE(flags_expirado, 0) AS flags_expirado,
            COALESCE(expirado,       0) AS expirado,
            COALESCE(deal_score,     0) AS deal_score,
            COALESCE(hook,          '') AS hook,
            COALESCE(social_context,'') AS social_context,
            COALESCE(emotional_tags,'[]') AS emotional_tags,
            COALESCE(stock_qty,      0) AS stock_qty,
            COALESCE(pocas_unidades,'') AS pocas_unidades,
            COALESCE(tallas,        '') AS tallas,
            precio_actualizado_en,
            precio_publicado,
            precio_verificado,
            precio_verificado_en,
            COALESCE(mas_rebajado,   0) AS mas_rebajado,
            publicado_en    AS timestamp,
            (SELECT COUNT(*) FROM deal_comments WHERE deal_id = deals_publicados.deal_id) AS comment_count
        FROM deals_publicados
        {where_sql}
        ORDER BY publicado_en DESC
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]

    with _get_db() as con:
        rows = con.execute(sql, params).fetchall()

    deals = [_normalize_deal_row(r) for r in rows]
    return JSONResponse(content=deals)


# ── Normalizer compartido entre /api/deals y /api/sections/* ──────────────────

def _normalize_deal_row(r) -> dict:
    """Convierte una fila sqlite en dict normalizado para el frontend."""
    d = dict(r)
    d["precio_actual"]   = d.get("precio_actual")   or 0.0
    d["precio_original"] = d.get("precio_original") or 0.0
    d["descuento_pct"]   = d.get("descuento_pct")   or 0
    d["precio_wallapop"] = d.get("precio_wallapop") or 0.0
    d["beneficio_neto"]  = d.get("beneficio_neto")  or 0.0
    d["imagen_url"]      = d.get("imagen_url")       or ""
    d["razonamiento"]    = d.get("razonamiento")     or ""
    d["votes_up"]        = d.get("votes_up")         or 0
    d["votes_down"]      = d.get("votes_down")       or 0
    d["categoria"]       = d.get("categoria")        or ""
    d["flags_expirado"]  = int(d.get("flags_expirado", 0) or 0)
    d["expirado"]        = bool(d.get("expirado", 0))
    d["comment_count"]   = int(d.get("comment_count", 0) or 0)
    d["deal_score"]      = int(d.get("deal_score", 0) or 0)
    d["hook"]            = d.get("hook")             or ""
    d["social_context"]  = d.get("social_context")   or ""
    try:    d["pros"]    = json.loads(d.get("pros")    or "[]")
    except: d["pros"]    = []
    try:    d["contras"] = json.loads(d.get("contras") or "[]")
    except: d["contras"] = []
    try:    d["emotional_tags"] = json.loads(d.get("emotional_tags") or "[]")
    except: d["emotional_tags"] = []
    d["stock_qty"]            = int(d.get("stock_qty", 0) or 0)
    d["pocas_unidades"]       = d.get("pocas_unidades") or ""
    d["tallas"]               = d.get("tallas") or ""
    d["precio_actualizado_en"] = d.get("precio_actualizado_en") or None
    # Verificación 3/7d: precio del 1er descuento, último precio verificado, flag rebajado
    d["precio_publicado"]      = d.get("precio_publicado")  or 0.0
    d["precio_verificado"]     = d.get("precio_verificado") or 0.0
    d["precio_verificado_en"]  = d.get("precio_verificado_en") or None
    d["mas_rebajado"]          = bool(d.get("mas_rebajado", 0))
    return d


# ══════════════════════════════════════════════════════════════════════════════
# DISCOVERY — secciones por emoción
# ══════════════════════════════════════════════════════════════════════════════

_SECTION_QUERIES = {
    # 🔥 Explotando hoy — top deal_score últimas 48h
    "trending": """
        SELECT *, publicado_en AS timestamp, deal_id AS id, precio AS precio_actual,
               url_afiliado AS url_affiliate,
               (SELECT COUNT(*) FROM deal_comments WHERE deal_id = deals_publicados.deal_id) AS comment_count
        FROM deals_publicados
        WHERE publicado_en >= datetime('now', '-2 days')
          AND COALESCE(expirado, 0) = 0
        ORDER BY deal_score DESC, publicado_en DESC
        LIMIT ?
    """,
    # 👀 Joyas ocultas — alto descuento + low score (marcas poco conocidas)
    "hidden-gems": """
        SELECT *, publicado_en AS timestamp, deal_id AS id, precio AS precio_actual,
               url_afiliado AS url_affiliate,
               (SELECT COUNT(*) FROM deal_comments WHERE deal_id = deals_publicados.deal_id) AS comment_count
        FROM deals_publicados
        WHERE publicado_en >= datetime('now', '-4 days')
          AND COALESCE(expirado, 0) = 0
          AND descuento_pct >= 50
          AND emotional_tags LIKE '%Hidden Gem%'
        ORDER BY descuento_pct DESC, publicado_en DESC
        LIMIT ?
    """,
    # 🧠 Compras inteligentes — ARBITRAJE con margen real
    "smart-buy": """
        SELECT *, publicado_en AS timestamp, deal_id AS id, precio AS precio_actual,
               url_afiliado AS url_affiliate,
               (SELECT COUNT(*) FROM deal_comments WHERE deal_id = deals_publicados.deal_id) AS comment_count
        FROM deals_publicados
        WHERE publicado_en >= datetime('now', '-4 days')
          AND COALESCE(expirado, 0) = 0
          AND tipo = 'ARBITRAJE'
          AND beneficio_neto >= 25
        ORDER BY beneficio_neto DESC, publicado_en DESC
        LIMIT ?
    """,
    # 📈 Viral — más clicks últimos 3 días
    "viral": """
        SELECT d.*, d.publicado_en AS timestamp, d.deal_id AS id, d.precio AS precio_actual,
               d.url_afiliado AS url_affiliate,
               (SELECT COUNT(*) FROM deal_comments WHERE deal_id = d.deal_id) AS comment_count,
               COUNT(c.id) AS clicks_recientes
        FROM deals_publicados d
        LEFT JOIN clicks c ON c.deal_id = d.deal_id
                          AND c.ts >= datetime('now', '-3 days')
        WHERE d.publicado_en >= datetime('now', '-7 days')
          AND COALESCE(d.expirado, 0) = 0
        GROUP BY d.deal_id
        HAVING clicks_recientes > 0
        ORDER BY clicks_recientes DESC, d.deal_score DESC
        LIMIT ?
    """,
}


@app.get("/api/sections/{name}")
def get_section(name: str, limit: int = Query(default=12, ge=1, le=50)):
    """
    Devuelve deals para una sección del feed de discovery.
    Secciones: trending | hidden-gems | smart-buy | viral
    """
    sql = _SECTION_QUERIES.get(name)
    if not sql:
        return JSONResponse(
            status_code=404,
            content={"error": f"sección desconocida — disponibles: {list(_SECTION_QUERIES)}"},
        )
    with _get_db() as con:
        rows = con.execute(sql, (limit,)).fetchall()
    return JSONResponse(content=[_normalize_deal_row(r) for r in rows])


@app.get("/api/deals/count")
def get_count():
    """
    `total` e `historico` son ya el MISMO número: el catálogo dejó de recortarse a
    30 días para que la cifra que anunciamos sea la que de verdad se puede navegar.
    Se mantienen las dos claves para no romper a quien ya consuma este endpoint.
    """
    with _get_db() as con:
        total = con.execute("SELECT COUNT(*) FROM deals_publicados").fetchone()[0]
        historico = total
        desde = con.execute("SELECT MIN(publicado_en) FROM deals_publicados").fetchone()[0] or ""
    return {"total": total, "historico": historico, "desde": desde[:10]}


@app.get("/api/promociones")
def get_promociones(limit: int = Query(default=40, ge=1, le=100)):
    """Promos/cupones de tienda VIGENTES HOY (AWIN + Tradedoubler), con código primero.

    El filtro va por fecha, no por el `estado` guardado: un cupón que empieza el día 16
    está en la tabla desde antes y se vuelve válido solo, sin depender de que el pipeline
    lo vuelva a capturar. Publicar uno que aún no ha empezado manda al usuario a una
    tienda donde el código todavía no funciona.
    """
    try:
        ahora = datetime.now(timezone.utc).isoformat()
        with _get_db() as con:
            rows = con.execute(
                "SELECT promo_id, tienda, titulo, descripcion, codigo, url, start_date, end_date, estado "
                "FROM promociones "
                "WHERE (end_date   = '' OR end_date   >= ?) "
                "  AND (start_date = '' OR start_date <= ?) "
                "ORDER BY (codigo != '') DESC, tienda, capturada_en DESC "
                "LIMIT ?",
                (ahora, ahora, limit),
            ).fetchall()
    except Exception:
        return JSONResponse(content=[])
    return JSONResponse(content=[dict(r) for r in rows])


@app.get("/api/price-history/{asin}")
def get_price_history(asin: str):
    with _get_db() as con:
        rows = con.execute(
            "SELECT fecha, precio, precio_original FROM price_history "
            "WHERE asin = ? ORDER BY fecha ASC",
            (asin,)
        ).fetchall()
    return JSONResponse(content=[dict(r) for r in rows])


@app.post("/api/deals/{deal_id}/vote")
def vote_deal(deal_id: str, body: VoteBody):
    """Registra o retira un voto (up/down/remove). Anti-spam client-side via localStorage."""
    if body.direction not in ("up", "down", "remove"):
        return JSONResponse(status_code=400, content={"error": "direction must be 'up', 'down', or 'remove'"})
    with _get_db() as con:
        if body.direction == "remove":
            sql = "UPDATE deals_publicados SET votes_up = MAX(0, votes_up - 1) WHERE deal_id = ?"
        else:
            col = "votes_up" if body.direction == "up" else "votes_down"
            sql = f"UPDATE deals_publicados SET {col} = {col} + 1 WHERE deal_id = ?"
        updated = con.execute(sql, (deal_id,)).rowcount
        if updated == 0:
            return JSONResponse(status_code=404, content={"error": "deal not found"})
        con.commit()
        row = con.execute(
            "SELECT COALESCE(votes_up,0) AS votes_up, COALESCE(votes_down,0) AS votes_down "
            "FROM deals_publicados WHERE deal_id = ?",
            (deal_id,),
        ).fetchone()
    return {"votes_up": row["votes_up"], "votes_down": row["votes_down"]}


@app.get("/api/deals/{deal_id}/comments")
def get_comments(deal_id: str):
    """Lista de comentarios de un deal, orden cronológico."""
    with _get_db() as con:
        rows = con.execute(
            "SELECT id, user_name, user_avatar, content, created_at "
            "FROM deal_comments WHERE deal_id = ? ORDER BY created_at ASC",
            (deal_id,)
        ).fetchall()
    return JSONResponse(content=[dict(r) for r in rows])


@app.post("/api/deals/{deal_id}/comments")
def add_comment(deal_id: str, body: CommentBody, request: Request):
    """Añade un comentario. Requiere JWT de usuario."""
    user = _require_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Debes iniciar sesión para comentar"})
    content = body.content.strip()
    if not content or len(content) > 500:
        return JSONResponse(status_code=400, content={"error": "El comentario debe tener entre 1 y 500 caracteres"})
    now = datetime.now(timezone.utc).isoformat()
    with _get_db() as con:
        cur = con.execute(
            "INSERT INTO deal_comments (deal_id, user_id, user_name, user_avatar, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (deal_id, user["sub"], user.get("name", ""), user.get("avatar", ""), content, now)
        )
        comment_id = cur.lastrowid
        con.commit()
    return {
        "id": comment_id,
        "user_name": user.get("name", ""),
        "user_avatar": user.get("avatar", ""),
        "content": content,
        "created_at": now,
    }


@app.post("/api/deals/{deal_id}/flag-expired")
def flag_expired(deal_id: str, request: Request):
    """
    Registra un voto de 'oferta expirada'.
    Anti-spam: mismo IP no puede flaggear el mismo deal más de una vez por hora.
    Con ≥1 flag verifica el precio sincrónicamente (timeout 5s) y devuelve
    el estado real al cliente para que marque la card inmediatamente si expiró.
    """
    ip       = request.client.host if request.client else "unknown"
    rate_key = f"{ip}:{deal_id}"
    now      = time.time()

    # Limpiar entradas antiguas + comprobar rate limit
    expired_keys = [k for k, v in _flag_rate_limit.items() if v <= now]
    for k in expired_keys:
        _flag_rate_limit.pop(k, None)

    if _flag_rate_limit.get(rate_key, 0) > now:
        return JSONResponse(status_code=429, content={"error": "Ya has reportado esta oferta recientemente"})

    _flag_rate_limit[rate_key] = now + _FLAG_COOLDOWN

    with _get_db() as con:
        row = con.execute(
            "SELECT titulo, url_afiliado, precio, flags_expirado, expirado "
            "FROM deals_publicados WHERE deal_id = ?",
            (deal_id,),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Deal no encontrado"})

        if row["expirado"]:
            return {"flags": row["flags_expirado"] or 0, "expirado": True}

        new_flags = (row["flags_expirado"] or 0) + 1
        con.execute(
            "UPDATE deals_publicados SET flags_expirado = ? WHERE deal_id = ?",
            (new_flags, deal_id),
        )
        con.commit()

    url_afiliado   = row["url_afiliado"] or ""
    titulo         = row["titulo"] or ""
    precio_stored  = float(row["precio"] or 0)

    # Verificar precio sincrónicamente — check de disponibilidad + subida de precio
    resultado = _check_price_expired(url_afiliado, precio_stored=precio_stored, timeout=5)

    if resultado is True:
        with _get_db() as con:
            con.execute(
                "UPDATE deals_publicados SET expirado = 1 WHERE deal_id = ?",
                (deal_id,),
            )
            con.commit()
        print(f"🔴 Deal marcado expirado (flag web): {titulo[:50]}")
        threading.Thread(
            target=_notify_admin_expiry, args=(deal_id, titulo, True), daemon=True
        ).start()
        return {"flags": new_flags, "expirado": True}

    # No verificable (resultado None): NO expirar con un solo flag.
    # Solo expira por consenso de varios reportes independientes (protocolo de revisión)
    # para evitar falsos expirados cuando la verificación falla (Cloudflare/CAPTCHA/JS redirect).
    if resultado is None and new_flags >= _FLAG_CONSENSUS_MIN:
        with _get_db() as con:
            con.execute(
                "UPDATE deals_publicados SET expirado = 1 WHERE deal_id = ?",
                (deal_id,),
            )
            con.commit()
        print(f"🔴 Deal marcado expirado (consenso {new_flags} flags): {titulo[:50]}")
        threading.Thread(
            target=_notify_admin_expiry, args=(deal_id, titulo, None), daemon=True
        ).start()
        return {"flags": new_flags, "expirado": True}

    # No confirmado todavía:
    #  - resultado False → precio verificado activo, sigue en el feed
    #  - resultado None con pocos flags → pendiente de consenso / revisión manual del admin
    if resultado is None:
        # Avisar al admin para revisión manual (sin expirar aún)
        threading.Thread(
            target=_notify_admin_expiry, args=(deal_id, titulo, None), daemon=True
        ).start()
    # Background verifica de nuevo y notifica si confirma expiración
    threading.Thread(
        target=_background_check_expiry,
        args=(deal_id, url_afiliado, titulo, precio_stored),
        daemon=True,
    ).start()
    return {"flags": new_flags, "expirado": False}


# ══════════════════════════════════════════════════════════════════════════════
# FICHA DE PRODUCTO (Haiku) — generada bajo demanda al abrir el detalle, cacheada
# para siempre en `ficha_ia`. Nunca menciona precio/descuento (ya lo muestra la UI).
# ══════════════════════════════════════════════════════════════════════════════

_FICHA_MODEL = "claude-haiku-4-5-20251001"

_FICHA_SCHEMA = {
    "type": "object",
    "properties": {
        "tipo":   {"type": "string", "enum": ["conocido", "generico"]},
        "intro":  {"type": "string"},
        "puntos": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tipo", "intro", "puntos"],
    "additionalProperties": False,
}

_FICHA_SYSTEM_PROMPT = """Eres redactor de fichas de producto para Flipazo, un canal español de chollos.
Escribes SOLO la descripción del producto — el precio, el descuento y el ahorro ya se
muestran aparte en la web, así que NUNCA los menciones ni justifiques por qué el precio es bueno.

Para cada producto decides tú el tipo:

TIPO "conocido" — identificas con confianza la marca Y el modelo exacto (ej. "Samsung Neo QLED
QN1EH", "Sony WH-1000XM5", "iPhone 15 Pro"):
- "intro": 2-4 frases en español, estilo ficha de producto, con características reales del
  modelo que conozcas con certeza.
- "puntos": 3-5 bullets, cada uno con UN emoji + un rasgo destacado en negrita markdown
  (**así**) + explicación breve.

TIPO "generico" — NO reconoces el modelo exacto, marca poco conocida, o el título no da datos
suficientes:
- "intro": 2-3 frases MUY breves — qué es y para qué sirve, usando solo lo que dice el título.
  Nada de relleno.
- "puntos": array vacío [].

REGLA DE SEGURIDAD (aplica siempre, en ambos tipos): NUNCA afirmes un número concreto (Hz, mAh,
Wh, GB, W, pulgadas exactas, etc.) que no venga en el título o los datos que te paso. Si no
estás seguro de un dato concreto, descríbelo en términos generales ("batería de larga
duración", "pantalla grande") en vez de inventar una cifra.

Nunca menciones precio, descuento, ahorro, ni "por qué es una buena oferta".

Responde SOLO el JSON pedido."""

# Tope diario de seguridad (contador en memoria, se resetea al reiniciar el servicio).
_FICHA_CAP_DIARIO = int(os.getenv("FICHA_IA_CAP_DIARIO", "300"))
_ficha_contador: dict = {"fecha": None, "n": 0}
_ficha_lock = threading.Lock()


def _ficha_puede_generar() -> bool:
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _ficha_lock:
        if _ficha_contador["fecha"] != hoy:
            _ficha_contador["fecha"] = hoy
            _ficha_contador["n"] = 0
        if _ficha_contador["n"] >= _FICHA_CAP_DIARIO:
            return False
        _ficha_contador["n"] += 1
        return True


def _generar_ficha_ia(titulo: str, tienda: str, categoria: str) -> dict | None:
    """Llama Haiku para generar la ficha. None si falla o no está configurado."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_FICHA_MODEL,
            max_tokens=600,
            system=_FICHA_SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": _FICHA_SCHEMA}},
            messages=[{
                "role": "user",
                "content": json.dumps(
                    {"titulo": (titulo or "")[:150], "tienda": tienda or "", "categoria": categoria or ""},
                    ensure_ascii=False,
                ),
            }],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("tipo") not in ("conocido", "generico"):
            return None
        es_conocido = data["tipo"] == "conocido"
        return {
            "tipo":   data["tipo"],
            "intro":  str(data.get("intro") or "")[:1200],
            "puntos": [str(p)[:200] for p in (data.get("puntos") or [])][:5] if es_conocido else [],
        }
    except Exception as e:
        print(f"⚠️  ficha_ia error: {e}")
        return None


@app.get("/api/deals/{deal_id}/ficha")
def get_ficha_ia(deal_id: str):
    """Ficha de producto para el detalle ampliado. Cacheada en BD tras la 1ª generación."""
    with _get_db() as con:
        row = con.execute(
            "SELECT titulo, tienda, categoria, ficha_ia FROM deals_publicados WHERE deal_id = ?",
            (deal_id,),
        ).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "deal no encontrado"})

        if row["ficha_ia"]:
            try:
                return JSONResponse(content=json.loads(row["ficha_ia"]))
            except Exception:
                pass  # caché corrupta — regenerar abajo

        if not _ficha_puede_generar():
            return JSONResponse(status_code=429, content={"error": "límite diario alcanzado, inténtalo más tarde"})

        ficha = _generar_ficha_ia(row["titulo"], row["tienda"], row["categoria"])
        if not ficha:
            return JSONResponse(status_code=503, content={"error": "no disponible por ahora"})

        con.execute(
            "UPDATE deals_publicados SET ficha_ia = ?, ficha_generada_en = ? WHERE deal_id = ?",
            (json.dumps(ficha, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), deal_id),
        )
        con.commit()
        return JSONResponse(content=ficha)


@app.get("/r/{deal_id}")
def redirect_afiliado(deal_id: str, request: Request, canal: str = "web", beacon: int = 0):
    """Redirect afiliado con tracking de click. `beacon=1` → solo registra el click y
    devuelve 204 (sin redirigir): lo usa el beacon no bloqueante del frontend, que ya
    navega directo a la tienda con el <a>. IP real vía X-Forwarded-For (tras nginx)."""
    with _get_db() as con:
        row = con.execute(
            "SELECT url_afiliado FROM deals_publicados WHERE deal_id = ?",
            (deal_id,)
        ).fetchone()
        if not row or not row["url_afiliado"]:
            if beacon:
                return Response(status_code=204)
            return JSONResponse(status_code=404, content={"error": "deal no encontrado"})
        ip = request.headers.get("X-Forwarded-For", "")
        ip = (ip.split(",")[0].strip() if ip else (request.client.host if request.client else "unknown"))
        con.execute(
            "INSERT INTO clicks (deal_id, canal, ip, ts) VALUES (?, ?, ?, ?)",
            (deal_id, canal, ip[:64], datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    if beacon:
        return Response(status_code=204)
    return RedirectResponse(url=row["url_afiliado"], status_code=302)


# ── Enlaces de bio contables (Threads, Instagram…) ───────────────────────────
# Redirect propio que cuenta cada click en el servidor (100% fiable, sin depender
# de cookies ni Google Analytics) y redirige a flipazo.es con UTM (para que GA
# también atribuya a quienes consienten). Ver clicks en /go-stats/{slug}.
_BIO_LINKS = {
    "threads":   "https://www.flipazo.es/?utm_source=threads&utm_medium=bio&utm_campaign=perfil",
    "instagram": "https://www.flipazo.es/?utm_source=instagram&utm_medium=bio&utm_campaign=perfil",
    "tiktok":    "https://www.flipazo.es/?utm_source=tiktok&utm_medium=bio&utm_campaign=perfil",
    "youtube":   "https://www.flipazo.es/?utm_source=youtube&utm_medium=bio&utm_campaign=perfil",
}


@app.get("/go/{slug}")
def go_bio(slug: str, request: Request):
    """Enlace de bio contable: registra el click y redirige a flipazo.es con UTM."""
    slug = "".join(c for c in (slug or "").lower() if c.isalnum() or c in "-_")[:32]
    destino = _BIO_LINKS.get(slug) or (
        f"https://www.flipazo.es/?utm_source={slug or 'bio'}&utm_medium=bio&utm_campaign=perfil"
    )
    ip = request.headers.get("X-Forwarded-For", "")
    ip = (ip.split(",")[0].strip() if ip else (request.client.host if request.client else "unknown"))
    try:
        with _get_db() as con:
            con.execute(
                "INSERT INTO clicks (deal_id, canal, ip, ts) VALUES (?, ?, ?, ?)",
                ("bio:" + slug, "bio:" + slug, ip[:64], datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
    except Exception:
        pass  # nunca bloquear la redirección por un fallo de logging
    return RedirectResponse(url=destino, status_code=302)


@app.get("/go-stats/{slug}")
def go_bio_stats(slug: str):
    """Clicks de un enlace de bio: total, IPs únicas y desglose por día."""
    slug = "".join(c for c in (slug or "").lower() if c.isalnum() or c in "-_")[:32]
    key = "bio:" + slug
    with _get_db() as con:
        total  = con.execute("SELECT COUNT(*) FROM clicks WHERE deal_id = ?", (key,)).fetchone()[0]
        unicos = con.execute("SELECT COUNT(DISTINCT ip) FROM clicks WHERE deal_id = ?", (key,)).fetchone()[0]
        dias   = con.execute(
            "SELECT substr(ts,1,10) d, COUNT(*) n FROM clicks WHERE deal_id = ? GROUP BY d ORDER BY d DESC LIMIT 60",
            (key,),
        ).fetchall()
    return {
        "slug": slug,
        "destino": _BIO_LINKS.get(slug),
        "clicks_total": total,
        "clicks_unicos_ip": unicos,
        "por_dia": {d: n for d, n in dias},
    }


def _geolocalizar_ips(ips: list) -> dict:
    """País/ciudad de una lista de IPs. Cachea en la tabla ip_geo y consulta las
    nuevas en ip-api.com (batch, gratis). Devuelve {ip: {country, code, city}}."""
    import ipaddress
    out: dict = {}
    ips = [ip for ip in dict.fromkeys(ips) if ip]  # únicas, orden estable
    if not ips:
        return out
    with _get_db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS ip_geo "
                    "(ip TEXT PRIMARY KEY, country TEXT, country_code TEXT, city TEXT, ts TEXT)")
        con.commit()
        ph = ",".join("?" * len(ips))
        for r in con.execute(f"SELECT ip, country, country_code, city FROM ip_geo WHERE ip IN ({ph})", ips):
            out[r["ip"]] = {"country": r["country"], "code": r["country_code"], "city": r["city"]}
    faltan = []
    for ip in ips:
        if ip in out:
            continue
        try:
            if ipaddress.ip_address(ip).is_global:
                faltan.append(ip)
        except Exception:
            pass
    nuevos = []
    for i in range(0, len(faltan), 100):
        chunk = faltan[i:i + 100]
        try:
            resp = _http.post("http://ip-api.com/batch?fields=status,country,countryCode,city,query",
                              json=chunk, timeout=8)
            for it in (resp.json() if resp.ok else []):
                if it.get("status") == "success":
                    ip = it.get("query")
                    g = {"country": it.get("country"), "code": it.get("countryCode"), "city": it.get("city")}
                    out[ip] = g
                    nuevos.append((ip, g["country"], g["code"], g["city"],
                                   datetime.now(timezone.utc).isoformat()))
        except Exception:
            break
    if nuevos:
        try:
            with _get_db() as con:
                con.executemany("INSERT OR REPLACE INTO ip_geo (ip,country,country_code,city,ts) "
                                "VALUES (?,?,?,?,?)", nuevos)
                con.commit()
        except Exception:
            pass
    return out


@app.get("/go-detail/{slug}")
def go_bio_detail(slug: str):
    """Detalle de un enlace de bio: ubicaciones (país/ciudad), heatmap día×hora
    (hora de Madrid), distribución horaria/semanal y conversión aproximada."""
    from collections import Counter
    slug = "".join(c for c in (slug or "").lower() if c.isalnum() or c in "-_")[:32]
    key = "bio:" + slug
    with _get_db() as con:
        rows = con.execute("SELECT ip, ts FROM clicks WHERE deal_id = ?", (key,)).fetchall()
        conv = con.execute(
            "SELECT COUNT(DISTINCT b.ip) FROM clicks b "
            "JOIN clicks d ON d.ip = b.ip AND d.deal_id NOT LIKE 'bio:%' AND d.ts >= b.ts "
            "WHERE b.deal_id = ?", (key,)).fetchone()[0]
    total = len(rows)
    ips = [r["ip"] for r in rows if r["ip"]]
    geo_map = _geolocalizar_ips(ips)
    paises, ciudades = Counter(), Counter()
    for ip in ips:
        g = geo_map.get(ip)
        if g and g.get("country"):
            paises[(g["country"], g.get("code") or "")] += 1
            if g.get("city"):
                ciudades[(g["city"], g.get("code") or "")] += 1
    try:
        from zoneinfo import ZoneInfo
        TZ = ZoneInfo("Europe/Madrid")
    except Exception:
        TZ = None
    heat = [[0] * 24 for _ in range(7)]
    por_hora, por_dow = [0] * 24, [0] * 7
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["ts"])
            if TZ:
                dt = dt.astimezone(TZ)
            heat[dt.weekday()][dt.hour] += 1
            por_hora[dt.hour] += 1
            por_dow[dt.weekday()] += 1
        except Exception:
            pass
    visitantes = len({ip for ip in ips})
    return {
        "slug": slug,
        "clicks_total": total,
        "clicks_unicos_ip": visitantes,
        "geo": {
            "paises":   [{"pais": k[0], "code": k[1], "clicks": v} for k, v in paises.most_common(12)],
            "ciudades": [{"ciudad": k[0], "code": k[1], "clicks": v} for k, v in ciudades.most_common(12)],
            "sin_geo":  total - sum(paises.values()),
        },
        "heatmap": heat,
        "por_hora": por_hora,
        "por_dow": por_dow,
        "conversion": {
            "visitantes": visitantes,
            "con_clic_deal": conv,
            "tasa": round(100 * conv / visitantes) if visitantes else 0,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/admin/login")
def admin_login(body: AdminLoginBody):
    """Autentica al administrador. Setea httpOnly cookie con JWT admin."""
    if not ADMIN_PASSWORD:
        return JSONResponse(
            status_code=503,
            content={"error": "Admin no configurado — añade ADMIN_PASSWORD a .env"}
        )
    ok_user = _hmac.compare_digest(body.username.encode(), ADMIN_USERNAME.encode())
    ok_pass = _hmac.compare_digest(body.password.encode(), ADMIN_PASSWORD.encode())
    if not (ok_user and ok_pass):
        return JSONResponse(status_code=401, content={"error": "Credenciales incorrectas"})
    token = _jwt_create({"role": "admin", "sub": body.username}, JWT_ADMIN_HOURS)
    response = JSONResponse({"ok": True, "expires_in": JWT_ADMIN_HOURS * 3600})
    _set_admin_cookie(response, token)
    return response


@app.get("/admin/me")
def admin_me(request: Request):
    """Verifica si hay sesión admin activa. Usado por el panel para comprobar auth."""
    payload = _require_admin(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})
    return {"username": payload.get("sub", "")}


@app.post("/admin/logout")
def admin_logout():
    """Cierra sesión admin eliminando la cookie httpOnly."""
    response = JSONResponse({"ok": True})
    _clear_admin_cookie(response)
    return response


@app.get("/admin/deals")
def admin_deals(
    request:   Request,
    limit:     int = Query(default=50, ge=1, le=200),
    offset:    int = Query(default=0,  ge=0),
    tipo:      Optional[str] = Query(default=None),
    tienda:    Optional[str] = Query(default=None),
    busqueda:  Optional[str] = Query(default=None),
    expirado:  Optional[int] = Query(default=None),
):
    """Lista deals con métricas de clicks y votos. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    where, params = [], []
    if tipo:
        where.append("d.tipo = ?"); params.append(tipo.upper())
    if tienda:
        where.append("d.tienda = ?"); params.append(tienda)
    if busqueda:
        where.append("d.titulo LIKE ?"); params.append(f"%{busqueda}%")
    if expirado is not None:
        where.append("COALESCE(d.expirado, 0) = ?"); params.append(expirado)

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    sql = f"""
        SELECT
            d.rowid, d.deal_id, d.titulo, d.tienda, d.tipo,
            d.precio          AS precio_actual,
            d.precio_original, d.descuento_pct,
            d.imagen_url,      d.publicado_en,
            d.url_afiliado,
            COALESCE(d.votes_up,      0) AS votes_up,
            COALESCE(d.votes_down,    0) AS votes_down,
            COALESCE(d.categoria,    '') AS categoria,
            COALESCE(d.flags_expirado,0) AS flags_expirado,
            COALESCE(d.expirado,      0) AS expirado,
            COUNT(c.id)                                          AS clicks_total,
            SUM(CASE WHEN c.canal = 'telegram' THEN 1 ELSE 0 END) AS clicks_telegram,
            SUM(CASE WHEN c.canal = 'web'      THEN 1 ELSE 0 END) AS clicks_web
        FROM deals_publicados d
        LEFT JOIN clicks c ON d.deal_id = c.deal_id
        {where_sql}
        GROUP BY d.deal_id
        ORDER BY d.publicado_en DESC
        LIMIT ? OFFSET ?
    """
    params_count = list(params)
    params += [limit, offset]

    with _get_db() as con:
        rows  = con.execute(sql, params).fetchall()
        total = con.execute(
            f"SELECT COUNT(*) FROM deals_publicados d {where_sql}", params_count
        ).fetchone()[0]

    deals = []
    for r in rows:
        d = dict(r)
        d["precio_actual"]   = d["precio_actual"]   or 0.0
        d["precio_original"] = d["precio_original"] or 0.0
        d["descuento_pct"]   = d["descuento_pct"]   or 0
        d["imagen_url"]      = d["imagen_url"]       or ""
        d["clicks_total"]    = d["clicks_total"]     or 0
        d["clicks_telegram"] = d["clicks_telegram"]  or 0
        d["clicks_web"]      = d["clicks_web"]       or 0
        deals.append(d)

    return {"deals": deals, "total": total, "limit": limit, "offset": offset}


@app.delete("/admin/deals/bulk")
def admin_bulk_delete_deals(body: BulkDeleteDealsBody, request: Request):
    """Elimina múltiples deals en una sola operación. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if not body.deal_ids:
        return {"deleted": 0}
    placeholders = ",".join("?" * len(body.deal_ids))
    now = datetime.utcnow().isoformat()
    with _get_db() as con:
        rows = con.execute(
            f"SELECT deal_id, titulo, tienda, precio FROM deals_publicados WHERE deal_id IN ({placeholders})",
            body.deal_ids,
        ).fetchall()
        for row in rows:
            con.execute(
                "INSERT OR IGNORE INTO deals_borrados (deal_id, titulo, tienda, precio, borrado_en) VALUES (?,?,?,?,?)",
                (*row, now),
            )
        deleted = con.execute(
            f"DELETE FROM deals_publicados WHERE deal_id IN ({placeholders})",
            body.deal_ids,
        ).rowcount
        con.commit()
    return {"deleted": deleted}


@app.delete("/admin/deals/{deal_id}")
def admin_delete_deal(deal_id: str, request: Request):
    """Elimina un deal permanentemente. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    now = datetime.utcnow().isoformat()
    with _get_db() as con:
        row = con.execute(
            "SELECT deal_id, titulo, tienda, precio FROM deals_publicados WHERE deal_id = ?", (deal_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "Deal no encontrado"})
        con.execute(
            "INSERT OR IGNORE INTO deals_borrados (deal_id, titulo, tienda, precio, borrado_en) VALUES (?,?,?,?,?)",
            (*row, now),
        )
        con.execute("DELETE FROM deals_publicados WHERE deal_id = ?", (deal_id,))
        con.commit()
    return {"deleted": True, "deal_id": deal_id}


@app.patch("/admin/deals/{deal_id}")
def admin_patch_deal(deal_id: str, body: PatchDealBody, request: Request):
    """Edita título y/o url_afiliado de un deal. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    updates = {}
    if body.titulo is not None:
        updates["titulo"] = body.titulo.strip()
    if body.url_afiliado is not None:
        updates["url_afiliado"] = body.url_afiliado.strip()
    if body.expirado is not None:
        updates["expirado"] = int(body.expirado)
    if not updates:
        return JSONResponse(status_code=400, content={"error": "Nada que actualizar"})
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [deal_id]
    with _get_db() as con:
        updated = con.execute(
            f"UPDATE deals_publicados SET {set_clause} WHERE deal_id = ?", values
        ).rowcount
        con.commit()
    if updated == 0:
        return JSONResponse(status_code=404, content={"error": "Deal no encontrado"})
    return {"updated": True, "deal_id": deal_id, **updates}


# ══════════════════════════════════════════════════════════════════════════════
# PRECIOS — panel de admin
#
# Los 22,6 M de precios que guardamos no se veían por ningún sitio: el endpoint
# /api/price-history existía y no lo llamaba nadie. Esto los hace mirables.
#
# Solo se puede buscar por NOMBRE lo que tiene nombre guardado: Decathlon y
# ToysRus (tablas propias, ~175 k productos) y los deals publicados con hist_pid.
# En price_history nunca guardamos el título, así que sus 1,5 M de productos solo
# son accesibles por id. Es una limitación real, no una decisión de diseño.
# ══════════════════════════════════════════════════════════════════════════════

_FUENTES_PRECIO = {
    # fuente → (tabla_productos, col_id, col_nombre, tabla_precios, tienda_fija)
    "decathlon": ("decathlon_productos", "model_id", "nombre", "decathlon_precios", "Decathlon"),
    "toysrus":   ("toysrus_productos",   "ean",     "nombre", "toysrus_precios",   "ToysRus"),
}


@app.get("/admin/precios/buscar")
def admin_precios_buscar(request: Request, q: str = Query(default="", max_length=80)):
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse(content=[])
    like = f"%{q}%"
    out = []
    with _get_db() as con:
        for fuente, (tprod, cid, cnom, tprec, tienda) in _FUENTES_PRECIO.items():
            try:
                filas = con.execute(f"""
                    SELECT p.{cid} AS id, p.{cnom} AS titulo,
                           (SELECT precio FROM {tprec} x WHERE x.{cid} = p.{cid}
                             ORDER BY x.fecha DESC LIMIT 1) AS precio,
                           (SELECT COUNT(DISTINCT fecha) FROM {tprec} x WHERE x.{cid} = p.{cid}) AS dias
                    FROM {tprod} p WHERE p.{cnom} LIKE ? LIMIT 25
                """, (like,)).fetchall()
            except Exception:
                continue
            for f in filas:
                if f["precio"] is None:
                    continue
                out.append({"fuente": fuente, "id": str(f["id"]), "titulo": f["titulo"],
                            "tienda": tienda, "precio": f["precio"], "dias": f["dias"] or 0})
        # Deals publicados que dejaron rastro al histórico general
        filas = con.execute("""
            SELECT hist_pid AS id, titulo, tienda, precio,
                   (SELECT COUNT(DISTINCT fecha) FROM price_history x
                     WHERE x.asin = d.hist_pid AND x.tienda = d.tienda) AS dias
            FROM deals_publicados d
            WHERE COALESCE(hist_pid,'') != '' AND titulo LIKE ? LIMIT 25
        """, (like,)).fetchall()
        for f in filas:
            out.append({"fuente": "historico", "id": str(f["id"]), "titulo": f["titulo"],
                        "tienda": f["tienda"], "precio": f["precio"], "dias": f["dias"] or 0})
    out.sort(key=lambda x: -x["dias"])
    return JSONResponse(content=out[:40])


@app.get("/admin/precios/serie")
def admin_precios_serie(request: Request, fuente: str = Query(default="historico"),
                        id: str = Query(default=""), tienda: str = Query(default=""),
                        dias: int = Query(default=90, ge=7, le=400)):
    """Serie + la referencia que calcula el pipeline + los días que publicamos."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    if not id:
        return JSONResponse(status_code=400, content={"error": "falta id"})
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).strftime("%Y-%m-%d")

    with _get_db() as con:
        if fuente in _FUENTES_PRECIO:
            _, cid, cnom, tprec, tienda_fija = _FUENTES_PRECIO[fuente]
            tprod = _FUENTES_PRECIO[fuente][0]
            puntos = con.execute(
                f"SELECT fecha, precio FROM {tprec} WHERE {cid} = ? AND fecha >= ? ORDER BY fecha",
                (id, desde)).fetchall()
            row = con.execute(f"SELECT {cnom} AS titulo FROM {tprod} WHERE {cid} = ?", (id,)).fetchone()
            titulo, tienda = (row["titulo"] if row else id), tienda_fija
        else:
            puntos = con.execute(
                "SELECT fecha, precio FROM price_history WHERE asin = ? AND tienda = ? "
                "AND fecha >= ? ORDER BY fecha", (id, tienda, desde)).fetchall()
            row = con.execute("SELECT titulo FROM deals_publicados WHERE hist_pid = ? AND tienda = ? "
                              "ORDER BY publicado_en DESC LIMIT 1", (id, tienda)).fetchone()
            titulo = row["titulo"] if row else id

        # Días en que publicamos este producto: la marca que dice si acertamos
        pubs = con.execute("""
            SELECT substr(publicado_en,1,10) AS fecha, precio, precio_original, descuento_pct
            FROM deals_publicados
            WHERE tienda = ? AND (COALESCE(hist_pid,'') = ? OR ? = '')
              AND substr(publicado_en,1,10) >= ?
            ORDER BY publicado_en
        """, (tienda, id, "" if fuente in _FUENTES_PRECIO else id, desde)).fetchall() \
            if fuente == "historico" else []

    serie = [{"fecha": p["fecha"], "precio": p["precio"]} for p in puntos]
    ref = referencia_de_serie([(p["fecha"], p["precio"]) for p in puntos])
    return JSONResponse(content={
        "titulo": titulo, "tienda": tienda, "fuente": fuente, "id": id,
        "puntos": serie,
        "referencia": ref[0] if ref else None,
        "dias_datos": ref[1] if ref else len({p["fecha"] for p in puntos}),
        "publicaciones": [dict(p) for p in pubs],
    })


@app.get("/admin/precios/salud")
def admin_precios_salud(request: Request):
    """Por tienda: cuánto seguimos, desde cuándo, y si lleva días sin moverse."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    with _get_db() as con:
        filas = con.execute("""
            SELECT tienda, COUNT(*) AS obs, COUNT(DISTINCT asin) AS productos,
                   COUNT(DISTINCT fecha) AS dias, MAX(fecha) AS ultima
            FROM price_history GROUP BY tienda ORDER BY obs DESC
        """).fetchall()
        # ¿se mueven los precios? un catálogo congelado no da ofertas jamás
        movidos = {r["tienda"]: r["n"] for r in con.execute("""
            SELECT tienda, COUNT(*) AS n FROM (
              SELECT tienda, asin FROM price_history
              WHERE fecha >= date('now','-14 day')
              GROUP BY tienda, asin HAVING COUNT(DISTINCT precio) > 1
            ) GROUP BY tienda
        """).fetchall()}
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return JSONResponse(content=[{
        "tienda": r["tienda"], "obs": r["obs"], "productos": r["productos"],
        "dias": r["dias"], "ultima": r["ultima"],
        "al_dia": r["ultima"] == hoy,
        "con_movimiento": movidos.get(r["tienda"], 0),
    } for r in filas])


@app.get("/admin/precios/cobertura")
def admin_precios_cobertura(request: Request):
    """Cuántos productos tienen ya histórico suficiente para poder generar un deal."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    with _get_db() as con:
        filas = con.execute("""
            SELECT tienda,
                   COUNT(*) AS total,
                   SUM(CASE WHEN dias >= 7 THEN 1 ELSE 0 END) AS listos
            FROM (
              SELECT tienda, asin, COUNT(DISTINCT fecha) AS dias
              FROM price_history WHERE fecha >= date('now','-30 day')
              GROUP BY tienda, asin
            ) GROUP BY tienda ORDER BY total DESC
        """).fetchall()
    return JSONResponse(content=[{
        "tienda": r["tienda"], "total": r["total"], "listos": r["listos"] or 0,
        "pct": round((r["listos"] or 0) * 100 / r["total"]) if r["total"] else 0,
    } for r in filas])


@app.get("/admin/stats")
def admin_stats(request: Request):
    """Estadísticas globales: totales, clicks, top deals. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    with _get_db() as con:
        total_deals   = con.execute("SELECT COUNT(*) FROM deals_publicados").fetchone()[0]
        # En catálogo = lo que la web puede mostrar (ventana de 30 días). Sin este
        # dato, admin y web daban cifras distintas sin explicación aparente.
        deals_catalogo = con.execute(
            "SELECT COUNT(*) FROM deals_publicados WHERE publicado_en >= datetime('now','-30 days')"
        ).fetchone()[0]
        today_deals   = con.execute(
            "SELECT COUNT(*) FROM deals_publicados WHERE publicado_en >= date('now')"
        ).fetchone()[0]
        total_clicks  = con.execute("SELECT COUNT(*) FROM clicks").fetchone()[0]
        today_clicks  = con.execute(
            "SELECT COUNT(*) FROM clicks WHERE ts >= date('now')"
        ).fetchone()[0]
        total_users   = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        premium_users = con.execute("SELECT COUNT(*) FROM users WHERE premium = 1").fetchone()[0]

        top_deals = con.execute("""
            SELECT d.deal_id, d.titulo, d.tienda, d.tipo,
                   COALESCE(d.votes_up,   0) AS votes_up,
                   COALESCE(d.votes_down, 0) AS votes_down,
                   COUNT(c.id) AS clicks
            FROM deals_publicados d
            LEFT JOIN clicks c ON d.deal_id = c.deal_id
            GROUP BY d.deal_id
            ORDER BY clicks DESC
            LIMIT 10
        """).fetchall()

        clicks_canal  = con.execute(
            "SELECT canal, COUNT(*) FROM clicks GROUP BY canal ORDER BY 2 DESC"
        ).fetchall()

        deals_tienda = con.execute(
            "SELECT tienda, COUNT(*) FROM deals_publicados GROUP BY tienda ORDER BY 2 DESC"
        ).fetchall()

    return {
        "total_deals":    total_deals,
        "deals_catalogo": deals_catalogo,   # los que la web puede mostrar (30 días)
        "today_deals":    today_deals,
        "total_clicks":  total_clicks,
        "today_clicks":  today_clicks,
        "total_users":   total_users,
        "premium_users": premium_users,
        "top_deals":     [dict(r) for r in top_deals],
        "clicks_canal":  {r[0]: r[1] for r in clicks_canal},
        "deals_tienda":  {r[0]: r[1] for r in deals_tienda},
    }


# ══════════════════════════════════════════════════════════════════════════════
# AUTH / OAUTH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/auth/google")
def auth_google():
    """Inicia el flujo OAuth con Google → redirige al usuario a Google."""
    if not GOOGLE_CLIENT_ID:
        return JSONResponse(status_code=503, content={"error": "Google OAuth no configurado"})
    state = _gen_state()
    qs = urllib.parse.urlencode({
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
        "prompt":        "select_account",
    })
    return RedirectResponse(
        f"https://accounts.google.com/o/oauth2/v2/auth?{qs}", status_code=302
    )


@app.get("/auth/google/callback")
def auth_google_callback(code: str = "", state: str = "", error: str = ""):
    """Callback de Google. Intercambia código por perfil, crea usuario, redirige con JWT."""
    if error:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=google_denied", status_code=302)
    if not _verify_state(state):
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=state_invalido", status_code=302)

    # Intercambiar código → access_token
    try:
        tr = _http.post("https://oauth2.googleapis.com/token", data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        }, timeout=10)
        access_token = tr.json().get("access_token", "")
    except Exception:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=token_exchange", status_code=302)

    if not access_token:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=no_token", status_code=302)

    # Obtener perfil del usuario
    try:
        ur = _http.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        ui = ur.json()
    except Exception:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=userinfo", status_code=302)

    user_id = f"google:{ui.get('id', '')}"
    _upsert_user(user_id, ui.get("email", ""), ui.get("name", ""), ui.get("picture", ""), "google")

    token = _jwt_create({
        "role":     "user",
        "sub":      user_id,
        "email":    ui.get("email",   ""),
        "name":     ui.get("name",    ""),
        "avatar":   ui.get("picture", ""),
        "provider": "google",
    }, JWT_USER_HOURS)

    # Token en la URL para que el frontend (localStorage) lo recoja + cookie httpOnly para API calls.
    sep = "&" if "?" in FRONTEND_CUENTA else "?"
    response = RedirectResponse(f"{FRONTEND_CUENTA}{sep}token={token}", status_code=302)
    _set_user_cookie(response, token)
    return response


@app.get("/auth/apple")
def auth_apple():
    """Inicia el flujo OAuth con Apple → redirige al usuario a Apple."""
    if not APPLE_CLIENT_ID:
        return JSONResponse(status_code=503, content={"error": "Apple OAuth no configurado"})
    state = _gen_state()
    qs = urllib.parse.urlencode({
        "client_id":     APPLE_CLIENT_ID,
        "redirect_uri":  APPLE_REDIRECT_URI,
        "response_type": "code id_token",
        "response_mode": "form_post",
        "scope":         "name email",
        "state":         state,
    })
    return RedirectResponse(
        f"https://appleid.apple.com/auth/authorize?{qs}", status_code=302
    )


@app.post("/auth/apple/callback")
async def auth_apple_callback(request: Request):
    """Callback de Apple Sign In (form_post). Crea usuario y redirige con JWT."""
    try:
        form  = await request.form()
        id_tk = form.get("id_token", "")
        user_json = form.get("user", "")   # solo en el primer inicio de sesión
        error = form.get("error", "")
    except Exception:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=apple_form", status_code=302)

    if error:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=apple_denied", status_code=302)
    if not id_tk:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=apple_no_token", status_code=302)

    # Decodificar payload del id_token de Apple (sin verificar firma — suficiente para MVP)
    try:
        p = id_tk.split('.')[1]
        p += '=' * (-len(p) % 4)
        apple_info = json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=apple_decode", status_code=302)

    sub   = apple_info.get("sub", "")
    email = apple_info.get("email", "")

    name = ""
    if user_json:
        try:
            ud = json.loads(user_json).get("name", {})
            name = f"{ud.get('firstName', '')} {ud.get('lastName', '')}".strip()
        except Exception:
            pass

    user_id = f"apple:{sub}"
    _upsert_user(user_id, email, name or email, "", "apple")

    token = _jwt_create({
        "role":     "user",
        "sub":      user_id,
        "email":    email,
        "name":     name or email,
        "avatar":   "",
        "provider": "apple",
    }, JWT_USER_HOURS)

    sep = "&" if "?" in FRONTEND_CUENTA else "?"
    response = RedirectResponse(f"{FRONTEND_CUENTA}{sep}token={token}", status_code=302)
    _set_user_cookie(response, token)
    return response


@app.get("/auth/me")
def auth_me(request: Request):
    """Devuelve el perfil del usuario autenticado. Requiere JWT de usuario."""
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})

    with _get_db() as con:
        row = con.execute(
            "SELECT email, name, avatar_url, premium, newsletter, created_at FROM users WHERE id = ?",
            (payload["sub"],)
        ).fetchone()

    if not row:
        return JSONResponse(status_code=404, content={"error": "Usuario no encontrado"})

    return {
        "id":         payload["sub"],
        "email":      row["email"],
        "name":       row["name"],
        "avatar_url": row["avatar_url"],
        "premium":    bool(row["premium"]),
        "newsletter": bool(row["newsletter"]),
        "provider":   payload.get("provider", ""),
        "created_at": row["created_at"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# AUTH EMAIL
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/auth/register")
def auth_register(body: RegisterBody):
    """Registro con email y contraseña. Envía email de verificación."""
    email = body.email.lower().strip()
    if not email or not body.password:
        return JSONResponse(status_code=400, content={"error": "Email y contraseña requeridos"})
    if len(body.password) < 8:
        return JSONResponse(status_code=400, content={"error": "La contraseña debe tener al menos 8 caracteres"})

    user_id = f"email:{email}"
    token   = secrets.token_urlsafe(32)
    now     = datetime.now(timezone.utc).isoformat()

    with _get_db() as con:
        existing = con.execute("SELECT id, email_verified FROM users WHERE id = ?", (user_id,)).fetchone()
        if existing:
            if existing["email_verified"]:
                return JSONResponse(status_code=409, content={"error": "Este email ya tiene cuenta. Inicia sesión."})
            # Reenviar verificación
            con.execute("UPDATE users SET verification_token = ? WHERE id = ?", (token, user_id))
        else:
            name = body.name.strip() or email.split("@")[0]
            con.execute("""
                INSERT INTO users
                  (id, email, name, avatar_url, provider, premium, password_hash,
                   email_verified, verification_token, newsletter, created_at, last_login)
                VALUES (?, ?, ?, '', 'email', 0, ?, 0, ?, 0, ?, ?)
            """, (user_id, email, name, _hash_password(body.password), token, now, now))
        con.commit()

    verify_url = f"{API_URL}/auth/verify-email?token={token}"
    html = f"""
    <div style="font-family:monospace;max-width:480px;margin:0 auto;padding:40px 24px">
      <p style="font-family:Georgia,serif;font-size:32px;font-weight:900;margin:0 0 4px">FLIPAZO</p>
      <p style="color:#888;font-size:11px;letter-spacing:.12em;text-transform:uppercase;margin:0 0 32px">El canal de ofertas más flipante de España</p>
      <p style="font-size:14px;color:#222;margin-bottom:24px">Haz clic para verificar tu dirección de email y activar tu cuenta:</p>
      <a href="{verify_url}"
         style="display:inline-block;background:#c0392b;color:#fff;padding:14px 32px;
                text-decoration:none;font-weight:700;font-size:12px;letter-spacing:.1em;text-transform:uppercase">
        VERIFICAR EMAIL →
      </a>
      <p style="color:#bbb;font-size:11px;margin-top:32px">Si no has creado esta cuenta, ignora este mensaje.</p>
    </div>
    """
    _send_email(email, "Verifica tu cuenta de Flipazo", html)
    return {"status": "verification_sent", "email": email}


@app.post("/auth/login/email")
def auth_login_email(body: EmailLoginBody):
    """Login con email y contraseña."""
    email   = body.email.lower().strip()
    user_id = f"email:{email}"

    with _get_db() as con:
        user = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    if not user or not _verify_password(body.password, user["password_hash"] or ""):
        return JSONResponse(status_code=401, content={"error": "Email o contraseña incorrectos"})

    if not user["email_verified"]:
        return JSONResponse(status_code=403, content={"error": "email_not_verified", "email": email})

    with _get_db() as con:
        con.execute("UPDATE users SET last_login = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), user_id))
        con.commit()

    token = _jwt_create({
        "role": "user", "sub": user_id, "email": user["email"],
        "name": user["name"], "avatar": "", "provider": "email",
    }, JWT_USER_HOURS)
    response = JSONResponse({"ok": True, "name": user["name"], "email": user["email"]})
    _set_user_cookie(response, token)
    return response


@app.post("/auth/logout")
def auth_logout():
    """Cierra sesión de usuario eliminando la cookie httpOnly."""
    response = JSONResponse({"ok": True})
    _clear_user_cookie(response)
    return response


# ── Threads OAuth — setup one-time ─────────────────────────────────────────────

@app.get("/auth/threads/start")
def threads_auth_start():
    """Redirige a Threads OAuth. Abre esta URL logueado como la cuenta de Flipazo en Threads."""
    if not THREADS_APP_SECRET:
        return JSONResponse(status_code=503, content={"error": "THREADS_APP_SECRET no configurado en .env"})
    url = (
        "https://threads.net/oauth/authorize"
        f"?client_id={THREADS_APP_ID}"
        f"&redirect_uri={urllib.parse.quote(THREADS_REDIRECT, safe='')}"
        "&scope=threads_basic,threads_content_publish,threads_delete"
        "&response_type=code"
    )
    return RedirectResponse(url, status_code=302)

@app.get("/auth/threads/callback")
def threads_auth_callback(code: str = "", error: str = ""):
    """Callback OAuth de Threads: intercambia code → short-lived → long-lived token y muestra las credenciales."""
    if error:
        return JSONResponse(status_code=400, content={"error": error})
    if not code:
        return JSONResponse(status_code=400, content={"error": "Sin code en la respuesta"})
    if not THREADS_APP_SECRET:
        return JSONResponse(status_code=503, content={"error": "THREADS_APP_SECRET no configurado"})

    # 1. Intercambiar code → short-lived token
    try:
        r1 = _http.post(
            "https://graph.threads.net/oauth/access_token",
            data={
                "client_id":     THREADS_APP_ID,
                "client_secret": THREADS_APP_SECRET,
                "code":          code,
                "grant_type":    "authorization_code",
                "redirect_uri":  THREADS_REDIRECT,
            },
            timeout=15,
        )
        d1 = r1.json()
        short_token = d1.get("access_token", "")
        if not short_token:
            return JSONResponse(status_code=502, content={"error": "No se obtuvo short-lived token", "detalle": d1})
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Error intercambiando code: {e}"})

    # 2. Short-lived → long-lived (60 días)
    try:
        r2 = _http.get(
            "https://graph.threads.net/v1.0/access_token",
            params={
                "grant_type":    "th_exchange_token",
                "client_id":     THREADS_APP_ID,
                "client_secret": THREADS_APP_SECRET,
                "access_token":  short_token,
            },
            timeout=15,
        )
        d2 = r2.json()
        long_token = d2.get("access_token", "")
        expires_in = d2.get("expires_in", "?")
        if not long_token:
            return JSONResponse(status_code=502, content={"error": "No se obtuvo long-lived token", "detalle": d2})
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Error intercambiando long-lived: {e}"})

    # 3. Obtener el User ID
    try:
        r3 = _http.get(
            "https://graph.threads.net/v1.0/me",
            params={"fields": "id,username", "access_token": long_token},
            timeout=15,
        )
        d3 = r3.json()
        user_id  = d3.get("id", "")
        username = d3.get("username", "")
    except Exception as e:
        user_id = username = f"(error: {e})"

    dias = round(int(expires_in) / 86400) if str(expires_in).isdigit() else expires_in
    html = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<title>Threads Auth OK — Flipazo</title>
<style>
  body{{font-family:monospace;max-width:700px;margin:60px auto;padding:0 24px;background:#f8f8f8}}
  h1{{color:#1a1a1a;font-size:22px}}
  .box{{background:#fff;border:2px solid #1a1a1a;border-radius:12px;padding:24px;margin:20px 0}}
  .label{{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#888;margin-bottom:4px}}
  .val{{font-size:14px;word-break:break-all;background:#f0f0f0;padding:10px 14px;border-radius:8px;margin-bottom:16px}}
  .cmd{{background:#111;color:#0f0;padding:16px;border-radius:8px;font-size:12px;white-space:pre-wrap;line-height:1.7}}
  .ok{{color:#00a550;font-size:18px;font-weight:700}}
</style></head><body>
<h1>✅ Threads conectado</h1>
<p class="ok">@{username} — token válido {dias} días</p>
<div class="box">
  <div class="label">THREADS_USER_ID</div>
  <div class="val">{user_id}</div>
  <div class="label">THREADS_TOKEN (long-lived, {dias} días)</div>
  <div class="val">{long_token}</div>
</div>
<div class="box">
  <div class="label">Ejecuta esto en el servidor para activar Threads:</div>
  <div class="cmd">ssh root@204.168.199.253 "echo 'THREADS_USER_ID={user_id}' >> /home/flipazo/app/.env && echo 'THREADS_TOKEN={long_token}' >> /home/flipazo/app/.env && systemctl restart flipazo.service && echo LISTO"</div>
</div>
<p style="color:#888;font-size:12px">⚠️ Guarda el token en un lugar seguro. Este endpoint no lo almacena.</p>
</body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@app.api_route("/auth/threads/deauthorize", methods=["GET", "POST"])
def threads_deauthorize():
    """Callback de desautorización que Meta exige para guardar la config de Threads.
    No almacenamos datos de usuarios de Threads, así que solo acusamos recibo (200)."""
    return JSONResponse(content={"ok": True})


@app.api_route("/auth/threads/delete", methods=["GET", "POST"])
def threads_data_deletion():
    """Callback de solicitud de borrado de datos (obligatorio para la config de Threads).
    Responde en el formato que Meta exige (url de estado + código). No guardamos datos
    personales de Threads, así que no hay nada que borrar."""
    code = secrets.token_hex(8)
    return JSONResponse(content={
        "url": f"https://api.flipazo.es/auth/threads/deletion-status?code={code}",
        "confirmation_code": code,
    })


@app.get("/auth/threads/deletion-status")
def threads_deletion_status(code: str = ""):
    """Estado de una solicitud de borrado. Sin datos personales almacenados → completada."""
    return JSONResponse(content={"code": code, "status": "completed"})


_threads_audience_cache: dict = {"data": None, "ts": 0.0}


@app.get("/threads-audience")
def threads_audience():
    """Demografía de seguidores de Threads (@flipazo.es): total + país/ciudad/edad/género.
    Cachea 1h en memoria — la API de Meta tarda varios segundos en resolver los 4 breakdowns."""
    now = time.time()
    if _threads_audience_cache["data"] and now - _threads_audience_cache["ts"] < 3600:
        return _threads_audience_cache["data"]
    if not THREADS_TOKEN or not THREADS_USER_ID_ENV:
        return JSONResponse(status_code=503, content={"error": "Threads no configurado"})

    base = f"https://graph.threads.net/v1.0/{THREADS_USER_ID_ENV}"
    result: dict = {"followers_count": 0, "country": [], "city": [], "age": [], "gender": []}

    try:
        r = _http.get(f"{base}/threads_insights",
                      params={"metric": "followers_count", "access_token": THREADS_TOKEN}, timeout=15)
        result["followers_count"] = r.json()["data"][0]["total_value"]["value"]
    except Exception:
        pass

    for bd in ("country", "city", "age", "gender"):
        try:
            r = _http.get(f"{base}/threads_insights",
                          params={"metric": "follower_demographics", "breakdown": bd,
                                  "access_token": THREADS_TOKEN}, timeout=20)
            tv = r.json()["data"][0].get("total_value", {})
            filas = tv.get("breakdowns", [{}])[0].get("results", [])
            result[bd] = sorted(
                [{"value": f["dimension_values"][0], "count": f.get("value", 0)} for f in filas],
                key=lambda x: -x["count"],
            )
        except Exception:
            pass

    _threads_audience_cache["data"] = result
    _threads_audience_cache["ts"] = now
    return result


@app.get("/auth/verify-email")
def auth_verify_email(token: str = ""):
    """Verifica el email con el token. Redirige a /cuenta con JWT."""
    if not token:
        return RedirectResponse(f"{FRONTEND_CUENTA}?error=token_invalido", status_code=302)

    with _get_db() as con:
        user = con.execute("SELECT * FROM users WHERE verification_token = ?", (token,)).fetchone()
        if not user:
            return RedirectResponse(f"{FRONTEND_CUENTA}?error=token_invalido", status_code=302)
        con.execute("UPDATE users SET email_verified = 1, verification_token = '' WHERE id = ?", (user["id"],))
        con.commit()

    jwt = _jwt_create({
        "role": "user", "sub": user["id"], "email": user["email"],
        "name": user["name"], "avatar": "", "provider": "email",
    }, JWT_USER_HOURS)
    response = RedirectResponse(FRONTEND_CUENTA, status_code=302)
    _set_user_cookie(response, jwt)
    return response


# ══════════════════════════════════════════════════════════════════════════════
# FAVORITOS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/user/favorites")
def get_favorites(request: Request):
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})

    with _get_db() as con:
        rows = con.execute("""
            SELECT d.rowid, d.deal_id AS id, d.titulo, d.tienda, d.tipo,
                   d.precio AS precio_actual, d.precio_original, d.descuento_pct,
                   d.imagen_url, d.url_afiliado AS url_affiliate,
                   d.precio_wallapop, d.beneficio_neto, d.publicado_en AS timestamp,
                   COALESCE(d.expirado, 0) AS expirado,
                   f.created_at AS saved_at
            FROM favorites f
            JOIN deals_publicados d ON f.deal_id = d.deal_id
            WHERE f.user_id = ?
            ORDER BY f.created_at DESC
        """, (payload["sub"],)).fetchall()

    return JSONResponse(content=[dict(r) for r in rows])


@app.post("/api/user/favorites/{deal_id}")
def add_favorite(deal_id: str, request: Request):
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})

    with _get_db() as con:
        if not con.execute("SELECT 1 FROM deals_publicados WHERE deal_id = ?", (deal_id,)).fetchone():
            return JSONResponse(status_code=404, content={"error": "Deal no encontrado"})
        con.execute(
            "INSERT OR IGNORE INTO favorites (user_id, deal_id, created_at) VALUES (?, ?, ?)",
            (payload["sub"], deal_id, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    return {"saved": True, "deal_id": deal_id}


@app.delete("/api/user/favorites/{deal_id}")
def remove_favorite(deal_id: str, request: Request):
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})

    with _get_db() as con:
        con.execute("DELETE FROM favorites WHERE user_id = ? AND deal_id = ?",
                    (payload["sub"], deal_id))
        con.commit()
    return {"removed": True, "deal_id": deal_id}


# ══════════════════════════════════════════════════════════════════════════════
# "PARA TI" — preferencias del usuario + vinculación con el bot de Telegram
#
# La web filtra en cliente con la MISMA lógica que el resto de filtros (para no
# duplicar las regex de categoría en Python). Aquí solo se guardan/leen las
# preferencias y se gestiona el enlace con Telegram; el resumen diario lo manda
# scripts/telegram_para_ti.py leyendo esta tabla.
# ══════════════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
# Path secreto del webhook: Telegram es el único que debe poder llamarlo.
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET", "")

_PREFS_DEFAULT = {
    "cats": [], "subcats": {}, "stores": [],
    "precio_min": 0, "precio_max": None,
    "tg_enabled": True, "tg_linked": False,
    "email_enabled": False, "email_freq": "semanal", "email_dias": [0],
}

# Frecuencias válidas del boletín. 'dias' = el usuario marca los días concretos.
_EMAIL_FREQS = ("diario", "semanal", "alternos", "dias")


class PrefsBody(BaseModel):
    cats:          Optional[list]  = None
    subcats:       Optional[dict]  = None
    stores:        Optional[list]  = None
    precio_min:    Optional[float] = None
    precio_max:    Optional[float] = None
    tg_enabled:    Optional[bool]  = None
    email_enabled: Optional[bool]  = None
    email_freq:    Optional[str]   = None
    email_dias:    Optional[list]  = None


def _normalizar_subcats(raw) -> dict:
    """
    {categoria: [subcat, ...]} — se puede elegir más de una subcategoría por
    categoría. Acepta también el formato antiguo ({categoria: "subcat"}) para no
    romper las preferencias ya guardadas. Lista vacía o "todas" = sin filtrar.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list] = {}
    for k, v in list(raw.items())[:20]:
        key = str(k)[:40]
        if isinstance(v, list):
            vals = [str(x)[:40] for x in v if isinstance(x, (str, int))][:20]
        elif isinstance(v, (str, int)):
            vals = [str(v)[:40]]                      # formato antiguo: una sola
        else:
            continue
        vals = [x for x in dict.fromkeys(vals) if x and x != "todas"]
        if vals:
            out[key] = vals
    return out


def _prefs_row_to_dict(row) -> dict:
    if not row:
        return dict(_PREFS_DEFAULT)
    def _j(txt, fallback):
        try:
            v = json.loads(txt or "")
            return v if isinstance(v, type(fallback)) else fallback
        except Exception:
            return fallback
    return {
        "cats":       _j(row["cats"], []),
        "subcats":    _normalizar_subcats(_j(row["subcats"], {})),
        "stores":     _j(row["stores"], []),
        "precio_min": row["precio_min"] or 0,
        "precio_max": row["precio_max"],
        "tg_enabled": bool(row["tg_enabled"]),
        "tg_linked":  bool(row["tg_chat_id"]),
        "email_enabled": bool(_col(row, "email_enabled", 0)),
        "email_freq":    (_col(row, "email_freq", "semanal") or "semanal"),
        "email_dias":    _normalizar_dias(_j(_col(row, "email_dias", "[0]"), [])),
    }


def _col(row, nombre, defecto):
    """Lee una columna que puede no existir aún (instalación sin migrar)."""
    try:
        v = row[nombre]
    except (IndexError, KeyError):
        return defecto
    return defecto if v is None else v


def _normalizar_dias(raw) -> list:
    """Días de la semana como enteros 0-6 (0 = lunes), ordenados y sin repetir."""
    out = []
    for x in (raw if isinstance(raw, list) else []):
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= n <= 6:
            out.append(n)
    return sorted(dict.fromkeys(out))


def _get_prefs(user_id: str) -> dict:
    with _get_db() as con:
        row = con.execute("SELECT * FROM user_prefs WHERE user_id = ?", (user_id,)).fetchone()
    return _prefs_row_to_dict(row)


@app.get("/api/user/prefs")
def get_user_prefs(request: Request):
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})
    return _get_prefs(payload["sub"])


@app.put("/api/user/prefs")
def put_user_prefs(body: PrefsBody, request: Request):
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})
    uid  = payload["sub"]
    ahora = datetime.now(timezone.utc).isoformat()
    cur  = _get_prefs(uid)

    cats    = body.cats    if body.cats    is not None else cur["cats"]
    subcats = body.subcats if body.subcats is not None else cur["subcats"]
    stores  = body.stores  if body.stores  is not None else cur["stores"]
    pmin    = body.precio_min if body.precio_min is not None else cur["precio_min"]
    pmax    = body.precio_max if body.precio_max is not None else cur["precio_max"]
    tg_en   = body.tg_enabled if body.tg_enabled is not None else cur["tg_enabled"]
    mail_on = body.email_enabled if body.email_enabled is not None else cur["email_enabled"]
    freq    = body.email_freq    if body.email_freq    is not None else cur["email_freq"]
    dias    = body.email_dias    if body.email_dias    is not None else cur["email_dias"]

    freq = freq if freq in _EMAIL_FREQS else "semanal"
    dias = _normalizar_dias(dias)
    # 'semanal' manda un solo día y 'dias' necesita al menos uno: sin esto el
    # boletín quedaría activado pero no le tocaría nunca, que es peor que no tenerlo.
    if freq == "semanal":
        dias = dias[:1] or [0]
    elif freq == "dias" and not dias:
        freq = "semanal"
        dias = [0]

    # Saneado: listas de strings cortas, precios no negativos y coherentes
    cats    = [str(c)[:40] for c in cats   if isinstance(c, (str, int))][:40]
    stores  = [str(s)[:60] for s in stores if isinstance(s, (str, int))][:60]
    subcats = _normalizar_subcats(subcats)
    try:    pmin = max(0.0, float(pmin or 0))
    except Exception: pmin = 0.0
    if pmax is not None:
        try:    pmax = max(0.0, float(pmax))
        except Exception: pmax = None
    if pmax is not None and pmax < pmin:
        pmin, pmax = pmax, pmin

    with _get_db() as con:
        con.execute("""
            INSERT INTO user_prefs (user_id, cats, subcats, stores, precio_min, precio_max,
                                    tg_enabled, email_enabled, email_freq, email_dias, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                cats=excluded.cats, subcats=excluded.subcats, stores=excluded.stores,
                precio_min=excluded.precio_min, precio_max=excluded.precio_max,
                tg_enabled=excluded.tg_enabled, email_enabled=excluded.email_enabled,
                email_freq=excluded.email_freq, email_dias=excluded.email_dias,
                updated_at=excluded.updated_at
        """, (uid, json.dumps(cats, ensure_ascii=False), json.dumps(subcats, ensure_ascii=False),
              json.dumps(stores, ensure_ascii=False), pmin, pmax, 1 if tg_en else 0,
              1 if mail_on else 0, freq, json.dumps(dias), ahora))
        con.commit()
    return _get_prefs(uid)


# ── Boletín: baja en un clic ──────────────────────────────────────────────────
# El enlace va en cada correo y NO puede pedir iniciar sesión: quien quiere darse
# de baja no va a loguearse para conseguirlo, marca el correo como spam. El token
# es un HMAC del user_id, así que no se puede dar de baja a otro adivinando su id.

def firmar_baja(user_id: str) -> str:
    firma = _hmac.new(JWT_SECRET.encode(), f"baja:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{firma}"


@app.get("/api/newsletter/baja")
def baja_newsletter(t: str = ""):
    uid, _, firma = (t or "").partition(".")
    esperada = _hmac.new(JWT_SECRET.encode(), f"baja:{uid}".encode(), hashlib.sha256).hexdigest()[:32]
    if not uid or not _hmac.compare_digest(firma, esperada):
        return HTMLResponse(_pagina_baja("Este enlace no es válido.", ok=False), status_code=400)
    with _get_db() as con:
        con.execute("UPDATE user_prefs SET email_enabled = 0, updated_at = ? WHERE user_id = ?",
                    (datetime.now(timezone.utc).isoformat(), uid))
        con.commit()
    return HTMLResponse(_pagina_baja("Listo. No volverás a recibir el boletín «Para ti»."))


def _pagina_baja(mensaje: str, ok: bool = True) -> str:
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Boletín · Flipazo</title>
<style>body{{font-family:system-ui,sans-serif;background:#FEFEF8;color:#111827;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0;padding:24px}}
.c{{max-width:420px;text-align:center}}h1{{font-size:20px;margin:0 0 10px}}
p{{color:#6B7280;line-height:1.5;margin:0 0 20px}}
a{{display:inline-block;background:#F52834;color:#fff;text-decoration:none;font-weight:700;
padding:11px 20px;border-radius:9999px}}</style></head><body><div class="c">
<h1>{'👋 Hasta luego' if ok else '⚠️ Enlace no válido'}</h1><p>{mensaje}</p>
<a href="https://flipazo.es">Volver a Flipazo</a></div></body></html>"""


# ── Telegram: vinculación por código ──────────────────────────────────────────

_tg_bot_username = None

def _tg_username() -> str:
    """Nombre del bot (para construir el deep link). Se cachea tras el primer getMe."""
    global _tg_bot_username
    if _tg_bot_username is not None:
        return _tg_bot_username
    _tg_bot_username = ""
    if TELEGRAM_TOKEN:
        try:
            r = _http.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=8)
            if r.ok:
                _tg_bot_username = r.json().get("result", {}).get("username", "") or ""
        except Exception:
            pass
    return _tg_bot_username


def _tg_send(chat_id: str, texto: str, markup: dict | None = None) -> bool:
    if not TELEGRAM_TOKEN or not chat_id:
        return False
    try:
        data = {"chat_id": chat_id, "text": texto,
                "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            data["reply_markup"] = json.dumps(markup)
        r = _http.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                       data=data, timeout=12)
        return r.ok
    except Exception:
        return False


# ── Categorías elegibles desde el bot ─────────────────────────────────────────
# Mismos ids que CATEGORIAS en index.html: las preferencias son las MISMAS que las
# de la web, así que lo que elija aquí se ve allí y al revés. "herramientas" está
# oculta en la web y "todas" no es una categoría, así que no salen.
_CATS_BOT = [
    ("tecnologia", "Tecnología"), ("moda", "Moda"), ("deportes", "Deportes"),
    ("hogar", "Hogar"), ("belleza", "Cuidado personal"), ("juguetes", "Juguetes"),
    ("mascotas", "Mascotas"),
]
_CATS_VALIDAS = {c for c, _ in _CATS_BOT}
_CAT_LABEL = dict(_CATS_BOT)

# El pipeline guarda algunas categorías con más detalle del que la web enseña
# (1.083 deals son "calzado" y 361 "low_cost"). Sin esto, quien elija Moda no
# vería ni una zapatilla, que es justo lo que más publicamos.
_CAT_ALIAS = {"calzado": {"moda", "deportes"}, "low_cost": set(), "otras": set()}


def _cat_encaja(cat_deal: str, elegidas: list) -> bool:
    if not elegidas:
        return True                       # sin filtro = todo
    cat = (cat_deal or "").strip()
    if cat in _CAT_ALIAS:
        return bool(_CAT_ALIAS[cat] & set(elegidas))
    return cat in elegidas


def _teclado_categorias(elegidas: list) -> dict:
    """Dos columnas de interruptores + fila de acciones. El estado se ve en el
    propio botón, así que no hace falta explicar nada."""
    filas, fila = [], []
    for cid, label in _CATS_BOT:
        marca = "✅" if cid in elegidas else "▫️"
        fila.append({"text": f"{marca} {label}", "callback_data": f"c:{cid}"})
        if len(fila) == 2:
            filas.append(fila); fila = []
    if fila:
        filas.append(fila)
    filas.append([
        {"text": "🔄 Todas", "callback_data": "c:_todas"},
        {"text": "✔️ Listo", "callback_data": "c:_fin"},
    ])
    return {"inline_keyboard": filas}


def _texto_intereses(elegidas: list) -> str:
    if elegidas:
        nombres = ", ".join(_CAT_LABEL.get(c, c) for c in elegidas)
        cuerpo = f"Ahora mismo te mando: <b>{nombres}</b>."
    else:
        cuerpo = "Ahora mismo te mando <b>todas las categorías</b>."
    return (f"⚙️ <b>Tus intereses</b>\n\n{cuerpo}\n\n"
            f"Toca una categoría para activarla o quitarla.")


def _prefs_de_chat(chat_id: str):
    """Devuelve (user_id, prefs) del chat vinculado, o (None, None)."""
    with _get_db() as con:
        row = con.execute("SELECT user_id FROM user_prefs WHERE tg_chat_id = ? "
                          "AND COALESCE(tg_chat_id,'') != ''", (chat_id,)).fetchone()
    if not row:
        return None, None
    return row["user_id"], _get_prefs(row["user_id"])


def _guardar_cats(user_id: str, cats: list) -> None:
    with _get_db() as con:
        con.execute("UPDATE user_prefs SET cats = ?, updated_at = ? WHERE user_id = ?",
                    (json.dumps(cats, ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat(), user_id))
        con.commit()


def _tg_editar(chat_id: str, message_id: int, texto: str, markup: dict | None = None) -> None:
    try:
        data = {"chat_id": chat_id, "message_id": message_id, "text": texto,
                "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            data["reply_markup"] = json.dumps(markup)
        _http.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
                   data=data, timeout=12)
    except Exception:
        pass


def _tg_responder_boton(cb_id: str, texto: str = "") -> None:
    """Telegram deja el botón 'cargando' hasta que se contesta al callback."""
    try:
        _http.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                   data={"callback_query_id": cb_id, "text": texto}, timeout=8)
    except Exception:
        pass


def _ofertas_para(prefs: dict, limite: int = 5) -> list:
    """Los mejores chollos vivos que encajan con sus preferencias."""
    cats   = prefs.get("cats") or []
    stores = prefs.get("stores") or []
    pmin   = prefs.get("precio_min") or 0
    pmax   = prefs.get("precio_max")
    with _get_db() as con:
        filas = con.execute("""
            SELECT deal_id, titulo, tienda, precio, precio_original, descuento_pct,
                   COALESCE(categoria,'') AS categoria
            FROM deals_publicados
            WHERE COALESCE(expirado,0) = 0
              AND publicado_en > datetime('now','-7 day')
            ORDER BY descuento_pct DESC, publicado_en DESC
            LIMIT 400
        """).fetchall()
    out = []
    for d in filas:
        if not _cat_encaja(d["categoria"], cats):
            continue
        if stores and d["tienda"] not in stores:
            continue
        p = d["precio"] or 0
        if pmin and p < pmin:
            continue
        if pmax is not None and p > pmax:
            continue
        out.append(d)
        if len(out) >= limite:
            break
    return out


def _fmt_eur(v) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


@app.post("/api/user/telegram/link")
def telegram_link(request: Request):
    """Genera (o reutiliza) el código de vinculación y devuelve el deep link del bot."""
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})
    uid   = payload["sub"]
    ahora = datetime.now(timezone.utc).isoformat()

    with _get_db() as con:
        row  = con.execute("SELECT tg_code FROM user_prefs WHERE user_id = ?", (uid,)).fetchone()
        code = (row["tg_code"] if row else "") or secrets.token_urlsafe(9).replace("-", "_")
        con.execute("""
            INSERT INTO user_prefs (user_id, tg_code, updated_at) VALUES (?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET tg_code=excluded.tg_code, updated_at=excluded.updated_at
        """, (uid, code, ahora))
        con.commit()

    user = _tg_username()
    if not user:
        return JSONResponse(status_code=503,
                            content={"error": "Bot de Telegram no disponible ahora mismo"})
    return {"url": f"https://t.me/{user}?start={code}", "bot": user, "code": code}


@app.post("/api/user/telegram/unlink")
def telegram_unlink(request: Request):
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})
    with _get_db() as con:
        con.execute("UPDATE user_prefs SET tg_chat_id='', updated_at=? WHERE user_id=?",
                    (datetime.now(timezone.utc).isoformat(), payload["sub"]))
        con.commit()
    return {"unlinked": True}


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Recibe los mensajes del bot. Solo /start <código>, /stop y /ayuda."""
    if not TG_WEBHOOK_SECRET or not _hmac.compare_digest(secret, TG_WEBHOOK_SECRET):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    try:
        upd = await request.json()
    except Exception:
        return {"ok": True}

    # ── Pulsaciones de botón (selector de categorías) ─────────────────────────
    cb = upd.get("callback_query")
    if cb:
        cb_id   = cb.get("id", "")
        chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id") or "")
        msg_id  = (cb.get("message") or {}).get("message_id")
        dato    = (cb.get("data") or "")
        if not chat_id or not dato.startswith("c:"):
            _tg_responder_boton(cb_id)
            return {"ok": True}
        uid, prefs = _prefs_de_chat(chat_id)
        if not uid:
            _tg_responder_boton(cb_id, "Vincula tu cuenta primero")
            return {"ok": True}
        cats = [c for c in (prefs.get("cats") or []) if c in _CATS_VALIDAS]
        accion = dato[2:]
        if accion == "_fin":
            _tg_responder_boton(cb_id, "Guardado")
            _tg_editar(chat_id, msg_id, _texto_intereses(cats).replace(
                "Toca una categoría para activarla o quitarla.",
                "Cambia esto cuando quieras con /intereses."))
            return {"ok": True}
        if accion == "_todas":
            cats = []
        elif accion in _CATS_VALIDAS:
            cats = [c for c in cats if c != accion] if accion in cats else cats + [accion]
        _guardar_cats(uid, cats)
        _tg_responder_boton(cb_id)
        _tg_editar(chat_id, msg_id, _texto_intereses(cats), _teclado_categorias(cats))
        return {"ok": True}

    msg     = upd.get("message") or upd.get("edited_message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    texto   = (msg.get("text") or "").strip()
    if not chat_id or not texto:
        return {"ok": True}

    ahora = datetime.now(timezone.utc).isoformat()

    if texto.startswith("/start"):
        partes = texto.split(maxsplit=1)
        code   = partes[1].strip() if len(partes) > 1 else ""
        if not code:
            _tg_send(chat_id, "Para recibir tus ofertas, entra en <b>flipazo.es</b> → Para ti "
                              "y pulsa «Conectar con Telegram».")
            return {"ok": True}
        with _get_db() as con:
            row = con.execute("SELECT user_id FROM user_prefs WHERE tg_code = ?", (code,)).fetchone()
            if not row:
                _tg_send(chat_id, "Ese enlace ya no es válido. Genera uno nuevo desde flipazo.es → Para ti.")
                return {"ok": True}
            # Un chat de Telegram solo puede estar vinculado a una cuenta
            con.execute("UPDATE user_prefs SET tg_chat_id='' WHERE tg_chat_id = ?", (chat_id,))
            con.execute("UPDATE user_prefs SET tg_chat_id=?, tg_enabled=1, updated_at=? WHERE user_id=?",
                        (chat_id, ahora, row["user_id"]))
            con.commit()
            prefs = _get_prefs(row["user_id"])
        cats = ", ".join(prefs["cats"]) if prefs["cats"] else "todas las categorías"
        _tg_send(chat_id,
                 "✅ <b>Cuenta vinculada.</b>\n\n"
                 f"Cada día te mando un resumen con los mejores chollos de: <b>{cats}</b>.\n\n"
                 "Cambia tus intereses en flipazo.es → Para ti.\n"
                 "Escribe /stop cuando quieras dejar de recibirlos.")
        return {"ok": True}

    if texto.startswith("/stop"):
        with _get_db() as con:
            con.execute("UPDATE user_prefs SET tg_enabled=0, updated_at=? WHERE tg_chat_id=?", (ahora, chat_id))
            con.commit()
        _tg_send(chat_id, "🔕 Listo, no te mando más resúmenes. Escribe /start para volver a activarlos.")
        return {"ok": True}

    # ── /intereses — elegir categorías sin salir de Telegram ──────────────────
    if texto.startswith("/intereses") or texto.startswith("/categorias"):
        uid, prefs = _prefs_de_chat(chat_id)
        if not uid:
            _tg_send(chat_id, "Primero vincula tu cuenta desde <b>flipazo.es → Para ti</b>.")
            return {"ok": True}
        cats = [c for c in (prefs.get("cats") or []) if c in _CATS_VALIDAS]
        _tg_send(chat_id, _texto_intereses(cats), _teclado_categorias(cats))
        return {"ok": True}

    # ── /ofertas — lo suyo, bajo demanda ──────────────────────────────────────
    if texto.startswith("/ofertas") or texto.startswith("/chollos"):
        uid, prefs = _prefs_de_chat(chat_id)
        if not uid:
            _tg_send(chat_id, "Primero vincula tu cuenta desde <b>flipazo.es → Para ti</b>.")
            return {"ok": True}
        deals = _ofertas_para(prefs)
        if not deals:
            _tg_send(chat_id, "Ahora mismo no hay nada que encaje con lo tuyo.\n\n"
                              "Prueba a ampliar categorías con /intereses.")
            return {"ok": True}
        partes = ["🔥 <b>Lo tuyo de esta semana</b>\n"]
        for d in deals:
            pct  = d["descuento_pct"] or 0
            ori  = (f" <s>{_fmt_eur(d['precio_original'])}</s>"
                    if (d["precio_original"] or 0) > (d["precio"] or 0) else "")
            partes.append(
                f"<b>−{pct}%</b> · {(d['titulo'] or '')[:70]}\n"
                f"<b>{_fmt_eur(d['precio'] or 0)}</b>{ori} · {d['tienda']}\n"
                f"https://flipazo.es/r/{d['deal_id']}?canal=telegram_ofertas\n")
        _tg_send(chat_id, "\n".join(partes))
        return {"ok": True}

    # ── /pausar — parar sin darse de baja ─────────────────────────────────────
    if texto.startswith("/pausar"):
        with _get_db() as con:
            con.execute("UPDATE user_prefs SET tg_enabled=0, updated_at=? WHERE tg_chat_id=?",
                        (ahora, chat_id))
            con.commit()
        _tg_send(chat_id, "⏸️ Pausado. No te mando el resumen diario.\n\n"
                          "Sigues vinculado y con tus intereses guardados: escribe "
                          "/start cuando quieras retomarlo, o /ofertas para mirar sin esperar.")
        return {"ok": True}

    _tg_send(chat_id,
             "Puedo hacer esto:\n\n"
             "/ofertas — lo tuyo de esta semana, ahora mismo\n"
             "/intereses — elegir qué categorías te mando\n"
             "/pausar — parar el resumen sin darte de baja\n"
             "/stop — dejar de recibir\n\n"
             "El resto de filtros (tiendas y precio) están en flipazo.es → Para ti.")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# NEWSLETTER
# ══════════════════════════════════════════════════════════════════════════════

@app.patch("/api/user/newsletter")
def toggle_newsletter(body: NewsletterBody, request: Request):
    payload = _require_user(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "No autenticado"})

    with _get_db() as con:
        con.execute("UPDATE users SET newsletter = ? WHERE id = ?",
                    (1 if body.subscribed else 0, payload["sub"]))
        con.commit()
    return {"newsletter": body.subscribed}


# ══════════════════════════════════════════════════════════════════════════════
# BLOG — público
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/blog")
def list_blog_posts(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    """Devuelve posts publicados ordenados del más reciente al más antiguo."""
    with _get_db() as con:
        rows = con.execute(
            "SELECT id, slug, titulo, resumen, imagen_url, created_at, updated_at, "
            "meta_description, tags, og_title, schema_type "
            "FROM blog_posts WHERE publicado = 1 ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        total = con.execute("SELECT COUNT(*) FROM blog_posts WHERE publicado = 1").fetchone()[0]
    return {"posts": [dict(r) for r in rows], "total": total}


@app.get("/blog/{slug}")
def get_blog_post(slug: str):
    """Devuelve un post por slug (solo si está publicado)."""
    with _get_db() as con:
        row = con.execute(
            "SELECT * FROM blog_posts WHERE slug = ? AND publicado = 1", (slug,)
        ).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Post no encontrado"})
    return dict(row)


# ══════════════════════════════════════════════════════════════════════════════
# BLOG — admin
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/blog")
def admin_list_blog(request: Request):
    """Lista todos los posts con contenido completo (publicados + borradores). Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    with _get_db() as con:
        rows = con.execute(
            "SELECT id, slug, titulo, resumen, contenido, imagen_url, publicado, "
            "created_at, updated_at, meta_description, tags, og_title, schema_type "
            "FROM blog_posts ORDER BY created_at DESC"
        ).fetchall()
    return {"posts": [dict(r) for r in rows]}


@app.post("/admin/blog")
def create_blog_post(body: BlogPostBody, request: Request):
    """Crea un nuevo post. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _get_db() as con:
            cur = con.execute(
                "INSERT INTO blog_posts "
                "(slug, titulo, resumen, contenido, imagen_url, publicado, created_at, updated_at, "
                "meta_description, tags, og_title, schema_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (body.slug, body.titulo, body.resumen, body.contenido,
                 body.imagen_url, 1 if body.publicado else 0, now, now,
                 body.meta_description, body.tags, body.og_title, body.schema_type or "Article")
            )
            con.commit()
            post_id = cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            return JSONResponse(status_code=409, content={"error": f"El slug '{body.slug}' ya existe"})
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"id": post_id, "slug": body.slug, "created_at": now}


@app.put("/admin/blog/{post_id}")
def update_blog_post(post_id: int, body: BlogPostBody, request: Request):
    """Actualiza un post existente. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    now = datetime.now(timezone.utc).isoformat()
    with _get_db() as con:
        updated = con.execute(
            "UPDATE blog_posts SET slug=?, titulo=?, resumen=?, contenido=?, "
            "imagen_url=?, publicado=?, updated_at=?, "
            "meta_description=?, tags=?, og_title=?, schema_type=? WHERE id=?",
            (body.slug, body.titulo, body.resumen, body.contenido,
             body.imagen_url, 1 if body.publicado else 0, now,
             body.meta_description, body.tags, body.og_title, body.schema_type or "Article",
             post_id)
        ).rowcount
        con.commit()
    if not updated:
        return JSONResponse(status_code=404, content={"error": "Post no encontrado"})
    return {"id": post_id, "updated_at": now}


@app.delete("/admin/blog/{post_id}")
def delete_blog_post(post_id: int, request: Request):
    """Elimina un post. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    with _get_db() as con:
        deleted = con.execute("DELETE FROM blog_posts WHERE id = ?", (post_id,)).rowcount
        con.commit()
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "Post no encontrado"})
    return {"deleted": True, "id": post_id}


# ══════════════════════════════════════════════════════════════════════════════
# PÁGINAS ESTÁTICAS EDITABLES (Sobre, etc.)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/paginas/{slug}")
def get_pagina_public(slug: str):
    """Devuelve el contenido JSON de una página editable (público)."""
    with _get_db() as con:
        row = con.execute("SELECT content FROM paginas WHERE slug = ?", (slug,)).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Página no encontrada"})
    import json as _json
    try:
        return {"slug": slug, "content": _json.loads(row["content"])}
    except Exception:
        return {"slug": slug, "content": {}}


@app.get("/admin/paginas/{slug}")
def get_pagina_admin(slug: str, request: Request):
    """Lee el contenido de una página editable. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    with _get_db() as con:
        row = con.execute("SELECT content, updated_at FROM paginas WHERE slug = ?", (slug,)).fetchone()
    if not row:
        return {"slug": slug, "content": {}, "updated_at": ""}
    import json as _json
    try:
        return {"slug": slug, "content": _json.loads(row["content"]), "updated_at": row["updated_at"]}
    except Exception:
        return {"slug": slug, "content": {}, "updated_at": ""}


@app.put("/admin/paginas/{slug}")
async def put_pagina_admin(slug: str, request: Request):
    """Guarda el contenido JSON de una página editable. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    import json as _json
    body = await request.json()
    content = body.get("content", {})
    now = datetime.utcnow().isoformat()
    with _get_db() as con:
        con.execute(
            "INSERT INTO paginas (slug, content, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
            (slug, _json.dumps(content, ensure_ascii=False), now)
        )
        con.commit()
    return {"slug": slug, "updated_at": now}


@app.get("/admin/users")
def admin_users(request: Request, limit: int = 100, offset: int = 0, q: str = ""):
    """Lista de usuarios registrados. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    with _get_db() as con:
        if q:
            pattern = f"%{q}%"
            rows = con.execute(
                """SELECT id, email, name, avatar_url, provider, premium,
                          newsletter, email_verified, created_at, last_login
                   FROM users
                   WHERE email LIKE ? OR name LIKE ?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (pattern, pattern, limit, offset)
            ).fetchall()
            total = con.execute(
                "SELECT COUNT(*) FROM users WHERE email LIKE ? OR name LIKE ?",
                (pattern, pattern)
            ).fetchone()[0]
        else:
            rows = con.execute(
                """SELECT id, email, name, avatar_url, provider, premium,
                          newsletter, email_verified, created_at, last_login
                   FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (limit, offset)
            ).fetchall()
            total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    cols = ["id", "email", "name", "avatar_url", "provider", "premium",
            "newsletter", "email_verified", "created_at", "last_login"]
    users = [dict(zip(cols, r)) for r in rows]
    return {"users": users, "total": total, "limit": limit, "offset": offset}


# ── WhatsApp Cloud API — webhook opt-in ────────────────────────────────────────

@app.get("/wa/webhook")
def wa_webhook_verify(request: Request):
    """Verificación del webhook por parte de Meta. GET con hub.challenge."""
    mode      = request.query_params.get("hub.mode")
    token     = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return JSONResponse(status_code=403, content={"error": "Token inválido"})


# ── Bot WhatsApp: deals por categoría a demanda ("envía CHOLLO al 2020") ───────
# trigger = lo que el usuario escribe; deal = patrón para encontrar deals; cat = columna categoria.
_WA_CATS = [
    {"nombre": "Bicicletas", "emoji": "🚲",
     "trigger": _re.compile(r'\b(bici|bicicleta|mtb|gravel|e-?bike|ciclism)', _re.I),
     "deal":    _re.compile(r'\b(bici|bicicleta|mtb|gravel|e-?bike|ebike)\b', _re.I),
     "tiendas": {"Mammoth Bikes"}},
    {"nombre": "Videojuegos", "emoji": "🎮",
     "trigger": _re.compile(r'\b(videojuego|video\s?juego|juego|gaming|consola|ps5|ps4|xbox|nintendo|switch)', _re.I),
     "deal":    _re.compile(r'\b(ps5|ps4|playstation|xbox|nintendo|switch|dualsense|mando|consola|gaming|videojuego)\b', _re.I),
     "tiendas": set()},
    {"nombre": "Televisores", "emoji": "📺",
     "trigger": _re.compile(r'\b(tv|tele|televis|smart\s?tv)', _re.I),
     "deal":    _re.compile(r'\b(tv|televisor|oled|qled|smart\s?tv|proyector)\b', _re.I),
     "tiendas": set()},
    {"nombre": "Audio", "emoji": "🎧",
     "trigger": _re.compile(r'\b(audio|auricular|cascos|altavoz|sonido|airpods)', _re.I),
     "deal":    _re.compile(r'\b(auricular|cascos|altavoz|barra de sonido|airpods|earbuds|soundbar)\b', _re.I),
     "tiendas": set()},
    {"nombre": "Hogar", "emoji": "🏠",
     "trigger": _re.compile(r'\b(hogar|casa|cocina|electrodom)', _re.I),
     "deal":    _re.compile(r'\b(aspirador|robot|cafetera|freidora|air.?fryer|batidora|microondas|sart[eé]n|olla|plancha|colch[oó]n|sof[aá]|exprimidor|tostador|licuadora|lavadora|secadora|nevera|frigor[ií]fico|horno|vitrocer)', _re.I),
     "cat": "hogar", "tiendas": set()},
    {"nombre": "Tecnología", "emoji": "💻",
     "trigger": _re.compile(r'\b(tecnolog|tech|inform[aá]tic|ordenador|gadget|m[oó]vil|port[aá]til|tablet)', _re.I),
     "deal":    _re.compile(r'\b(port[aá]til|laptop|m[oó]vil|smartphone|tablet|monitor|teclado|rat[oó]n|smartwatch|ssd|disco duro|router|webcam|impresora|gr[aá]fica|procesador)\b', _re.I),
     "cat": "tecnologia", "tiendas": {"PcComponentes", "PCBox"}},
    {"nombre": "Deportes", "emoji": "⚽",
     "trigger": _re.compile(r'\b(deporte|fitness|gym|running|correr|monta[ñn]a|outdoor)', _re.I),
     "deal":    _re.compile(r'\b(mancuerna|fitness|running|trekking|monta[ñn]a|nataci[oó]n|f[uú]tbol|camping|mochila)', _re.I),
     "cat": "deportes", "tiendas": {"Decathlon", "PrivateSportShop"}},
    {"nombre": "Belleza y cuidado", "emoji": "💄",
     "trigger": _re.compile(r'\b(belleza|cuidado|perfum|cosm[eé]tic|maquillaje|peluquer)', _re.I),
     "deal":    _re.compile(r'\b(perfume|colonia|maquillaje|afeitadora|depiladora|secador|plancha de pelo|cepillo dental|crema|s[eé]rum)', _re.I),
     "cat": "belleza", "tiendas": set()},
    {"nombre": "Juguetes", "emoji": "🧸",
     "trigger": _re.compile(r'\b(juguete|jugar|peque|infantil)', _re.I),
     "deal":    _re.compile(r'\b(lego|playmobil|mu[ñn]eca|juguete|puzzle|juego de mesa|hot wheels|nerf|barbie|funko)', _re.I),
     "cat": "juguetes", "tiendas": {"ToysRus"}},
    {"nombre": "Herramientas", "emoji": "🔧",
     "trigger": _re.compile(r'\b(herramienta|bricolaje|taladr|brico)', _re.I),
     "deal":    _re.compile(r'\b(taladro|atornillador|makita|dewalt|sierra|lijadora|amoladora|destornillador|caja de herramienta)', _re.I),
     "tiendas": set()},
]
_WA_STOP = {"los","las","una","unos","unas","del","para","con","que","las","mejores","ofertas",
            "oferta","chollos","chollo","deals","deal","quiero","dame","enviame","envíame","busco",
            "tienes","hay","algun","algún","alguna","mas","más","por","favor"}

def _wa_buscar_deals(texto: str):
    """Devuelve (nombre, emoji, [rows]) según la categoría pedida, o búsqueda libre."""
    cat = next((c for c in _WA_CATS if c["trigger"].search(texto)), None)
    with _get_db() as con:
        rows = con.execute(
            "SELECT deal_id, titulo, precio, precio_original, descuento_pct, tienda, COALESCE(categoria,'') AS categoria "
            "FROM deals_publicados WHERE COALESCE(expirado,0)=0 ORDER BY publicado_en DESC LIMIT 700"
        ).fetchall()
    if cat:
        vistos, out = set(), []
        # Pasada 1: coincidencias precisas (título o tienda específica)
        for r in rows:
            if cat["deal"].search(r["titulo"] or "") or r["tienda"] in cat["tiendas"]:
                out.append(r); vistos.add(r["deal_id"])
                if len(out) >= 5:
                    break
        # Pasada 2: rellenar con la columna categoria si faltan
        if len(out) < 5 and cat.get("cat"):
            for r in rows:
                if r["deal_id"] not in vistos and r["categoria"] == cat["cat"]:
                    out.append(r)
                    if len(out) >= 5:
                        break
        return (cat["nombre"], cat["emoji"], out)
    # Sin categoría conocida → búsqueda libre por las palabras del mensaje
    palabras = [w for w in _re.findall(r'[a-zñáéíóú0-9]{3,}', texto.lower()) if w not in _WA_STOP]
    if palabras:
        out = []
        for r in rows:
            tl = (r["titulo"] or "").lower()
            if any(w in tl for w in palabras):
                out.append(r)
                if len(out) >= 5:
                    break
        if out:
            return ("tu búsqueda", "🔎", out)
    return None

def _wa_formatear_deals(nombre: str, emoji: str, deals: list) -> str:
    if not deals:
        return (f"{emoji} No tengo chollos de *{nombre}* ahora mismo 😕\n\n"
                "Prueba con: bicicletas, videojuegos, TV, audio, hogar, tecnología, "
                "deportes, belleza, juguetes o herramientas.")
    lineas = [f"{emoji} *Top {len(deals)} chollos de {nombre}:*", ""]
    for i, d in enumerate(deals, 1):
        precio = float(d["precio"] or 0)
        orig   = float(d["precio_original"] or 0)
        desc   = int(d["descuento_pct"] or 0)
        link   = f"https://flipazo.es/r/{d['deal_id']}?canal=whatsapp"
        precio_txt = (f"~{orig:.0f}€~ → *{precio:.2f}€*  (-{desc}%)" if orig > precio
                      else f"*{precio:.2f}€*")
        lineas.append(f"{i}. {(d['titulo'] or '')[:70]}")
        lineas.append(f"   {precio_txt}")
        lineas.append(f"   🛒 {link}")
        lineas.append("")
    lineas.append("_Pídeme otra categoría cuando quieras_ 😉")
    return "\n".join(lineas)

def _wa_ayuda() -> str:
    return ("👋 ¡Hola! Soy *Flipazo*, tu buscador de chollos verificados.\n\n"
            "📲 *Escríbeme una categoría* y te mando los 5 mejores chollos:\n"
            "🚲 bicicletas · 🎮 videojuegos · 📺 TV · 🎧 audio\n"
            "🏠 hogar · 💻 tecnología · ⚽ deportes · 💄 belleza\n"
            "🧸 juguetes · 🔧 herramientas\n\n"
            "O dime qué buscas (ej. _auriculares Sony_) y lo busco por ti.\n\n"
            "• *ALTA* → recibe los mejores deals del día\n"
            "• *BAJA* → darte de baja")


@app.post("/wa/webhook")
async def wa_webhook_message(request: Request):
    """
    Recibe mensajes entrantes de WhatsApp vía Meta webhook.
    Comandos reconocidos:
      ALTA   → suscribirse a alertas de deals
      BAJA   → desuscribirse
      (otro) → mensaje de bienvenida con instrucciones
    """
    try:
        body = await request.json()
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return {"ok": True}

        msg = messages[0]
        from_number = msg.get("from", "")
        text_raw = (msg.get("text") or {}).get("body", "").strip()
        text = text_raw.upper()

        if not from_number:
            return {"ok": True}

        now = datetime.now(timezone.utc).isoformat()
        if text in ("ALTA", "SUSCRIBIR", "START"):
            with _get_db() as con:
                con.execute(
                    "INSERT INTO wa_suscriptores (telefono, alta_en, activo) "
                    "VALUES (?, ?, 1) ON CONFLICT(telefono) DO UPDATE SET activo=1, baja_en=NULL",
                    (from_number, now),
                )
            _wa_responder(from_number, "✅ ¡Suscrito a Flipazo!\n\nTe avisaremos de los mejores chollos del día.\nEscríbeme una categoría (ej. *videojuegos*) y te mando los 5 mejores. Para baja responde BAJA.")
        elif text in ("BAJA", "STOP", "UNSUBSCRIBE"):
            with _get_db() as con:
                con.execute(
                    "UPDATE wa_suscriptores SET activo=0, baja_en=? WHERE telefono=?",
                    (now, from_number),
                )
            _wa_responder(from_number, "✅ Dado de baja. Responde ALTA cuando quieras volver.")
        elif text in ("HOLA", "INFO", "AYUDA", "HELP", "MENU", "MENÚ"):
            _wa_responder(from_number, _wa_ayuda())
        else:
            # ¿Pide chollos de una categoría o una búsqueda?
            resultado = _wa_buscar_deals(text_raw)
            if resultado:
                nombre, emoji, deals = resultado
                _wa_responder(from_number, _wa_formatear_deals(nombre, emoji, deals))
            else:
                _wa_responder(from_number, _wa_ayuda())
        return {"ok": True}
    except Exception as e:
        print(f"❌ WA webhook error: {e}")
        return {"ok": True}  # siempre 200 para Meta


def _wa_responder(telefono: str, mensaje: str) -> None:
    """Envía un mensaje de respuesta vía WhatsApp Cloud API."""
    if not WA_PHONE_NUMBER_ID or not WA_TOKEN:
        return
    try:
        _http.post(
            f"https://graph.facebook.com/v20.0/{WA_PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": telefono,
                "type": "text",
                "text": {"body": mensaje},
            },
            timeout=10,
        )
    except Exception:
        pass


@app.get("/admin/wa-suscriptores")
def admin_wa_suscriptores(request: Request):
    """Lista de suscriptores de WhatsApp. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})
    with _get_db() as con:
        rows = con.execute(
            "SELECT telefono, nombre, activo, alta_en, baja_en FROM wa_suscriptores ORDER BY alta_en DESC"
        ).fetchall()
    suscriptores = [
        {"telefono": r[0], "nombre": r[1], "activo": bool(r[2]), "alta_en": r[3], "baja_en": r[4]}
        for r in rows
    ]
    activos = sum(1 for s in suscriptores if s["activo"])
    return {"suscriptores": suscriptores, "total": len(suscriptores), "activos": activos}
