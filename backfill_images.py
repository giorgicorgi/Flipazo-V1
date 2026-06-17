"""
backfill_images.py — Rellena imagen_url en deals existentes sin imagen.
Extrae la imagen principal de la página de producto de Amazon via requests.
"""
import re
import sqlite3
import time
import requests

DB_PATH = "flipazo_deals.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

def get_amazon_image(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if not resp.ok:
            return ""
        html = resp.text
        # og:image es la imagen principal del producto
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        if m:
            return m.group(1)
        # Fallback: buscar en el JSON de imágenes de Amazon
        m = re.search(r'"hiRes":"(https://m\.media-amazon\.com/images/I/[^"]+)"', html)
        if m:
            return m.group(1)
        m = re.search(r'"large":"(https://m\.media-amazon\.com/images/I/[^"]+)"', html)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"   ❌ Error: {e}")
    return ""

con = sqlite3.connect(DB_PATH)
rows = con.execute(
    'SELECT deal_id, titulo, url_afiliado FROM deals_publicados WHERE imagen_url IS NULL OR imagen_url = ""'
).fetchall()

print(f"🔍 {len(rows)} deals sin imagen\n")

for deal_id, titulo, url in rows:
    print(f"  → {titulo[:50]}")
    img = get_amazon_image(url)
    if img:
        con.execute('UPDATE deals_publicados SET imagen_url = ? WHERE deal_id = ?', (img, deal_id))
        con.commit()
        print(f"     ✅ {img[:70]}")
    else:
        print(f"     ⚠️  No encontrada")
    time.sleep(2)

print("\n✅ Backfill completado")
