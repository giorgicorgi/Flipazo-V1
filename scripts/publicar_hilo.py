#!/usr/bin/env python3
"""
publicar_hilo.py — Publica en Threads el hilo que toque hoy.

Lee la cola de `scripts/hilos_programados.json` (un hilo por fecha) y publica
el de HOY encadenado: cada post es respuesta al anterior vía `reply_to_id`.

Cron (dos franjas; cada hilo lleva la suya en el JSON):
    0 10 * * *  cd /home/flipazo/app && ./venv/bin/python scripts/publicar_hilo.py \
                >> /home/flipazo/app/hilos.log 2>&1
    0 18 * * *  cd /home/flipazo/app && ./venv/bin/python scripts/publicar_hilo.py \
                >> /home/flipazo/app/hilos.log 2>&1

La franja importa: 16-19h da mediana 18.026 views y la noche 20-23h solo 9.447.
La mañana queda en medio (15.490) y es donde van los que se recuperan de un día
perdido, nunca a las 20h o más tarde.

Decisiones que importan:

  · **No recupera días perdidos.** Si un día falla el cron, ese hilo se queda
    sin publicar y el log lo dice. Lo contrario —ponerse al día— sacaría dos
    hilos el mismo día, que es peor que saltarse uno: la cuenta vive de un
    hilo diario a la misma hora.
  · **Idempotente.** Al terminar escribe `publicado_en` y el permalink en el
    propio JSON. Si el cron se dispara dos veces, el segundo no hace nada.
    Threads NO permite borrar por API (los scopes actuales dan error 10), así
    que un duplicado habría que borrarlo a mano desde el móvil.
  · Entre crear el contenedor y publicarlo hay que esperar: la API devuelve el
    id antes de tener el post listo.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLA     = os.path.join(BASE_DIR, "scripts", "hilos_programados.json")
ENV      = os.path.join(BASE_DIR, ".env")

ESPERA_CREAR    = 2.5   # s entre crear el contenedor y publicarlo
ESPERA_ENTRE    = 4.0   # s entre un post y el siguiente


def log(m: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {m}", flush=True)


def credenciales() -> tuple[str, str]:
    env = {}
    with open(ENV, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    return env.get("THREADS_TOKEN", ""), env.get("THREADS_USER_ID", "")


def publicar_post(base: str, token: str, texto: str, prev: str | None) -> str | None:
    payload = {"media_type": "TEXT", "text": texto}
    if prev:
        payload["reply_to_id"] = prev
    r1 = requests.post(f"{base}/threads", params={"access_token": token},
                       json=payload, timeout=30)
    cid = r1.json().get("id") if r1.ok else None
    if not cid:
        log(f"   ❌ contenedor: {r1.text[:180]}")
        return None
    time.sleep(ESPERA_CREAR)
    r2 = requests.post(f"{base}/threads_publish", params={"access_token": token},
                       json={"creation_id": cid}, timeout=30)
    pid = r2.json().get("id") if r2.ok else None
    if not pid:
        log(f"   ❌ publicar: {r2.text[:180]}")
    return pid


def main() -> int:
    hoy = os.getenv("HILO_FECHA") or datetime.now().strftime("%Y-%m-%d")
    token, uid = credenciales()
    if not token or not uid:
        log("❌ faltan THREADS_TOKEN o THREADS_USER_ID en .env")
        return 1

    # La franja la decide el JSON, no el cron: así un mismo día puede llevar dos
    # hilos (p. ej. uno de recuperación a las 10 y el del día a las 18) sin que
    # el cron de una hora se lleve el hilo de la otra.
    ahora = os.getenv("HILO_HORA") or datetime.now().strftime("%H:%M")
    franja = "10:00" if ahora < "14:00" else "18:00"

    cola = json.load(open(COLA, encoding="utf-8"))
    hilo = next((h for h in cola
                 if h["fecha"] == hoy and h.get("hora", "18:00") == franja), None)
    if not hilo:
        log(f"no hay hilo programado para {hoy} en la franja de las {franja} — nada que hacer")
        return 0
    if hilo.get("publicado_en"):
        log(f"«{hilo['titulo']}» ya se publicó el {hilo['publicado_en']} — no se repite")
        return 0

    log(f"publicando «{hilo['titulo']}» ({len(hilo['posts'])} posts) · franja {franja}")
    base, prev, ids = f"https://graph.threads.net/v1.0/{uid}", None, []
    for i, texto in enumerate(hilo["posts"], 1):
        pid = publicar_post(base, token, texto, prev)
        if not pid:
            log(f"   ⚠️  cortado en el post {i}. Los {len(ids)} anteriores SÍ están publicados.")
            break
        ids.append(pid)
        log(f"   {i}/{len(hilo['posts'])} ok")
        prev = pid
        if i < len(hilo["posts"]):
            time.sleep(ESPERA_ENTRE)

    if not ids:
        return 1
    # Permalink del primero: es el que se comparte
    try:
        enlace = requests.get(f"https://graph.threads.net/v1.0/{ids[0]}",
                              params={"fields": "permalink", "access_token": token},
                              timeout=20).json().get("permalink", "")
    except Exception:
        enlace = ""
    hilo["publicado_en"] = datetime.now().isoformat(timespec="seconds")
    hilo["permalink"]    = enlace
    hilo["post_ids"]     = ids
    json.dump(cola, open(COLA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"✅ {len(ids)}/{len(hilo['posts'])} posts · {enlace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
