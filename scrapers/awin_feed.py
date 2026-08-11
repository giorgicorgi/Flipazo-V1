"""
scrapers/awin_feed.py — Lector del product feed de AWIN (Create-a-Feed).

La URL completa del feed (con apikey + fids + columnas) se guarda en `.env` como
`AWIN_FEED_URL` — NUNCA en código (contiene la credencial apikey).

Comportamiento por tienda (según lo que trae el feed):
  - Padel Market      → tiene `product_price_old` (precio antes) fiable → se PUBLICA
                        como deal si el descuento ≥ mínimo. `aw_deep_link` ya es el
                        enlace de afiliado, así que se usa directamente.
  - ECI / Zalando /   → el feed NO trae "precio antes". Se REGISTRA su precio diario en
    Deporte Outlet /    price_history y se detectan bajadas ≥X% vs su propio máximo
    Brico Depot         histórico (scrapers/price_drop.py, genérico) → se publican con
                        descuento REAL verificado por nosotros, en cuanto hay ≥7 días de datos.

Caché 23h en memoria, igual que el feed de Tradedoubler.
"""

import csv
import gzip
import io
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta

import collections

import requests
from dotenv import load_dotenv

from scrapers.price_drop import cargar_referencias, evaluar_bajada

load_dotenv()

AWIN_FEED_URL = os.getenv("AWIN_FEED_URL", "")

# Feeds ADICIONALES, cada uno con sus propios fids. Se leen por separado a
# propósito: meter comercios nuevos dentro del feed principal fue justo lo que
# dejó a El Corte Inglés fuera (Carrefour, 864k filas, lo empujó detrás del
# punto donde se cortaba la descarga). Un feed por lote = fallos aislados.
AWIN_FEED_URLS_EXTRA = [
    u for u in (os.getenv(f"AWIN_FEED_URL_{i}", "").strip() for i in range(2, 6)) if u
]

def _feeds_configurados() -> list[str]:
    return [u for u in ([AWIN_FEED_URL] + AWIN_FEED_URLS_EXTRA) if u]

_CACHE_TTL_H = 23
_cache: list[dict] = []
_last_fetch: datetime | None = None

# Motivo del último truncado, o None si el feed se leyó entero. Lo consulta
# flipazo_main para avisar al admin: un feed a medias es pérdida silenciosa de
# tiendas enteras, no un aviso cosmético.
ultimo_fetch_truncado: str | None = None

# True si la última llamada se sirvió de caché en vez de bajar el feed. Quien quiera
# hacer trabajo caro "una vez al día" debe mirarlo: la caché dura 23h pero la función
# se llama cada ciclo, así que volver de caché NO es lo mismo que no llamarla.
ultimo_fetch_cacheado: bool = False

# merchant_name (tal cual viene en el feed) → nombre de tienda interno de Flipazo
_MERCHANT_MAP = {
    "Padel Market":         "Padel Market",
    "adidas ES":            "Adidas",            # product_price_old == precio (no es "antes") → histórico
    "El Corte Ingles ES":   "ElCorteIngles",
    "BRICO DEPÔT_ES":       "Brico Depot",
    "Privé by Zalando ES":  "Zalando",
    "Deporte Outlet ES":    "Deporte Outlet",
    "Paco Perfumerias ES":  "Paco Perfumerias",  # perfumería, sin "precio antes"
    "BIKILA ES":            "Bikila",            # running/trail, sin "precio antes"
    "Carrefour Supermercado Online":  "Carrefour",  # marketplace ruidoso, product_price_old = PVP inflado → histórico + blocklist
    # Feed 2 (fids 78257,101515):
    "Foot Locker ES":       "Foot Locker",   # product_price_old REAL en 8.185/49.742 → publicable
    "TodoConsolas ES":      "TodoConsolas",  # product_price_old vacío en las 24.596 → histórico
    # Feed 3 (fid 78983): bricolaje/baño/iluminación. Cadena propia, no marketplace.
    "BAUHAUS ES":           "Bauhaus",       # product_price_old y rrp_price VACÍOS → histórico
}
# Tiendas con product_price_old fiable → se publican como deals
_PUBLICABLE = {"Padel Market", "Foot Locker"}
# Tiendas sin precio de referencia usable → solo histórico (registro diario de precio actual)
# para detectar bajadas ≥40% por histórico propio (los feeds no traen "precio antes" real;
# adidas trae product_price_old pero == precio actual, así que tampoco sirve).
# TodoConsolas: las 24.596 filas traen product_price_old VACÍO. Tiene rrp_price en
# 2.087, pero con mediana de solo -15% y siendo PVP de fabricante (la misma trampa
# que Beep) → no sirve como "precio antes". Histórico propio.
_SOLO_HISTORICO = {"ElCorteIngles", "Brico Depot", "Zalando", "Deporte Outlet",
                   "Paco Perfumerias", "Bikila", "Adidas", "Carrefour", "TodoConsolas",
                   "Bauhaus"}
