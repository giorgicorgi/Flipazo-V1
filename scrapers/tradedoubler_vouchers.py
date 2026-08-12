"""
scrapers/tradedoubler_vouchers.py — Vouchers/cupones activos de Tradedoubler.

TD llama "vouchers" a los cupones/promos de tienda (códigos de descuento, promos).
API: GET https://api.tradedoubler.com/1.0/vouchers.json?token=<TRADEDOUBLER_VOUCHER_TOKEN>
⚠️ Requiere el token de la API de **Vouchers** (distinto del de productos; el de
productos da 403 "Token not authorized"). Se guarda en `.env` como
`TRADEDOUBLER_VOUCHER_TOKEN` (usar el token de tipo "voucher site").

Devuelve dicts en el MISMO formato que `awin_promotions.fetch_awin_promociones()`
para volcarlos en la tabla `promociones` (los muestra la web en /cupones).
"""

import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

TD_VOUCHER_TOKEN = os.getenv("TRADEDOUBLER_VOUCHER_TOKEN", "")
_API             = "https://api.tradedoubler.com/1.0/vouchers.json"
_MAX_POR_TIENDA  = int(os.getenv("TD_VOUCHER_MAX_POR_TIENDA", "6"))

# programName (tal cual viene de TD) → nombre limpio para mostrar
_TIENDA_LIMPIA = {
    "Tiendanimal ES": "Tiendanimal",
    "LOccitane":      "L'Occitane",
    "Moulinex ES":    "Moulinex",
    "Rowenta ES":     "Rowenta",
    "Esdemarca ES":   "Esdemarca",
    "Resuinsa Home":  "Resuinsa",
    "Desigual ES":    "Desigual",
    "Tefal ES":       "Tefal",
    "iHerb ES":       "iHerb",
    "WMF ES":         "WMF",
    "Toni Pons ES":   "Toni Pons",
    "Cole Haan España | Cole Haan Spain– colehaan.es": "Cole Haan",
}


def _limpiar(nombre: str) -> str:
    if nombre in _TIENDA_LIMPIA:
        return _TIENDA_LIMPIA[nombre]
    n = (nombre or "").split("|")[0].strip()
    if n.endswith(" ES"):
        n = n[:-3].strip()
    return n


def _ms_to_iso(ms) -> str:
    """TD da las fechas como epoch en MILISEGUNDOS (string). → ISO 8601 UTC."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def fetch_td_vouchers() -> list[dict]:
    """Vouchers/cupones activos de TD, curados (sin expirados, dedup por tienda+título,
    tope por tienda). list[dict] listo para la tabla `promociones`."""
    if not TD_VOUCHER_TOKEN:
        print("   ⚠️ TRADEDOUBLER_VOUCHER_TOKEN no configurado — skip vouchers TD")
        return []

    try:
        r = requests.get(f"{_API}?token={TD_VOUCHER_TOKEN}", timeout=40)
        if r.status_code != 200:
            print(f"   ⚠️ TD vouchers HTTP {r.status_code}: {r.text[:120]}")
            return []
        crudas = r.json()
        if isinstance(crudas, dict):
            crudas = crudas.get("vouchers", [])
        if not isinstance(crudas, list):
            crudas = []
    except Exception as e:
        print(f"   ❌ TD vouchers error: {e}")
        return []

    ahora = datetime.now(timezone.utc)

    # Los que traen CÓDIGO primero: el tope por tienda se aplica en orden de llegada
    # y no queremos que 6 promos sin código dejen fuera al único cupón canjeable
    # (le pasó a AWIN con Voghion — ver awin_promotions.py).
    crudas.sort(key=lambda v: 0 if (v.get("code") or "").strip() else 1)

    vistos: set = set()
    por_tienda: dict = {}
    out: list[dict] = []
    no_iniciados = expirados = 0
    for v in crudas:
        try:
            end_iso   = _ms_to_iso(v.get("endDate"))
            start_iso = _ms_to_iso(v.get("startDate"))
            if end_iso and datetime.fromisoformat(end_iso) < ahora:
                expirados += 1
                continue  # caducada
            # "NO INICIADO" en el panel de TD: el cupón existe pero aún no vale. Publicarlo
            # es mandar al usuario a una tienda donde el código no funciona todavía. Vuelve
            # solo: el fetch corre cada ciclo y lo recoge el día que arranca.
            if start_iso and datetime.fromisoformat(start_iso) > ahora:
                no_iniciados += 1
                continue
            tienda = _limpiar(v.get("programName", ""))
            titulo = (v.get("title") or v.get("shortDescription") or "").strip()
            if not tienda or not titulo:
                continue
            clave = (tienda, titulo.lower())
            if clave in vistos:
                continue
            if por_tienda.get(tienda, 0) >= _MAX_POR_TIENDA:
                continue
            vistos.add(clave)
            por_tienda[tienda] = por_tienda.get(tienda, 0) + 1
            out.append({
                "promo_id":    "td_" + str(v.get("id") or ""),
                "tienda":      tienda,
                "titulo":      titulo,
                "descripcion": (v.get("shortDescription") or v.get("description") or "").strip()[:300],
                "codigo":      (v.get("code") or "").strip(),
                "url":         (v.get("defaultTrackUri") or v.get("landingUrl") or "").strip(),
                "start_date":  start_iso,
                "end_date":    end_iso,
                "estado":      "active",
            })
        except Exception:
            continue
    print(f"   🎟️  TD vouchers: {len(out)} activos curados (de {len(crudas)} crudos · "
          f"{no_iniciados} aún no empiezan · {expirados} caducados)")
    return out
