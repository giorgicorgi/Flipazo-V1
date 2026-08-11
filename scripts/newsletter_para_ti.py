#!/usr/bin/env python3
"""
newsletter_para_ti.py — Boletín "Para ti" por correo.

Cada usuario elige QUÉ quiere ver (categorías, tiendas, precio — tabla `user_prefs`)
y CADA CUÁNTO quiere recibirlo:

    diario    → todos los días
    semanal   → un solo día a la semana (el primero de `email_dias`)
    alternos  → día sí, día no (48 h desde el último envío)
    dias      → los días concretos que haya marcado (L M X J V S D)

Se ejecuta por cron UNA VEZ AL DÍA; el script decide a quién le toca hoy:
    0 8 * * *  /home/flipazo/app/venv/bin/python /home/flipazo/app/scripts/newsletter_para_ti.py \
               >> /home/flipazo/app/newsletter.log 2>&1

Idempotente: `email_last_sent` impide un segundo envío el mismo día, así que
relanzarlo no duplica correos.

    --dry-run   no envía, escribe en pantalla a quién le tocaría y con qué deals
    --force     ignora el calendario (útil para probar un envío real)

Proveedor de envío: SMTP. Por defecto usa el mismo Gmail que la verificación de
cuentas, pero un boletín NO debe salir por ahí (Gmail limita a ~500/día, no
gestiona bajas ni rebotes y acaba en spam). Con SMTP_HOST/SMTP_USER/SMTP_PASS
apuntando a un proveedor de boletines (Brevo: smtp-relay.brevo.com:587) el
código no cambia — solo el .env.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_PATH   = os.getenv("DB_PATH", os.path.join(BASE_DIR, "flipazo_deals.db"))
SITE      = "https://flipazo.es"
API       = os.getenv("API_BASE", "https://api.flipazo.es")
JWT_SECRET = os.getenv("JWT_SECRET", "")

# SMTP: proveedor de boletín si está configurado; si no, el Gmail de la verificación.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))   # el 465 lo bloquea Hetzner
SMTP_USER = os.getenv("SMTP_USER") or os.getenv("EMAIL_ADDRESS", "")
SMTP_PASS = os.getenv("SMTP_PASS") or os.getenv("EMAIL_APP_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", "Flipazo <hola@flipazo.es>")

MAX_DEALS     = 8     # un boletín, no un catálogo
VENTANA_DIAS  = 8     # tope de antigüedad al reunir deals (cubre el caso semanal)
DRY_RUN = "--dry-run" in sys.argv
FORCE   = "--force"   in sys.argv

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _json(txt, fallback):
    try:
        v = json.loads(txt or "")
        return v if isinstance(v, type(fallback)) else fallback
    except Exception:
        return fallback


# ── ¿A quién le toca hoy? ─────────────────────────────────────────────────────

def toca_hoy(freq: str, dias: list, last_sent: str, hoy) -> bool:
    """`hoy` es un date. `last_sent` un ISO-8601 o cadena vacía."""
    ultimo = None
    if last_sent:
        try:
            ultimo = datetime.fromisoformat(last_sent.replace("Z", "+00:00")).date()
        except ValueError:
            ultimo = None
    if ultimo == hoy:
        return False                     # ya se le envió hoy
    if freq == "diario":
        return True
    if freq == "alternos":
        # Día sí, día no. Sin envío previo, empieza hoy.
        return ultimo is None or (hoy - ultimo).days >= 2
    if freq == "semanal":
        return hoy.weekday() == (dias[0] if dias else 0)
    if freq == "dias":
        return hoy.weekday() in dias
    return False


def encaja(deal: sqlite3.Row, cats: list, stores: list, pmin: float, pmax) -> bool:
    """Filtro por categoría, tienda y precio.

    Las SUBcategorías (TV, Audio, Running…) son un refinamiento de la web: sus
    regex viven en index.html y duplicarlas aquí se desincronizaría. El boletín
    se queda en el nivel de categoría — más ancho, nunca más estrecho de lo pedido."""
    if cats and (deal["categoria"] or "") not in cats:
        return False
    if stores and (deal["tienda"] or "") not in stores:
        return False
    p = deal["precio"] or 0
    if pmin and p < pmin:
        return False
    if pmax is not None and p > pmax:
        return False
    return True


def fmt_precio(v) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def token_baja(user_id: str) -> str:
    """Mismo esquema que api.firmar_baja: HMAC del user_id con JWT_SECRET."""
    firma = hmac.new(JWT_SECRET.encode(), f"baja:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{firma}"


# ── Correo ────────────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def construir_texto(nombre: str, deals: list, url_baja: str, cadencia: str,
                    primer_envio: bool = False) -> str:
    """Versión en texto plano. No es un adorno: un multipart/alternative que solo
    lleva HTML es una señal clásica de correo masivo, y los filtros lo puntúan
    peor. Además es lo que se lee en relojes, lectores de pantalla y clientes
    que bloquean el HTML."""
    lineas = [f"Hola{', ' + nombre if nombre else ''}: esto es lo que ha salido de lo que te interesa.", ""]
    for d in deals:
        pct  = d["descuento_pct"] or 0
        prec = fmt_precio(d["precio"] or 0)
        orig = f" (antes {fmt_precio(d['precio_original'])})" if (d["precio_original"] or 0) > (d["precio"] or 0) else ""
        lineas += [
            f"* {(d['titulo'] or '')[:80]}",
            f"  {prec}{orig} -{pct}% en {d['tienda']}",
            f"  {SITE}/r/{d['deal_id']}?canal=email_parati",
            "",
        ]
    if primer_envio:
        lineas += ["Un favor para no perderte nada: arrastra este correo a la pestana Principal",
                   "y anade hola@flipazo.es a tus contactos.", ""]
    lineas += [
        "---",
        f"Recibes este correo {cadencia} porque lo pediste en tu seccion 'Para ti'.",
        f"Cambiar que recibo o cada cuanto: {SITE}/?prefs=1",
        f"Darme de baja: {url_baja}",
    ]
    return "\n".join(lineas)


def construir_html(nombre: str, deals: list, url_baja: str, cadencia: str,
                   primer_envio: bool = False) -> str:
    """HTML de correo con el mismo lenguaje visual que flipazo.es: rojo de marca
    #F52834, precio en azul #0581FC, ahorro en verde agua #14C1AE, papel #FEFEF8,
    títulos en Playfair Display y el resto en Nunito.

    Todo va en tablas y con estilos EN LÍNEA: Gmail descarta <style> y no entiende
    flexbox ni grid. Las webfonts solo cargan en Apple Mail y similares, así que
    cada font-family lleva su alternativa del sistema (Georgia para el serif)."""
    filas = []
    for d in deals:
        pct   = d["descuento_pct"] or 0
        precio = d["precio"] or 0
        p_ori  = d["precio_original"] or 0
        prec  = fmt_precio(precio)
        # Precio anterior tachado en rojo, como en la web (efecto anclaje)
        orig  = (f'<span style="color:#6B7280;font-size:15px;text-decoration:line-through;'
                 f'text-decoration-color:#F52834">{fmt_precio(p_ori)}</span>&nbsp;'
                 ) if p_ori > precio else ""
        ahorro = p_ori - precio
        pill = (f'<div style="padding-top:8px"><span style="background:#E6F9F7;color:#0D7A6F;'
                f'font-size:12px;font-weight:800;padding:4px 10px;border-radius:9999px;'
                f'font-family:Nunito,-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
                f'Ahorras {fmt_precio(ahorro)}</span></div>') if ahorro > 0 else ""
        url   = f'{SITE}/r/{d["deal_id"]}?canal=email_parati'
        img   = d["imagen_url"] or ""
        imgtd = (f'<td width="104" style="padding:0 16px 0 0" valign="top">'
                 f'<a href="{url}"><img src="{_esc(img)}" width="104" alt="" '
                 f'style="width:104px;height:104px;object-fit:contain;border-radius:12px;'
                 f'background:#FFFFFF;border:1px solid #E5E7EB;display:block"></a></td>') if img else ""
        filas.append(f"""
        <tr><td style="padding:0 0 8px">
          <table width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:#FFFFFF;border:1px solid #E5E7EB;border-radius:14px;padding:16px">
            <tr>
            {imgtd}
            <td valign="top">
              <div style="font-family:Nunito,-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
                          font-size:10px;font-weight:800;color:#6B7280;text-transform:uppercase;
                          letter-spacing:.08em;padding-bottom:5px">{_esc(d["tienda"])}</div>
              <a href="{url}" style="font-family:\'Playfair Display\',Georgia,serif;font-size:17px;
                 font-weight:700;color:#111827;text-decoration:none;line-height:1.25">{_esc((d["titulo"] or "")[:88])}</a>
              <div style="padding-top:9px">
                {orig}<span style="font-family:Nunito,-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
                      font-size:23px;font-weight:800;color:#0581FC">{prec}</span>
                <span style="background:#F52834;color:#FFFFFF;font-size:12px;font-weight:800;
                      padding:3px 8px;border-radius:6px;margin-left:8px;
                      font-family:Nunito,-apple-system,Segoe UI,Helvetica,Arial,sans-serif">&minus;{pct}%</span>
              </div>
              {pill}
              <a href="{url}" style="display:inline-block;margin-top:12px;background:#F52834;
                 color:#FFFFFF;font-size:13px;font-weight:800;text-decoration:none;
                 padding:10px 22px;border-radius:9999px;
                 font-family:Nunito,-apple-system,Segoe UI,Helvetica,Arial,sans-serif">Ver oferta</a>
            </td>
          </tr></table>
        </td></tr>""")

    # Solo en el primer correo: pedir que lo muevan a Principal. Es lo único que
    # de verdad mueve la clasificación de Gmail, y repetirlo cada vez cansaría.
    bienvenida = (f"""
    <tr><td style="padding:0 0 18px">
      <table width="100%" cellpadding="0" cellspacing="0" border="0"
             style="background:#E6F9F7;border-radius:12px;padding:14px 16px">
        <tr><td style="font-family:Nunito,-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
                       font-size:13px;color:#0D7A6F;line-height:1.55">
          <b>Un favor para no perderte nada:</b> arrastra este correo a la pestaña
          <b>Principal</b> y añade <b>hola@flipazo.es</b> a tus contactos. Así los
          siguientes te llegan ahí y no entre la publicidad.
        </td></tr>
      </table>
    </td></tr>""") if primer_envio else ""

    saludo = f"Hola{', ' + _esc(nombre) if nombre else ''}"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#F3F4F6;-webkit-font-smoothing:antialiased">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">
  {len(deals)} ofertas con descuento verificado de lo que te interesa.
</div>
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F3F4F6;padding:24px 12px">
<tr><td align="center">
  <table width="100%" cellpadding="0" cellspacing="0" border="0"
         style="max-width:580px;background:#FEFEF8;border-radius:16px;padding:26px 22px;
                font-family:Nunito,-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">
    <tr><td style="padding-bottom:16px;border-bottom:1px solid #E5E7EB">
      <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        <td valign="middle">
          <a href="{SITE}"><img src="https://www.flipazo.es/flipazo-logo.png" height="34" alt="Flipazo"
             style="height:34px;display:block;border:0"></a>
        </td>
        <td valign="middle" align="right" style="font-size:11px;font-weight:800;color:#F52834;
            text-transform:uppercase;letter-spacing:.1em">Para ti</td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:20px 0 18px;font-size:16px;color:#374151;line-height:1.5">
      {saludo}: esto es lo que ha salido de lo que te interesa.
    </td></tr>
    {bienvenida}
    {''.join(filas)}
    <tr><td align="center" style="padding:14px 0 4px">
      <a href="{SITE}" style="display:inline-block;border:1.5px solid #2C3E50;color:#2C3E50;
         font-size:13px;font-weight:800;text-decoration:none;padding:11px 26px;border-radius:9999px">
         Ver todas las ofertas</a>
    </td></tr>
    <tr><td style="padding-top:18px;border-top:1px solid #E5E7EB">
      <div style="font-size:12px;color:#9CA3AF;line-height:1.7;padding-top:14px">
        Recibes este correo {cadencia} porque lo pediste en tu sección «Para ti».<br>
        <a href="{SITE}/?prefs=1" style="color:#6B7280">Cambiar qué recibo o cada cuánto</a>
        &nbsp;·&nbsp;
        <a href="{url_baja}" style="color:#6B7280">Darme de baja</a>
      </div>
    </td></tr>
  </table>
  <div style="font-size:11px;color:#9CA3AF;padding:14px 0 0;
       font-family:Nunito,-apple-system,Segoe UI,Helvetica,Arial,sans-serif">
    Flipazo &middot; ofertas con descuento real verificado
  </div>
</td></tr></table></body></html>"""


