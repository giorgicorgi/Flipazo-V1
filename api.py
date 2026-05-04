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
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests as _http
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

load_dotenv()

DB_PATH = "flipazo_deals.db"

# ── Admin & JWT ────────────────────────────────────────────────────────────────
ADMIN_USERNAME  = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD  = os.getenv("ADMIN_PASSWORD", "")   # contraseña en .env (no en git)
JWT_SECRET      = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ADMIN_HOURS = 12    # horas de validez del token admin
JWT_USER_HOURS  = 720   # 30 días para tokens de usuario

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

# ── Frontend (para redirects post-OAuth) ───────────────────────────────────────
FRONTEND_CUENTA = os.getenv("FRONTEND_CUENTA", "https://flipazo.es/cuenta")
API_URL         = os.getenv("API_URL",          "https://api.flipazo.es")

# ── Email (Gmail SMTP para verificación de cuentas) ────────────────────────────
EMAIL_ADDRESS      = os.getenv("EMAIL_ADDRESS",      "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")

# ── OAuth state store en memoria (anti-CSRF) ───────────────────────────────────
_oauth_states: dict[str, float] = {}

# ── Rate-limit para flags de expiración (IP:deal_id → expiry timestamp) ────────
_flag_rate_limit: dict[str, float] = {}
_FLAG_COOLDOWN = 3600  # 1 h por IP por deal — evita multivoto del mismo usuario


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
    allow_headers=["*"],
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


def _require_admin(request: Request) -> dict | None:
    """Extrae y valida el JWT admin del header Authorization. None si no autorizado."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = _jwt_decode(auth[7:])
    return payload if payload and payload.get("role") == "admin" else None


def _require_user(request: Request) -> dict | None:
    """Extrae y valida el JWT de usuario. None si no autenticado."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = _jwt_decode(auth[7:])
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
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD:
        print("⚠️  Email no configurado — verificación omitida")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Flipazo <{EMAIL_ADDRESS}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            srv.sendmail(EMAIL_ADDRESS, to, msg.as_string())
        return True
    except Exception as e:
        print(f"❌ Email error: {e}")
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


# ── Verificación ligera de precio expirado ─────────────────────────────────────

def _check_price_expired(url_afiliado: str) -> bool | None:
    """
    Comprueba si el producto ya no está disponible.
    Retorna True (expirado), False (activo), o None (no se pudo verificar → revisión manual).
    Links de Tradedoubler usan JS redirect — se construye URL directa de MediaMarkt.
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

        # Links de Tradedoubler (pdt.tradedoubler.com) usan meta-refresh JS —
        # extraemos el product ID y construimos URL directa de MediaMarkt
        td_m = _re.search(r'product\(\d+-(\d+)\)', url_afiliado)
        if td_m and "tradedoubler" in url_afiliado:
            product_id = td_m.group(1)
            url = f"https://www.mediamarkt.es/es/product/_{product_id}.html"

        resp = _http.get(url, headers=headers, timeout=12, allow_redirects=True)

        if resp.status_code in (404, 410):
            return True

        # Cloudflare o página de error genérica — no sabemos el estado real
        if resp.status_code in (403, 503) or "challenge" in resp.url:
            return None

        content = resp.text.lower()

        # Cloudflare challenge HTML
        if 'id="challenge-form"' in content or "just a moment" in content:
            return None

        signals = [
            "actualmente no disponible", "currently unavailable",
            "no está disponible", "este artículo no está disponible",
            "temporalmente sin existencias", "agotado", "out of stock",
            "producto no encontrado", "página no encontrada",
        ]
        return any(s in content for s in signals)

    except Exception:
        return None  # Error de red → revisión manual


def _background_check_expiry(deal_id: str, url_afiliado: str, titulo: str) -> None:
    """
    Hilo background: verifica si la oferta expiró y actualiza la BD.
    Notifica al admin por Telegram con el resultado.
    """
    try:
        resultado = _check_price_expired(url_afiliado)

        with _get_db() as con:
            if resultado is True:
                con.execute(
                    "UPDATE deals_publicados SET expirado = 1 WHERE deal_id = ?",
                    (deal_id,),
                )
                con.commit()
                print(f"🔴 Deal marcado expirado: {titulo[:50]}")

        token    = os.getenv("TELEGRAM_BOT_TOKEN", "")
        admin_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
        if token and admin_id:
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
                f"🚩 <b>Alerta de oferta expirada</b> (2 votos usuarios)\n\n"
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
        print(f"❌ Background expiry check error: {e}")


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

        # Blog posts
        con.execute("""
            CREATE TABLE IF NOT EXISTS blog_posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT    UNIQUE NOT NULL,
                titulo      TEXT    NOT NULL,
                resumen     TEXT    DEFAULT '',
                contenido   TEXT    DEFAULT '',
                imagen_url  TEXT    DEFAULT '',
                publicado   INTEGER DEFAULT 0,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_blog_slug ON blog_posts(slug)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_blog_pub  ON blog_posts(publicado, created_at)")
        con.commit()


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