# Carrefour "Supermercado Online" es en realidad un marketplace mayormente B2B/pro (tóner,
# hardware de servidor, AV de integración tipo Crestron/CTouch, reacondicionados). NO nos vale
# un blocklist (el 58% del catálogo ≥100€ es material profesional). Usamos ALLOWLIST de marcas
# de consumo deseables (solo esas se trackean) + un blocklist que quita consumibles que se cuelan
# por marca (p.ej. "Canon Tambor de impresora"). Solo histórico: publicamos bajadas reales propias.
_CARREFOUR_KEEP = re.compile(
    r"\bbose\b|\bsony\b|\bjbl\b|marshall|\bsonos\b|bang\s*&\s*olufsen|"
    r"\bcanon\b|\bnikon\b|fujifilm|gopro|\bdji\b|insta360|"
    r"garmin|fitbit|\bpolar\b|\bsuunto\b|amazfit|"
    r"philips|\bbraun\b|oral-?b|dyson|rowenta|cecotec|tefal|de'?longhi|delonghi|nespresso|krups|moulinex|\bsmeg\b|kitchenaid|"
    r"roomba|irobot|roborock|\bconga\b|"
    r"samsung|\blg\b|xiaomi|\btcl\b|hisense|"
    r"\bapple\b|\bipad\b|macbook|airpods|"
    r"nintendo|playstation|\bps5\b|\bxbox\b|"
    r"\blego\b|playmobil|\bnerf\b|"
    r"logitech|razer|corsair|steelseries|hyperx|"
    r"huawei|motorola|\bnothing\b|realme|"
    r"kindle|echo dot|fire tv|"
    r"whirlpool|\bbosch\b|siemens|\bbalay\b|\bteka\b|\bhaier\b|\bbeko\b|"
    r"\bnike\b|\badidas\b|\bpuma\b|new balance|reebok|"
    r"\bcasio\b|g-?shock|fossil|"
    r"sandisk|kingston|crucial|western digital|seagate|\bwd\b",
    re.I,
)
_CARREFOUR_SKIP = re.compile(
    # "cartucho" no bastaba: Carrefour lo abrevia ("Samsung Print Cart. Clt-m659s"),
    # y ese tóner acabó publicado como deal. Se añaden la abreviatura y las series
    # de tóner que se nombran solo por referencia (Samsung CLT-, Brother TN-).
    r"t[oó]ner|tinta|cartucho|print cart|\bcart\.|\bclt-[a-z]?\d|\btn-?\d{3}|"
    r"\bdrum\b|t[aá]mbor|fusor|\bpapel\b|\bdvd\b|dvd[+\-]?r|\bcd-?r\b|"
    # "switch" a secas bloqueaba también Nintendo Switch (la allowlist sí deja pasar
    # "nintendo", así que sus juegos y consolas se caían en silencio). Ahora solo se
    # bloquea el switch de RED, por contexto.
    r"\bhpe\b|servidor|\bserver\b|ethernet|base-?t|\bsfp\b|\brack\b|patch panel|"
    r"switch\s+(?:de\s+red|gigabit|poe|ethernet|gestionab|administrab|no\s+gestionad|\d+\s*puertos)|"
    r"(?:de\s+red|gigabit|poe|ethernet)\s+switch|"
    r"licencia|\blicense\b|warranty|garant[ií]a ext|reacondicionad|refurbish|renewed|open box|"
    r"segunda mano|recambio|repuesto|consumible|resma|etiquetas|precinto|embalaje",
    re.I,
)
# Suelo de precio para registrar histórico (evita inflar la BD: ECI son ~967k productos).
# A 100€ son ~246k obs/día; subir el suelo (env AWIN_HIST_PRECIO_MIN) reduce volumen.
_HIST_PRECIO_MIN = float(os.getenv("AWIN_HIST_PRECIO_MIN", "100"))
# Conservar histórico AWIN solo N días (acota el tamaño de price_history)
_HIST_DIAS = 45
# Máx. deals detectados por bajada por tienda y pasada (anti-flood en rebajas masivas)
_MAX_DETECT_POR_TIENDA = int(os.getenv("PRICE_DROP_MAX_POR_TIENDA", "40"))


def _to_float(s) -> float:
    try:
        return float(str(s).replace(",", ".").strip())
    except (ValueError, TypeError, AttributeError):
        return 0.0


