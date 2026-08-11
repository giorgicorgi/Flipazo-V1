"""
scrapers/price_drop.py — Detección GENÉRICA de bajadas de precio por histórico propio.

Para tiendas cuyos feeds NO traen "precio antes" (El Corte Inglés, Privé by Zalando,
Deporte Outlet…), registramos su precio cada día en `price_history` y aquí detectamos
cuándo un producto cae ≥X% respecto a su **precio de referencia**: el precio HABITUAL del
producto en la ventana. El descuento es REAL, verificado por nosotros.

"Habitual" exige tres cosas, y las tres nacen de fallos reales:

  1. Sostenido — vigente ≥`min_dias_ref` días distintos. Un pico de un solo día suele ser
     un error de dato, no un precio.
  2. Dominante — vigente en ≥`ref_cuota_min` (40%) de los días observados. Sin esto basta
     con que un precio absurdo aguante 3 días para convertirse en la referencia: así
     publicamos una mesa de jardín de Brico Depot a 29,95€ "antes 189€" (-84%) cuando el
     precio de verdad eran 35€ y los 189€ eran un pico que iba y venía. Su histórico:
     35€ → 14 días · 189€ → 7 días · 29,95€ → 7 días. Con la cuota, la referencia pasa a
     ser 35€ y la "oferta" queda en un -14% honesto que no se publica.
  3. Sin yoyó — si el precio bajo YA existía antes de que la referencia dejara de estar
     vigente, la serie oscila y no hay tal bajada: es su precio normal yendo y viniendo.

Las tres son genéricas: no miran qué tienda es, solo la forma de la serie.

Genérico: NO conoce ninguna tienda concreta. Cualquier scraper/feed que (a) acumule
histórico en `price_history` y (b) tenga el producto actual (título/URL/imagen) puede usar:

    refs = cargar_referencias(db_path, tiendas)          # 1 query, dict en memoria
    res  = evaluar_bajada(refs.get((pid, tienda)), precio_actual)
    if res:
        precio_original, descuento_pct = res

Parámetros tunables por entorno:
    PRICE_DROP_MIN_PCT     (def 40)  — % mínimo de bajada para publicar
    PRICE_DROP_VENTANA_DIAS(def 30)  — ventana de histórico considerada
    PRICE_DROP_MIN_DIAS    (def 7)   — días distintos de datos exigidos (fiabilidad)
    PRICE_DROP_MIN_DIAS_REF(def 3)   — días que el precio de referencia debe haberse sostenido
    PRICE_DROP_REF_CUOTA   (def 0.40)— fracción de los días observados que debe ocupar la referencia
    PRICE_DROP_CAP_PCT     (def 85)  — descuento máx. (descarta errores de dato)
"""

import os
import sqlite3
from datetime import datetime, timedelta

DESC_MIN      = int(os.getenv("PRICE_DROP_MIN_PCT", "40"))
VENTANA_DIAS  = int(os.getenv("PRICE_DROP_VENTANA_DIAS", "30"))
MIN_DIAS      = int(os.getenv("PRICE_DROP_MIN_DIAS", "7"))
MIN_DIAS_REF  = int(os.getenv("PRICE_DROP_MIN_DIAS_REF", "3"))
REF_CUOTA_MIN = float(os.getenv("PRICE_DROP_REF_CUOTA", "0.40"))
CAP_DESCUENTO = int(os.getenv("PRICE_DROP_CAP_PCT", "85"))


