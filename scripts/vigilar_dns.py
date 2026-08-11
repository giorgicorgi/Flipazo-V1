#!/usr/bin/env python3
"""
vigilar_dns.py — Comprueba que el DNS de flipazo.es sigue en pie y avisa si no.

Un DNS mal editado es más caro que un dominio robado: si desaparece un MX, un
CNAME o el DKIM, se cae el correo o la web y te enteras por un usuario. Esto lo
caza en el siguiente ciclo.

Lo comprueba TODO contra CADA servidor autoritativo por separado, no contra un
resolutor cualquiera. Es la lección del 11-ago-2026: había dos registros DMARC
(el de GoDaddy y uno que añadió Brevo), ns61 servía uno y ns62 servía dos, y
consultando "el DNS" en general salía bien la mitad de las veces. Dos DMARC =
permerror = Gmail rechaza la autenticación de todo el correo del dominio.

Cron sugerido, una vez al día:
    15 7 * * *  /home/flipazo/app/venv/bin/python /home/flipazo/app/scripts/vigilar_dns.py \
                >> /home/flipazo/app/dns.log 2>&1

    --dry-run   comprueba y escribe el resultado, sin avisar por Telegram

Antispam: guarda la huella de los problemas en `.dns_watch.json`. Solo vuelve a
avisar si el conjunto de problemas CAMBIA, o si sigue roto pasados RE_AVISO_DIAS.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN    = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
ESTADO_PATH       = os.path.join(BASE_DIR, ".dns_watch.json")
RE_AVISO_DIAS     = 3
DRY_RUN           = "--dry-run" in sys.argv

DOMINIO = "flipazo.es"
# Servidores autoritativos. Si esto cambia, es que alguien movió el dominio de
# proveedor: se avisa igualmente porque el chequeo de NS lo detecta.
AUTORITATIVOS = ["ns61.domaincontrol.com", "ns62.domaincontrol.com"]


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {m}", flush=True)


def dig(nombre: str, tipo: str, servidor: str | None = None) -> list[str]:
    """Devuelve las respuestas como lista de cadenas. Lista vacía si no hay o falla."""
    cmd = ["dig", "+short", "+time=5", "+tries=2", tipo, nombre]
    if servidor:
        cmd.insert(1, f"@{servidor}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=25).stdout
    except Exception as e:
        log(f"  ⚠️  dig {tipo} {nombre} falló: {e}")
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def _txt(lineas: list[str]) -> list[str]:
    """Une los trozos entrecomillados de cada TXT (los >255 bytes vienen partidos)."""
    fuera = []
    for l in lineas:
        if '"' not in l:
            continue                       # es un CNAME intermedio, no el TXT
        fuera.append("".join(re.findall(r'"([^"]*)"', l)))
    return fuera


# ── Comprobaciones ────────────────────────────────────────────────────────────
# Cada una devuelve (ok, descripción). El texto se usa tal cual en el aviso.

def check_ns() -> tuple[bool, str]:
    ns = sorted(x.rstrip(".").lower() for x in dig(DOMINIO, "NS"))
    esperados = sorted(AUTORITATIVOS)
    if ns == esperados:
        return True, f"NS: {', '.join(ns)}"
    return False, f"NS CAMBIADOS: {', '.join(ns) or '(ninguno)'} — se esperaban {', '.join(esperados)}"


def check_web() -> tuple[bool, str]:
    a = dig(DOMINIO, "A")
    www = dig("www." + DOMINIO, "CNAME") + dig("www." + DOMINIO, "A")
    if not a:
        return False, "La web NO resuelve: sin registro A en la raíz"
    if not www:
        return False, "www.flipazo.es no resuelve (ni CNAME ni A)"
    return True, f"Web: A={a[0]} · www→{www[0].rstrip('.')}"


def check_mx() -> tuple[bool, str]:
    mx = sorted(x.split()[-1].rstrip(".").lower() for x in dig(DOMINIO, "MX") if x.split())
    if not mx:
        return False, "SIN registros MX: el correo entrante está caído"
    if not any("zoho" in m for m in mx):
        return False, f"MX inesperados (no son de Zoho): {', '.join(mx)}"
    return True, f"MX: {', '.join(mx)}"


def check_spf(por_servidor: dict) -> tuple[bool, str]:
    problemas = []
    for srv, txts in por_servidor.items():
        spf = [t for t in txts if t.lower().startswith("v=spf1")]
        if len(spf) != 1:
            problemas.append(f"{srv}: {len(spf)}")
    if problemas:
        return False, "SPF mal: debe haber exactamente 1 registro — " + ", ".join(problemas)
    ejemplo = next(t for t in next(iter(por_servidor.values())) if t.lower().startswith("v=spf1"))
    return True, f"SPF: 1 registro ({ejemplo[:58]}…)"


def check_dmarc() -> tuple[bool, str]:
    """El que nos mordió. Se consulta servidor por servidor: con dos registros
    repartidos entre ellos, preguntar 'al DNS' acierta la mitad de las veces."""
    cuentas = {}
    for srv in AUTORITATIVOS:
        txts = _txt(dig("_dmarc." + DOMINIO, "TXT", srv))
        cuentas[srv] = [t for t in txts if t.lower().startswith("v=dmarc1")]
    total = {srv: len(v) for srv, v in cuentas.items()}
    if any(n == 0 for n in total.values()):
        return False, f"DMARC AUSENTE en algún servidor: {total}"
    if any(n > 1 for n in total.values()):
        detalle = "; ".join(f"{s}: {n}" for s, n in total.items())
        return False, (f"DMARC DUPLICADO ({detalle}). Dos registros = permerror: "
                       f"Gmail rechaza la autenticación de TODO el correo del dominio")
    pol = re.search(r"p=(\w+)", cuentas[AUTORITATIVOS[0]][0])
    return True, f"DMARC: 1 registro · p={pol.group(1) if pol else '?'}"


def check_dkim() -> tuple[bool, str]:
    faltan = []
    for sel in ("brevo1", "brevo2"):
        txts = _txt(dig(f"{sel}._domainkey.{DOMINIO}", "TXT"))
        if not any("k=rsa" in t and "p=" in t for t in txts):
            faltan.append(sel)
    if faltan:
        return False, (f"DKIM ROTO en {', '.join(faltan)}._domainkey: no se llega a la clave. "
                       f"El correo dejará de estar firmado y caerá en spam")
    return True, "DKIM: brevo1 y brevo2 llegan a su clave RSA"


def check_brevo_code(txts_raiz: list[str]) -> tuple[bool, str]:
    if any(t.startswith("brevo-code:") for t in txts_raiz):
        return True, "brevo-code: presente"
    return False, "Falta el TXT brevo-code: Brevo puede desautenticar el dominio"


def check_http() -> tuple[bool, str]:
    fallos = []
    for url, esperado in (("https://flipazo.es", (200, 301, 302, 307, 308)),
                          ("https://api.flipazo.es/api/deals/count", (200,))):
        try:
            r = requests.get(url, timeout=15, allow_redirects=False)
            if r.status_code not in esperado:
                fallos.append(f"{url} → HTTP {r.status_code}")
        except Exception as e:
            fallos.append(f"{url} → {type(e).__name__}")
    if fallos:
        return False, "No responden: " + " · ".join(fallos)
    return True, "Web y API responden"


def main() -> int:
    log(f"Vigilando el DNS de {DOMINIO}…")

    # TXT de la raíz, por servidor: SPF y brevo-code salen de aquí
    txt_por_servidor = {srv: _txt(dig(DOMINIO, "TXT", srv)) for srv in AUTORITATIVOS}
    txt_raiz = txt_por_servidor.get(AUTORITATIVOS[0], [])

    resultados = [
        ("NS",         check_ns()),
        ("Web",        check_web()),
        ("MX",         check_mx()),
        ("SPF",        check_spf(txt_por_servidor)),
        ("DMARC",      check_dmarc()),
        ("DKIM",       check_dkim()),
        ("brevo-code", check_brevo_code(txt_raiz)),
        ("HTTP",       check_http()),
    ]

    problemas = []
    for nombre, (ok, desc) in resultados:
        log(f"  {'✅' if ok else '❌'} {nombre:<11} {desc}")
        if not ok:
            problemas.append(f"{nombre}: {desc}")

    # ── Antispam: solo avisar si cambia el problema, o si sigue roto tras N días ──
    try:
        estado = json.load(open(ESTADO_PATH))
    except Exception:
        estado = {}
    huella = "|".join(sorted(problemas))
    ahora  = datetime.now(timezone.utc)

    if not problemas:
        if estado.get("huella"):
            log("  ↩️  resuelto: el DNS vuelve a estar correcto")
            _avisar("DNS restablecido", "Todo vuelve a estar correcto.")
        _guardar({"huella": "", "ultimo_aviso": ""})
        log("DNS correcto.")
        return 0

    repetir = True
    if estado.get("huella") == huella and estado.get("ultimo_aviso"):
        try:
            dias = (ahora - datetime.fromisoformat(estado["ultimo_aviso"])).days
            repetir = dias >= RE_AVISO_DIAS
        except ValueError:
            repetir = True

    if repetir:
        _avisar(f"DNS: {len(problemas)} problema(s)", "\n\n".join(problemas))
        _guardar({"huella": huella, "ultimo_aviso": ahora.isoformat()})
    else:
        log("  (mismo problema ya avisado — no se repite el aviso)")
        _guardar({"huella": huella, "ultimo_aviso": estado.get("ultimo_aviso", "")})
    return 1


def _guardar(d: dict) -> None:
    if DRY_RUN:
        return
    try:
        json.dump(d, open(ESTADO_PATH, "w"))
    except Exception as e:
        log(f"  ⚠️  no se pudo guardar el estado: {e}")


def _avisar(titulo: str, detalle: str) -> None:
    if DRY_RUN:
        log(f"  [dry-run] avisaría: {titulo}\n{detalle}")
        return
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN:
        log("  ⚠️  Telegram del admin sin configurar — solo queda en el log")
        return
    ts = datetime.now().strftime("%d/%m %H:%M")
    texto = f"🚨 <b>Flipazo — {titulo}</b>\n<i>{ts}</i>\n\n<code>{detalle[:1500]}</code>"
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_ADMIN, "text": texto,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=15)
    except Exception as e:
        log(f"  ⚠️  no se pudo avisar por Telegram: {e}")


if __name__ == "__main__":
    sys.exit(main())