def fetch_awin_productos(
    descuento_minimo: int = 40,
    precio_minimo: float = 25.0,
    precio_maximo: float = 9999.0,
    db_path: str | None = None,
    descuento_minimo_fn=None,
) -> list[dict]:
    """Descarga el feed AWIN, devuelve los deals publicables (Padel Market) y registra
    el histórico de precios de las tiendas sin precio de referencia (ECI/Brico).
    Caché 23h. Devuelve list[dict] compatible con el constructor de Producto."""
    global _cache, _last_fetch

    feeds = _feeds_configurados()
    if not feeds:
        return []

    global ultimo_fetch_cacheado
    ahora = datetime.now()
    if _last_fetch and (ahora - _last_fetch) < timedelta(hours=_CACHE_TTL_H):
        ultimo_fetch_cacheado = True
        print(f"   📦 AWIN caché activa: {len(_cache)} deals")
        return _cache
    ultimo_fetch_cacheado = False

    temporales: list[str] = []
    abiertos: list = []
    global ultimo_fetch_truncado
    ultimo_fetch_truncado = None

    def _descargar(url: str, etiqueta: str) -> str | None:
        """Baja el .gz ENTERO a disco antes de parsear.

        Antes se parseaba directamente del socket, y como el bucle hace trabajo por
        fila (lookup de referencia, acumular observaciones), éramos un consumidor
        lento: AWIN cortaba la conexión a mitad del stream (ProtocolError) tras
        150-260k filas de un feed de 1,9 M. Mientras El Corte Inglés empezaba sobre
        la fila 160k eso pasaba desapercibido, pero al entrar Carrefour en el feed
        (864k filas, justo antes de ECI) ECI se fue a la fila 1.024.428 y dejó de
        leerse por completo. Descargando primero, la red no depende del parseo."""
        with requests.get(url, stream=True, timeout=180) as r:
            if r.status_code != 200:
                print(f"   ❌ AWIN {etiqueta} HTTP {r.status_code} — se salta este feed")
                return None
            r.raw.decode_content = False  # el cuerpo ES gzip, no transfer-encoding
            with tempfile.NamedTemporaryFile(prefix="awin_feed_", suffix=".gz", delete=False) as tmp:
                nbytes = 0
                for chunk in r.raw.stream(1 << 20, decode_content=False):
                    tmp.write(chunk)
                    nbytes += len(chunk)
        print(f"   ⬇️  AWIN {etiqueta} descargado: {nbytes / 1048576:.0f} MB")
        return tmp.name

    try:
        print(f"   📡 AWIN feed (Create-a-Feed) — {len(feeds)} feed(s)...")
        lectores = []
        for i, url in enumerate(feeds, 1):
            etiqueta = "feed principal" if i == 1 else f"feed {i}"
            ruta = _descargar(url, etiqueta)
            if not ruta:
                continue
            temporales.append(ruta)
            _gz = gzip.open(ruta, "rb")
            abiertos.append(_gz)
            lectores.append(csv.DictReader(io.TextIOWrapper(_gz, encoding="utf-8", errors="replace")))
        if not lectores:
            print(f"   ❌ Ningún feed AWIN legible — se mantiene caché previa ({len(_cache)})")
            return _cache

        # Referencias de histórico (precio_max sostenido) por producto, para detectar
        # bajadas en las tiendas sin "precio antes" en el feed. 1 query antes de stremear.
        ref_index = cargar_referencias(db_path, sorted(_SOLO_HISTORICO)) if db_path else {}
        detect_cnt: collections.Counter = collections.Counter()

        publicables: list[dict] = []
        obs: list[tuple] = []  # (asin, tienda, precio, precio_ref, fecha) para price_history
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        n = 0

        def _rows_seguras(reader, etiqueta):
            # Si aun así el .gz llega corrupto, se procesan las filas leídas en vez de
            # perder el feed entero. PERO se marca como truncado: este "seguir adelante
            # a medias" fue lo que dejó a El Corte Inglés 2 semanas fuera sin que nada
            # chillara — el ciclo parecía correcto porque solo se imprimía un warning.
            global ultimo_fetch_truncado
            nonlocal n
            try:
                for _r in reader:
                    n += 1
                    yield _r
            except Exception as _e:
                ultimo_fetch_truncado = f"{etiqueta}: {type(_e).__name__} tras {n:,} filas"
                print(f"   ⚠️  AWIN {etiqueta} truncado a {n:,} filas ({type(_e).__name__}) — se procesan las leídas")

        def _todas_las_filas():
            for i, lector in enumerate(lectores, 1):
                yield from _rows_seguras(lector, "feed principal" if i == 1 else f"feed {i}")

        for row in _todas_las_filas():
            tienda = _MERCHANT_MAP.get((row.get("merchant_name") or "").strip())
            if not tienda:
                continue
            cur = _to_float(row.get("search_price"))
            if cur <= 0:
                continue

            # ── Tiendas solo-histórico (ECI/Zalando/Deporte/Brico) ───────────────
            # Registrar precio actual + detectar bajada ≥X% vs su propio máximo histórico.
            if tienda in _SOLO_HISTORICO:
                # Carrefour: solo marcas de consumo deseables (allowlist), sin consumibles (blocklist).
                if tienda == "Carrefour":
                    _nm = row.get("product_name") or ""
                    if not _CARREFOUR_KEEP.search(_nm) or _CARREFOUR_SKIP.search(_nm):
                        continue
                if cur >= _HIST_PRECIO_MIN:
                    pid = ((row.get("merchant_product_id") or row.get("aw_product_id") or "")).strip()[:60]
                    if pid:
                        obs.append((pid, tienda, cur, _to_float(row.get("product_price_old")), fecha_hoy))
                        # Detección de bajada por histórico propio (precio actual = feed de hoy)
                        if detect_cnt[tienda] < _MAX_DETECT_POR_TIENDA:
                            res = evaluar_bajada(ref_index.get((pid, tienda)), cur)
                            if res:
                                titulo = (row.get("product_name") or "").strip()
                                in_stock = (row.get("in_stock") or "").strip().lower() in ("1", "yes", "true", "y")
                                if titulo and in_stock:
                                    publicables.append({
                                        "titulo":          titulo,
                                        "asin":            (row.get("aw_deep_link") or "").strip(),
                                        "precio_actual":   cur,
                                        "precio_original": res[0],   # precio_max histórico
                                        "descuento_pct":   res[1],
                                        "tienda":          tienda,
                                        "imagen_url":      (row.get("merchant_image_url") or row.get("aw_image_url") or "").strip(),
                                        # Rastro al histórico: permite revalidar el descuento días después
                                        "hist_pid":        pid,
                                    })
                                    detect_cnt[tienda] += 1
                continue

            # ── Tiendas publicables (Padel Market): requieren precio de referencia ──
            if tienda in _PUBLICABLE:
                ref = _to_float(row.get("product_price_old"))
                in_stock = (row.get("in_stock") or "").strip().lower() in ("1", "yes", "true", "y")
                if ref <= cur or not in_stock:
                    continue
                if not (precio_minimo <= cur <= precio_maximo):
                    continue
                desc = int((1 - cur / ref) * 100)
                titulo = (row.get("product_name") or "").strip()
                dmin = descuento_minimo_fn(titulo, cur) if descuento_minimo_fn else descuento_minimo
                if desc < dmin:
                    continue
                publicables.append({
                    "titulo":          titulo,
                    "asin":            (row.get("aw_deep_link") or "").strip(),  # enlace afiliado AWIN
                    "precio_actual":   cur,
                    "precio_original": ref,
                    "descuento_pct":   desc,
                    "tienda":          tienda,
                    "imagen_url":      (row.get("merchant_image_url") or row.get("aw_image_url") or "").strip(),
                })

        # ── Registrar histórico ECI/Brico + podar antiguo ──────────────────────
        if obs and db_path:
            try:
                with sqlite3.connect(db_path) as con:
                    con.executemany(
                        "INSERT OR IGNORE INTO price_history (asin, tienda, precio, precio_original, fecha) "
                        "VALUES (?, ?, ?, ?, ?)",
                        obs,
                    )
                    desde = (datetime.now() - timedelta(days=_HIST_DIAS)).strftime("%Y-%m-%d")
                    _ph = ",".join("?" * len(_SOLO_HISTORICO))
                    con.execute(
                        f"DELETE FROM price_history WHERE tienda IN ({_ph}) AND fecha < ?",
                        (*sorted(_SOLO_HISTORICO), desde),
                    )
                    con.commit()
                print(f"   📈 AWIN histórico ECI/Brico: {len(obs)} observaciones registradas")
            except Exception as e:
                print(f"   ⚠️  AWIN histórico error: {e}")

        _cache = publicables
        _last_fetch = ahora
        n_detect = sum(detect_cnt.values())
        detalle = f" (incl. {n_detect} por bajada histórica: {dict(detect_cnt)})" if n_detect else ""
        print(f"   ✅ AWIN: {len(publicables)} deals publicables{detalle} · {n} filas · {len(obs)} obs histórico")
        return publicables

    except Exception as e:
        print(f"   ❌ AWIN feed error: {e} — se mantiene caché previa ({len(_cache)})")
        return _cache

    finally:
        # Los .gz temporales ocupan ~110 MB entre todos: fuera pase lo que pase.
        for _gz in abiertos:
            try:
                _gz.close()
            except Exception:
                pass
        for ruta in temporales:
            try:
                os.unlink(ruta)
            except OSError:
                pass