def cargar_referencias(db_path: str, tiendas, ventana_dias: int = VENTANA_DIAS,
                       min_dias: int = MIN_DIAS, min_dias_ref: int = MIN_DIAS_REF,
                       ref_cuota_min: float = REF_CUOTA_MIN) -> dict:
    """Devuelve {(asin, tienda): (precio_referencia, n_dias)} para los productos de
    `tiendas` con al menos `min_dias` de histórico en los últimos `ventana_dias` días.

    `precio_referencia` = el precio HABITUAL: el más alto que estuvo vigente ≥`min_dias_ref`
    días distintos **y** al menos `ref_cuota_min` de los días observados. Sin la cuota, un
    precio erróneo que aguante 3 días se convierte en la referencia (ver cabecera del módulo).
    Se descartan además las series en yoyó, donde el precio bajo ya existía antes de que la
    referencia dejara de estar vigente.

    Una sola consulta `GROUP BY asin,tienda,precio` que se recorre en streaming agrupando
    por producto → memoria baja aunque haya millones de filas. El MIN/MAX(fecha) por nivel
    de precio sale de ese mismo GROUP BY, así que detectar el yoyó no cuesta otra pasada."""
    tiendas = list(tiendas or [])
    if not tiendas or not db_path:
        return {}
    desde = (datetime.now() - timedelta(days=ventana_dias)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(tiendas))
    umbral_bajo = 1 - DESC_MIN / 100.0     # por debajo de esto ya sería "chollo" vs la referencia
    out: dict = {}

    def _flush(key, niveles):
        """niveles = {precio: (dias, primera_fecha, ultima_fecha)}"""
        if not key or not niveles:
            return
        total = sum(d for d, _, _ in niveles.values())  # 1 precio por día → total = días distintos
        if total < min_dias:
            return
        minimo_dias = max(min_dias_ref, total * ref_cuota_min)
        sostenidos = [(p, ult) for p, (d, _, ult) in niveles.items() if d >= minimo_dias]
        if not sostenidos:
            return
        pref, pref_ultimo_dia = max(sostenidos)         # max por precio
        # Yoyó: si un precio lo bastante bajo como para ser "oferta" ya se había visto
        # ANTES de que la referencia dejara de estar vigente, el precio oscila y no hay bajada.
        tope = pref * umbral_bajo
        if any(pri < pref_ultimo_dia for p, (_, pri, _) in niveles.items() if p <= tope):
            return
        out[key] = (pref, total)

    try:
        with sqlite3.connect(db_path) as con:
            cur = con.execute(
                f"SELECT asin, tienda, precio, COUNT(DISTINCT fecha) AS dias, "
                f"       MIN(fecha) AS primera, MAX(fecha) AS ultima "
                f"FROM price_history "
                f"WHERE tienda IN ({placeholders}) AND fecha >= ? AND precio > 0 "
                f"GROUP BY asin, tienda, precio "
                f"ORDER BY asin, tienda",
                (*tiendas, desde),
            )
            key = None
            niveles: dict = {}
            for asin, tienda, precio, dias, primera, ultima in cur:
                k = (asin, tienda)
                if k != key:
                    _flush(key, niveles)
                    key, niveles = k, {}
                niveles[precio] = (dias, primera, ultima)
            _flush(key, niveles)
    except Exception as e:
        print(f"   ⚠️  price_drop.cargar_referencias: {e}")
    return out


def evaluar_bajada(ref, precio_actual: float, descuento_min: int = DESC_MIN,
                   cap_descuento: int = CAP_DESCUENTO):
    """Dado el ref `(precio_referencia, n_dias)` y el precio actual, devuelve
    `(precio_referencia, descuento_pct)` si es una bajada real ≥`descuento_min`, o None."""
    if not ref or precio_actual <= 0:
        return None
    pref = ref[0]
    if pref <= 0 or pref <= precio_actual:
        return None
    desc = round((1 - precio_actual / pref) * 100)
    if desc < descuento_min or desc > cap_descuento:
        return None
    return (pref, desc)


def revalidar_publicados(db_path: str, tiendas, descuento_min: int = DESC_MIN) -> int:
    """Caduca los deals vivos cuyo "precio antes" ha dejado de ser cierto. Devuelve cuántos.

    Un descuento contra el histórico caduca solo, aunque nadie toque el precio: si el
    producto lleva tres semanas al precio "de oferta", ese ES su precio y la referencia
    antigua se ha salido de la ventana. Sin esta revisión el deal sigue vivo anunciando
    un ahorro que ya no existe — es como llegamos a tener 38 deals de El Corte Inglés,
    Adidas y Deporte Outlet diciendo "-50%" sobre precios de hace un mes.

    Solo mira deals con `hist_pid` (los detectados por bajada propia) y solo caduca
    cuando el histórico da un veredicto claro; si el producto ya no está en la ventana
    no hay con qué juzgarlo y se deja en paz."""
    tiendas = list(tiendas or [])
    if not tiendas or not db_path:
        return 0
    caducados = 0
    try:
        refs = cargar_referencias(db_path, tiendas)
        with sqlite3.connect(db_path) as con:
            placeholders = ",".join("?" * len(tiendas))
            vivos = con.execute(
                f"SELECT deal_id, titulo, tienda, precio, precio_original, hist_pid "
                f"FROM deals_publicados "
                f"WHERE tienda IN ({placeholders}) AND COALESCE(expirado,0)=0 "
                f"  AND COALESCE(hist_pid,'') != ''",
                tuple(tiendas),
            ).fetchall()
            for deal_id, titulo, tienda, precio, _p_orig, pid in vivos:
                ref = refs.get((pid, tienda))
                if ref is None:
                    continue          # sin histórico suficiente → no hay veredicto
                if evaluar_bajada(ref, precio, descuento_min=descuento_min):
                    continue          # el descuento sigue en pie
                con.execute(
                    "UPDATE deals_publicados SET expirado = 1 WHERE deal_id = ?", (deal_id,))
                caducados += 1
                print(f"   ⌛ ya no es oferta ({tienda}, ref {ref[0]}€ vs {precio}€): {titulo[:52]}")
            con.commit()
    except Exception as e:
        print(f"   ⚠️  price_drop.revalidar_publicados: {e}")
    return caducados