class BlogPostBody(BaseModel):
    slug:       str
    titulo:     str
    resumen:    str = ""
    contenido:  str = ""
    imagen_url: str = ""
    publicado:  bool = False


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
    tipo:      Optional[str] = Query(default=None, description="OFERTA | ARBITRAJE"),
    tienda:    Optional[str] = Query(default=None),
    categoria: Optional[str] = Query(default=None),
):
    """Devuelve deals publicados ordenados del más reciente al más antiguo."""
    where_clauses, params = [], []
    if tipo:
        where_clauses.append("tipo = ?"); params.append(tipo.upper())
    if tienda:
        where_clauses.append("tienda = ?"); params.append(tienda)
    if categoria:
        where_clauses.append("categoria = ?"); params.append(categoria.lower())

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
            publicado_en    AS timestamp
        FROM deals_publicados
        {where_sql}
        ORDER BY publicado_en DESC
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]

    with _get_db() as con:
        rows = con.execute(sql, params).fetchall()

    deals = []
    for r in rows:
        d = dict(r)
        d["precio_actual"]   = d["precio_actual"]   or 0.0
        d["precio_original"] = d["precio_original"] or 0.0
        d["descuento_pct"]   = d["descuento_pct"]   or 0
        d["precio_wallapop"] = d["precio_wallapop"] or 0.0
        d["beneficio_neto"]  = d["beneficio_neto"]  or 0.0
        d["imagen_url"]      = d["imagen_url"]       or ""
        d["razonamiento"]    = d["razonamiento"]     or ""
        d["votes_up"]        = d["votes_up"]         or 0
        d["votes_down"]      = d["votes_down"]       or 0
        d["categoria"]        = d["categoria"]         or ""
        d["flags_expirado"]   = int(d.get("flags_expirado", 0) or 0)
        d["expirado"]         = bool(d.get("expirado", 0))
        try:    d["pros"]    = json.loads(d["pros"]    or "[]")
        except: d["pros"]    = []
        try:    d["contras"] = json.loads(d["contras"] or "[]")
        except: d["contras"] = []
        deals.append(d)

    return JSONResponse(content=deals)


@app.get("/api/deals/count")
def get_count():
    with _get_db() as con:
        total = con.execute("SELECT COUNT(*) FROM deals_publicados").fetchone()[0]
    return {"total": total}


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
    """Registra un voto (up/down). Anti-spam client-side via localStorage."""
    if body.direction not in ("up", "down"):
        return JSONResponse(status_code=400, content={"error": "direction must be 'up' or 'down'"})
    col = "votes_up" if body.direction == "up" else "votes_down"
    with _get_db() as con:
        updated = con.execute(
            f"UPDATE deals_publicados SET {col} = {col} + 1 WHERE deal_id = ?",
            (deal_id,),
        ).rowcount
        if updated == 0:
            return JSONResponse(status_code=404, content={"error": "deal not found"})
        con.commit()
        row = con.execute(
            "SELECT COALESCE(votes_up,0) AS votes_up, COALESCE(votes_down,0) AS votes_down "
            "FROM deals_publicados WHERE deal_id = ?",
            (deal_id,),
        ).fetchone()
    return {"votes_up": row["votes_up"], "votes_down": row["votes_down"]}


