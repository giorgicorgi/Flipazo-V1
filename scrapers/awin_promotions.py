"""
scrapers/awin_promotions.py — Promociones/cupones activos de AWIN (Promotions API).

A diferencia del product feed (deals de producto con precio), esto son PROMOS de tienda:
"3x2 en Maquillaje", "Hasta -75%", cupones con código, etc. Sirve para tener presencia
inmediata de tiendas cuyo feed no da descuentos a nivel de producto (ECI, Zalando…).

Auth: `AWIN_API_TOKEN` en `.env` (token OAuth2 de la cuenta, secreto). Publisher id =
`AWIN_PUBLISHER_ID` (3254573 user / 2935183 account).

Endpoint (OJO: "publisher" en singular):
    POST https://api.awin.com/publisher/{publisherId}/promotions/
    body: {"filters": {"membership": "joined", "regionCodes": ["ES"]}, "pagination": {...}}
"""

import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

AWIN_API_TOKEN    = os.getenv("AWIN_API_TOKEN", "")
AWIN_PUBLISHER_ID = os.getenv("AWIN_PUBLISHER_ID", "2935183")

# Nombre del advertiser en AWIN → nombre limpio para mostrar
_TIENDA_LIMPIA = {
    "El Corte Ingles ES":  "El Corte Inglés",
    "Privé by Zalando ES": "Privé by Zalando",
    "Padel Market":        "Padel Market",
    "SharkNinja ES":       "SharkNinja",
    "Deporte Outlet ES":   "Deporte Outlet",
    "BRICO DEPÔT_ES":      "Brico Depôt",
}
_MAX_POR_TIENDA = int(os.getenv("AWIN_PROMO_MAX_POR_TIENDA", "6"))


def _limpiar_tienda(nombre: str) -> str:
    return _TIENDA_LIMPIA.get(nombre, (nombre or "").replace(" ES", "").strip())


def fetch_awin_promociones() -> list[dict]:
    """Devuelve las promos activas (joined, ES), curadas: sin expiradas, sin títulos
    duplicados por tienda y con tope por tienda. list[dict] listo para guardar/mostrar."""
    if not AWIN_API_TOKEN:
        print("   ⚠️ AWIN_API_TOKEN no configurado — skip promociones")
        return []

    url = f"https://api.awin.com/publisher/{AWIN_PUBLISHER_ID}/promotions/"
    headers = {
        "Authorization": f"Bearer {AWIN_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    crudas: list[dict] = []
    try:
        page = 1
        while page <= 15:
            r = requests.post(
                url, headers=headers,
                json={"filters": {"membership": "joined", "regionCodes": ["ES"]},
                      "pagination": {"page": page, "pageSize": 100}},
                timeout=40,
            )
            if r.status_code != 200:
                print(f"   ⚠️ AWIN promotions HTTP {r.status_code}: {r.text[:120]}")
                break
            chunk = r.json().get("data") or []
            crudas.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
    except Exception as e:
        print(f"   ❌ AWIN promotions error: {e}")
        return []

    ahora = datetime.now(timezone.utc)

    # Los que traen CÓDIGO van primero. El tope por tienda se aplica en orden de
    # llegada, y AWIN devuelve los cupones al final de cada anunciante: con 19
    # promos de Voghion, las 6 primeras (sin código) llenaban el cupo y sus 4
    # cupones reales — los únicos con código de todo AWIN — se perdían siempre.
    crudas.sort(key=lambda p: 0 if ((p.get("voucher") or {}).get("code") or "").strip() else 1)

    vistos: set = set()          # (tienda, titulo) → dedup
    por_tienda: dict = {}
    out: list[dict] = []
    for p in crudas:
        try:
            fin = p.get("endDate")
            if fin:
                try:
                    if datetime.fromisoformat(fin.replace("Z", "+00:00")) < ahora:
                        continue  # expirada
                except ValueError:
                    pass
            tienda = _limpiar_tienda((p.get("advertiser") or {}).get("name", ""))
            titulo = (p.get("title") or "").strip()
            if not tienda or not titulo:
                continue
            clave = (tienda, titulo.lower())
            if clave in vistos:
                continue
            if por_tienda.get(tienda, 0) >= _MAX_POR_TIENDA:
                continue
            vistos.add(clave)
            por_tienda[tienda] = por_tienda.get(tienda, 0) + 1
            url_track = (p.get("urlTracking") or p.get("url") or "").strip()
            out.append({
                "promo_id":    str(p.get("promotionId") or ""),
                "tienda":      tienda,
                "titulo":      titulo,
                "descripcion": (p.get("description") or "").strip(),
                "codigo":      ((p.get("voucher") or {}).get("code") or "").strip(),
                "url":         url_track,
                "start_date":  p.get("startDate") or "",
                "end_date":    fin or "",
                "estado":      p.get("status") or "active",
            })
        except Exception:
            continue
    print(f"   🎟️  AWIN promos: {len(out)} activas curadas (de {len(crudas)} crudas)")
    return out
