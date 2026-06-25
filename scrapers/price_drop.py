"""
scrapers/price_drop.py — Detección GENÉRICA de bajadas de precio por histórico propio.

Para tiendas cuyos feeds NO traen "precio antes" (El Corte Inglés, Privé by Zalando,
Deporte Outlet…), registramos su precio cada día en `price_history` y aquí detectamos
cuándo un producto cae ≥X% respecto a su **precio de referencia**: el precio más alto que
ha estado vigente de forma SOSTENIDA (≥`min_dias_ref` días distintos) en la ventana. Usar
el "sostenido" en vez del máximo bruto evita que un pico de un solo día (error de dato)
genere un descuento falso. El descuento es REAL, verificado por nosotros.

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
    PRICE_DROP_CAP_PCT     (def 85)  — descuento máx. (descarta errores de dato)
"""

import os
import sqlite3
from datetime import datetime, timedelta

DESC_MIN      = int(os.getenv("PRICE_DROP_MIN_PCT", "40"))
VENTANA_DIAS  = int(os.getenv("PRICE_DROP_VENTANA_DIAS", "30"))
MIN_DIAS      = int(os.getenv("PRICE_DROP_MIN_DIAS", "7"))
MIN_DIAS_REF  = int(os.getenv("PRICE_DROP_MIN_DIAS_REF", "3"))
CAP_DESCUENTO = int(os.getenv("PRICE_DROP_CAP_PCT", "85"))


def cargar_referencias(db_path: str, tiendas, ventana_dias: int = VENTANA_DIAS,
                       min_dias: int = MIN_DIAS, min_dias_ref: int = MIN_DIAS_REF) -> dict:
    """Devuelve {(asin, tienda): (precio_referencia, n_dias)} para los productos de
    `tiendas` con al menos `min_dias` de histórico en los últimos `ventana_dias` días.

    `precio_referencia` = el precio más alto que estuvo vigente ≥`min_dias_ref` días distintos
    (precio "regular sostenido", robusto frente a picos puntuales).

    Una sola consulta `GROUP BY asin,tienda,precio` que se recorre en streaming agrupando
    por producto → memoria baja aunque haya millones de filas."""
    tiendas = list(tiendas or [])
    if not tiendas or not db_path:
        return {}
    desde = (datetime.now() - timedelta(days=ventana_dias)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(tiendas))
    out: dict = {}

    def _flush(key, dias_por_precio):
        if not key or not dias_por_precio:
            return
        total = sum(dias_por_precio.values())          # 1 precio por día → total = días distintos
        if total < min_dias:
            return
        sostenidos = [p for p, d in dias_por_precio.items() if d >= min_dias_ref]
        if not sostenidos:
            return
        out[key] = (max(sostenidos), total)

    try:
        with sqlite3.connect(db_path) as con:
            cur = con.execute(
                f"SELECT asin, tienda, precio, COUNT(DISTINCT fecha) AS dias "
                f"FROM price_history "
                f"WHERE tienda IN ({placeholders}) AND fecha >= ? AND precio > 0 "
                f"GROUP BY asin, tienda, precio "
                f"ORDER BY asin, tienda",
                (*tiendas, desde),
            )
            key = None
            dias_por_precio: dict = {}
            for asin, tienda, precio, dias in cur:
                k = (asin, tienda)
                if k != key:
                    _flush(key, dias_por_precio)
                    key, dias_por_precio = k, {}
                dias_por_precio[precio] = dias
            _flush(key, dias_por_precio)
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