@app.post("/api/deals/{deal_id}/flag-expired")
def flag_expired(deal_id: str, request: Request):
    """
    Registra un voto de 'oferta expirada'.
    Anti-spam: mismo IP no puede flaggear el mismo deal más de una vez por hora.
    Con ≥2 flags activa una verificación de precio en background y notifica al admin.
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

    # Con ≥2 flags → verificar precio en hilo background
    if new_flags >= 2:
        threading.Thread(
            target=_background_check_expiry,
            args=(deal_id, row["url_afiliado"] or "", row["titulo"] or ""),
            daemon=True,
        ).start()

    return {"flags": new_flags, "expirado": False}


@app.get("/r/{deal_id}")
def redirect_afiliado(deal_id: str, request: Request, canal: str = "web"):
    """Redirect afiliado con tracking de click."""
    with _get_db() as con:
        row = con.execute(
            "SELECT url_afiliado FROM deals_publicados WHERE deal_id = ?",
            (deal_id,)
        ).fetchone()
        if not row or not row["url_afiliado"]:
            return JSONResponse(status_code=404, content={"error": "deal no encontrado"})
        con.execute(
            "INSERT INTO clicks (deal_id, canal, ip, ts) VALUES (?, ?, ?, ?)",
            (deal_id, canal,
             request.client.host if request.client else "unknown",
             datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    return RedirectResponse(url=row["url_afiliado"], status_code=302)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/admin/login")
def admin_login(body: AdminLoginBody):
    """Autentica al administrador. Devuelve JWT con role=admin."""
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
    return {"token": token, "expires_in": JWT_ADMIN_HOURS * 3600}


@app.get("/admin/deals")
def admin_deals(
    request:  Request,
    limit:    int = Query(default=50, ge=1, le=200),
    offset:   int = Query(default=0,  ge=0),
    tipo:     Optional[str] = Query(default=None),
    tienda:   Optional[str] = Query(default=None),
    busqueda: Optional[str] = Query(default=None),
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

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    sql = f"""
        SELECT
            d.rowid, d.deal_id, d.titulo, d.tienda, d.tipo,
            d.precio          AS precio_actual,
            d.precio_original, d.descuento_pct,
            d.imagen_url,      d.publicado_en,
            d.url_afiliado,
            COALESCE(d.votes_up,   0) AS votes_up,
            COALESCE(d.votes_down, 0) AS votes_down,
            COALESCE(d.categoria,  '') AS categoria,
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
    with _get_db() as con:
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
    with _get_db() as con:
        deleted = con.execute(
            "DELETE FROM deals_publicados WHERE deal_id = ?", (deal_id,)
        ).rowcount
        con.commit()
    if deleted == 0:
        return JSONResponse(status_code=404, content={"error": "Deal no encontrado"})
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


@app.get("/admin/stats")
def admin_stats(request: Request):
    """Estadísticas globales: totales, clicks, top deals. Requiere JWT admin."""
    if not _require_admin(request):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    with _get_db() as con:
        total_deals   = con.execute("SELECT COUNT(*) FROM deals_publicados").fetchone()[0]
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
        "total_deals":   total_deals,
        "today_deals":   today_deals,
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

    return RedirectResponse(
        f"{FRONTEND_CUENTA}?token={urllib.parse.quote(token, safe='')}",
        status_code=302,
    )


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

    return RedirectResponse(
        f"{FRONTEND_CUENTA}?token={urllib.parse.quote(token, safe='')}",
        status_code=302,
    )


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
    return {"token": token}


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
    return RedirectResponse(
        f"{FRONTEND_CUENTA}?token={urllib.parse.quote(jwt, safe='')}",
        status_code=302,
    )


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
            "SELECT id, slug, titulo, resumen, imagen_url, created_at, updated_at "
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
            "SELECT id, slug, titulo, resumen, contenido, imagen_url, publicado, created_at, updated_at "
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
                "INSERT INTO blog_posts (slug, titulo, resumen, contenido, imagen_url, publicado, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (body.slug, body.titulo, body.resumen, body.contenido,
                 body.imagen_url, 1 if body.publicado else 0, now, now)
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
            "imagen_url=?, publicado=?, updated_at=? WHERE id=?",
            (body.slug, body.titulo, body.resumen, body.contenido,
             body.imagen_url, 1 if body.publicado else 0, now, post_id)
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
