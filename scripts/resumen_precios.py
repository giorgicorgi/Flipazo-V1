#!/usr/bin/env python3
"""
resumen_precios.py — Precalcula el resumen por tienda del panel de admin.

El panel de Precios daba "error" y no era un fallo de código: las dos consultas
recorrían los 22,6 M de filas de `price_history` y tardaban **73 s y 56 s**. Por
HTTP eso no llega nunca — cualquier timeout las corta antes.

Aquí se calculan una vez al día y se guardan en `precios_resumen`. El panel pasa
de 130 segundos a leer 35 filas.

Cron:
    45 6 * * *  /home/flipazo/app/venv/bin/python /home/flipazo/app/scripts/resumen_precios.py \
                >> /home/flipazo/app/resumen.log 2>&1

Va ANTES del vigilante de las 7:15 para que el dato del día ya esté listo.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "flipazo_deals.db"))


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {m}", flush=True)


ESQUEMA = """
CREATE TABLE IF NOT EXISTS precios_resumen (
    tienda          TEXT PRIMARY KEY,
    obs             INTEGER,   -- filas de histórico
    productos       INTEGER,   -- productos distintos
    dias            INTEGER,   -- días distintos con datos
    ultima          TEXT,      -- última fecha registrada
    con_movimiento  INTEGER,   -- productos que cambiaron de precio en 14 días
    total_30d       INTEGER,   -- productos vistos en los últimos 30 días
    listos          INTEGER,   -- de esos, con >= 7 días (pueden generar deal)
    calculado_en    TEXT NOT NULL
)
"""


def main() -> int:
    if not os.path.exists(DB_PATH):
        log(f"❌ No existe la base de datos: {DB_PATH}")
        return 1
    t0 = time.time()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(ESQUEMA)

    log("calculando totales por tienda…")
    t = time.time()
    base = {r["tienda"]: dict(r) for r in con.execute("""
        SELECT tienda, COUNT(*) AS obs, COUNT(DISTINCT asin) AS productos,
               COUNT(DISTINCT fecha) AS dias, MAX(fecha) AS ultima
        FROM price_history GROUP BY tienda
    """)}
    log(f"   {len(base)} tiendas · {time.time() - t:.0f}s")

    # Productos cuyo precio se ha movido: un catálogo congelado no dará ofertas
    # jamás, y sin este número no hay forma de distinguirlo de uno sano.
    log("calculando movimiento de precios (14 días)…")
    t = time.time()
    movidos = {r["tienda"]: r["n"] for r in con.execute("""
        SELECT tienda, COUNT(*) AS n FROM (
          SELECT tienda, asin FROM price_history
          WHERE fecha >= date('now','-14 day')
          GROUP BY tienda, asin HAVING COUNT(DISTINCT precio) > 1
        ) GROUP BY tienda
    """)}
    log(f"   {time.time() - t:.0f}s")

    log("calculando cobertura (30 días)…")
    t = time.time()
    cobertura = {r["tienda"]: (r["total"], r["listos"] or 0) for r in con.execute("""
        SELECT tienda, COUNT(*) AS total,
               SUM(CASE WHEN dias >= 7 THEN 1 ELSE 0 END) AS listos
        FROM (
          SELECT tienda, asin, COUNT(DISTINCT fecha) AS dias
          FROM price_history WHERE fecha >= date('now','-30 day')
          GROUP BY tienda, asin
        ) GROUP BY tienda
    """)}
    log(f"   {time.time() - t:.0f}s")

    ahora = datetime.now().isoformat(timespec="seconds")
    filas = []
    for tienda, b in base.items():
        total, listos = cobertura.get(tienda, (0, 0))
        filas.append((tienda, b["obs"], b["productos"], b["dias"], b["ultima"],
                      movidos.get(tienda, 0), total, listos, ahora))
    con.execute("DELETE FROM precios_resumen")
    con.executemany(
        "INSERT INTO precios_resumen (tienda,obs,productos,dias,ultima,"
        "con_movimiento,total_30d,listos,calculado_en) VALUES (?,?,?,?,?,?,?,?,?)", filas)
    con.commit()

    congeladas = [f[0] for f in filas if f[5] == 0 and f[3] >= 7]
    log(f"✅ {len(filas)} tiendas resumidas en {time.time() - t0:.0f}s")
    if congeladas:
        log(f"   ⚠️  sin ningún cambio de precio en 14 días: {', '.join(congeladas)}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
