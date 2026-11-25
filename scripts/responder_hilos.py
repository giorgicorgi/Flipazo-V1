#!/usr/bin/env python3
"""
responder_hilos.py — Deja el enlace a la web al final de las historias que funcionan.

Cuando una historia de la cola (`scripts/hilos_programados.json`) pasa de
UMBRAL_VISTAS, la cuenta responde a su ÚLTIMO post con un comentario corto que
enlaza a flipazo.es/h/<historia>. Ese enlace llega a /desde-threads y cuenta los
clics por historia (`bio:h-<historia>` en `clicks`; `/go-stats/h-<historia>`).

Cron (cada hora a y media, solo de día; la publicación va a las 10:00 y 19:00):
    30 9-22 * * *  cd /home/flipazo/app && ./venv/bin/python scripts/responder_hilos.py \
                   >> /home/flipazo/app/hilos.log 2>&1

Por qué así:
  · 15,5 M de visualizaciones en 30 días trajeron 2 clics al enlace de la bio:
    la gente lee en el feed y no entra al perfil. En cambio, la historia de la
    bici (12-ago-2026), la única con enlace dentro, trajo 34 clics con 7.800
    visualizaciones. El enlace tiene que estar donde se termina de leer.
  · Pero un enlace dentro de la historia hunde su alcance. Por eso va como
    respuesta y con ESPERA_MIN_H de margen, cuando ya ha pasado el pico.
  · Una sola respuesta por historia, nunca más (Threads no deja borrar por API).
    Queda apuntada en el JSON (`enlace_id`), igual que `publicado_en`.
  · Una respuesta por pasada: si coinciden varias, salen en horas distintas.

RESPONDER_DRY=1 enseña lo que haría sin publicar nada.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from publicar_hilo import COLA, credenciales, log  # noqa: E402

UMBRAL_VISTAS = 300_000
ESPERA_MIN_H  = 20     # horas desde la publicación antes de enlazar
VENTANA_D     = 7      # pasado esto, la historia ya casi no tiene lectores nuevos
GRAPH         = "https://graph.threads.net/v1.0"

# Varias redacciones para que no parezca un bot pegando lo mismo. Todas cuelgan
# de la tesis de la cuenta: lo que vale algo de verdad vs. lo que parece.
TEXTOS = [
    "Nada vale lo que parece. Con los precios pasa lo mismo.\n\n"
    "Cada día miramos el precio real de miles de productos, no el del cartel. "
    "Los que hoy están en su mínimo de verdad, aquí:\n{url}",

    "Gracias por leer hasta el final.\n\n"
    "Por si os sirve: seguimos cada día lo que cuestan las cosas de verdad. "
    "Estos están hoy más baratos que nunca:\n{url}",

    "En esta cuenta contamos lo que valen las cosas de verdad. "
    "En la web hacemos lo mismo con los precios.\n\n"
    "Lo que hoy está en su precio más bajo real:\n{url}",

    "Lo que parece caro a veces no lo es, y al revés.\n\n"
    "Con las tiendas pasa igual. Los productos que hoy están de verdad en su mínimo:\n{url}",
]


def slug_de(titulo: str) -> str:
    s = unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:30].rstrip("-")


def vistas(post_id: str, token: str) -> int | None:
    try:
        r = requests.get(f"{GRAPH}/{post_id}/insights",
                         params={"metric": "views", "access_token": token}, timeout=20).json()
        return int(r["data"][0]["values"][0]["value"])
    except Exception:
        return None


def responder(token: str, uid: str, reply_to: str, texto: str, url: str) -> str | None:
    base = f"{GRAPH}/{uid}"
    r1 = requests.post(f"{base}/threads", params={"access_token": token}, timeout=30, json={
        "media_type": "TEXT", "text": texto, "reply_to_id": reply_to, "link_attachment": url})
    cid = r1.json().get("id") if r1.ok else None
    if not cid:
        log(f"   ❌ contenedor respuesta: {r1.text[:180]}")
        return None
    time.sleep(3)
    r2 = requests.post(f"{base}/threads_publish", params={"access_token": token},
                       json={"creation_id": cid}, timeout=30)
    pid = r2.json().get("id") if r2.ok else None
    if not pid:
        log(f"   ❌ publicar respuesta: {r2.text[:180]}")
    return pid


def main() -> int:
    dry = os.getenv("RESPONDER_DRY") == "1"
    token, uid = credenciales()
    if not token or not uid:
        log("❌ responder_hilos: faltan THREADS_TOKEN o THREADS_USER_ID")
        return 1
    ahora = datetime.now()
    cola = json.load(open(COLA, encoding="utf-8"))
    ya_enlazadas = sum(1 for h in cola if h.get("enlace_id"))

    for h in cola:
        if h.get("enlace_id") or not h.get("post_ids") or not h.get("publicado_en"):
            continue
        edad = ahora - datetime.fromisoformat(h["publicado_en"])
        if not (timedelta(hours=ESPERA_MIN_H) <= edad <= timedelta(days=VENTANA_D)):
            continue
        v = vistas(h["post_ids"][0], token)
        if v is None or v < UMBRAL_VISTAS:
            continue

        slug = slug_de(h["titulo"])
        url = f"https://www.flipazo.es/h/{slug}"
        texto = TEXTOS[ya_enlazadas % len(TEXTOS)].format(url=url)
        log(f"🔗 «{h['titulo']}» lleva {v:,} vistas → respuesta con {url}".replace(",", "."))
        if dry:
            log("   (RESPONDER_DRY=1: no se publica)\n" + texto)
            return 0
        pid = responder(token, uid, h["post_ids"][-1], texto, url)
        if not pid:
            return 1
        # Releer antes de escribir: publicar_hilo.py comparte este JSON.
        cola = json.load(open(COLA, encoding="utf-8"))
        for x in cola:
            if x.get("post_ids") == h["post_ids"]:
                x.update(enlace_id=pid, enlace_url=url, enlace_texto=texto,
                         enlace_publicado_en=datetime.now().isoformat(timespec="seconds"),
                         vistas_al_enlazar=v)
        json.dump(cola, open(COLA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log(f"   ✅ respuesta publicada ({pid}). Clics: /go-stats/h-{slug}")
        return 0  # una por pasada
    return 0


if __name__ == "__main__":
    sys.exit(main())