def enviar(to: str, asunto: str, texto: str, html: str, url_baja: str) -> bool:
    if DRY_RUN:
        log(f"  [dry-run] → {to} · {asunto}")
        return True
    if not SMTP_USER or not SMTP_PASS:
        log("  ⚠️  SMTP sin configurar (SMTP_USER/SMTP_PASS o EMAIL_ADDRESS/EMAIL_APP_PASSWORD)")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = asunto
        msg["From"]    = MAIL_FROM
        msg["To"]      = to
        # Deja que el cliente de correo ofrezca la baja en su propia interfaz:
        # sin esto, mucha gente usa el botón de spam en su lugar.
        msg["List-Unsubscribe"] = f"<{url_baja}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        # Orden obligatorio en multipart/alternative: de peor a mejor. El cliente
        # se queda con la ÚLTIMA que sepa mostrar, así que el HTML va al final.
        msg.attach(MIMEText(texto, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as srv:
                srv.login(SMTP_USER, SMTP_PASS)
                srv.sendmail(SMTP_USER, to, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as srv:
                srv.starttls()
                srv.login(SMTP_USER, SMTP_PASS)
                srv.sendmail(SMTP_USER, to, msg.as_string())
        return True
    except Exception as e:
        log(f"  ⚠️  Error enviando a {to}: {e}")
        return False


def describir(freq: str, dias: list) -> str:
    if freq == "diario":
        return "todos los días"
    if freq == "alternos":
        return "un día sí y otro no"
    if freq == "semanal":
        return f"cada {DIAS_ES[dias[0] if dias else 0]}"
    if freq == "dias" and dias:
        # lunes…viernes no cambian en plural; sábado y domingo sí
        plural = [DIAS_ES[d] + ("s" if d >= 5 else "") for d in dias]
        return "los " + ", ".join(plural)
    return "periódicamente"


def main() -> int:
    if not os.path.exists(DB_PATH):
        log(f"❌ No existe la base de datos: {DB_PATH}")
        return 1
    if not JWT_SECRET:
        log("❌ JWT_SECRET no configurado: los enlaces de baja no serían válidos. Aborto.")
        return 1

    ahora = datetime.now(timezone.utc)
    hoy   = ahora.date()
    con   = db()

    usuarios = con.execute("""
        SELECT p.*, COALESCE(u.name,'') AS nombre, COALESCE(u.email,'') AS email
        FROM user_prefs p JOIN users u ON u.id = p.user_id
        WHERE COALESCE(p.email_enabled,0) = 1 AND COALESCE(u.email,'') != ''
    """).fetchall()

    if not usuarios:
        log("Nadie tiene el boletín activado — nada que enviar.")
        return 0

    # Un solo lote de candidatos para todos: la ventana cubre hasta el caso semanal
    desde = (ahora - timedelta(days=VENTANA_DIAS)).isoformat()
    candidatos = con.execute("""
        SELECT deal_id, titulo, tienda, precio, precio_original, descuento_pct,
               COALESCE(categoria,'') AS categoria, COALESCE(imagen_url,'') AS imagen_url,
               publicado_en
        FROM deals_publicados
        WHERE COALESCE(expirado,0) = 0 AND publicado_en > ?
        ORDER BY descuento_pct DESC, publicado_en DESC
        LIMIT 800
    """, (desde,)).fetchall()
    log(f"{len(usuarios)} usuario(s) con boletín · {len(candidatos)} deals candidatos")

    enviados = tocaban = 0
    for u in usuarios:
        freq = (u["email_freq"] or "semanal")
        dias = [int(d) for d in _json(u["email_dias"], []) if str(d).isdigit()]
        if not (FORCE or toca_hoy(freq, dias, u["email_last_sent"] or "", hoy)):
            continue
        tocaban += 1

        # Desde el último envío (no repetir), con el tope de la ventana
        ultimo = u["email_last_sent"] or ""
        corte  = max(ultimo, desde) if ultimo else desde
        propios = [d for d in candidatos if (d["publicado_en"] or "") > corte]

        match = [d for d in propios
                 if encaja(d, _json(u["cats"], []), _json(u["stores"], []),
                           u["precio_min"] or 0, u["precio_max"])][:MAX_DEALS]
        if not match:
            log(f"  · {u['email']} sin novedades que encajen — no se envía correo vacío")
            continue

        url_baja = f"{API}/api/newsletter/baja?t={token_baja(u['user_id'])}"
        asunto   = (f"{len(match)} ofertas para ti" if len(match) > 1
                    else f"Una oferta para ti: {(match[0]['titulo'] or '')[:48]}")
        _cad     = describir(freq, dias)
        # Primer correo de este suscriptor: lleva la petición de moverlo a
        # Principal. Solo una vez — repetirlo en cada envío cansaría.
        primero  = not (u["email_last_sent"] or "")
        texto    = construir_texto(u["nombre"], match, url_baja, _cad, primero)
        html     = construir_html(u["nombre"], match, url_baja, _cad, primero)

        if enviar(u["email"], asunto, texto, html, url_baja):
            enviados += 1
            if not DRY_RUN:
                con.execute("UPDATE user_prefs SET email_last_sent = ? WHERE user_id = ?",
                            (ahora.isoformat(), u["user_id"]))
                con.commit()
            log(f"  ✅ {u['email']} → {len(match)} deals ({describir(freq, dias)})")
        time.sleep(0.5)   # no golpear el SMTP en ráfaga

    log(f"Boletín: {enviados}/{tocaban} enviados (de {len(usuarios)} suscriptores)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
