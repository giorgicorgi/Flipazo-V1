# Flipazo — Contexto del Proyecto para Claude Code

Canal de deals automatizado para España. Descuento ≥40% sobre precio histórico, filtrado con IA, publicado en Telegram y web (flipazo.es).

**Modelo de negocio:** freemium (deals públicos) + premium 3,90€/mes (tiempo real, canal privado).  
**Estado:** En producción en Hetzner. Pipeline completo funcionando. Threads activo (@flipazo.es). Pendiente: premium/Stripe, WhatsApp.

---

## Infraestructura de producción

| Componente | Detalle |
|---|---|
| Servidor | Hetzner VPS — IP `204.168.199.253` |
| Directorio app | `/home/flipazo/app/` ← **SIEMPRE subir aquí con scp** |
| Python env | `/home/flipazo/app/venv/bin/python` |
| Base de datos | SQLite en `/home/flipazo/app/flipazo_deals.db` |
| Web frontend | Vercel (auto-deploy desde GitHub push a `main`) |
| Repo GitHub | `https://github.com/giorgicorgi/Flipazo-V1` |
| Auto-deploy | `/home/flipazo/app/auto-deploy.sh` — cron cada 10 min |

**Servicios systemd:** `flipazo.service` (pipeline) · `flipazo-api.service` (uvicorn `api:app`, puerto 8081 — sirve `/api/deals`) · `flipazo-analytics.service` (uvicorn `analytics.tracker:app`, puerto 8080 — `/r/{id}` + `/stats`). nginx (80/443) hace reverse-proxy.

> ⚠️ Al desplegar **`api.py`** hay que reiniciar **`flipazo-api.service`** (NO el analytics). El `_ensure_schema` de api.py (migraciones de columnas + arranque del verificador de precios) corre en el `startup` de ESE servicio.

### Comandos de operación

```bash
# Deploy: git push → auto-deploy en ≤10 min
git add flipazo_main.py && git commit -m "fix: ..." && git push origin main

# Deploy inmediato: scp + restart
scp /Users/jorgeu/Desktop/Flipazo/flipazo_main.py root@204.168.199.253:/home/flipazo/app/flipazo_main.py
ssh root@204.168.199.253 "systemctl restart flipazo.service"

# Deploy inmediato de api.py → reiniciar flipazo-api.service (puerto 8081)
scp /Users/jorgeu/Desktop/Flipazo/api.py root@204.168.199.253:/home/flipazo/app/api.py
ssh root@204.168.199.253 "systemctl restart flipazo-api.service"

# Logs
ssh root@204.168.199.253 "journalctl -u flipazo.service -f --no-pager"
ssh root@204.168.199.253 "tail -30 /home/flipazo/app/deploy.log"

# DB (sqlite3 CLI no instalado — usar Python)
ssh root@204.168.199.253 "/home/flipazo/app/venv/bin/python -c \"
import sqlite3; con = sqlite3.connect('/home/flipazo/app/flipazo_deals.db')
for r in con.execute('SELECT titulo[:60], publicado_en FROM deals_publicados ORDER BY publicado_en DESC LIMIT 10').fetchall(): print(r)
\""
```

---

## Archivos clave

```
flipazo_main.py          ← MONOLITO PRINCIPAL (~2100 líneas, todo el pipeline)
api.py                   ← FastAPI: /api/deals + /r/{id} redirect
index.html               ← Frontend web (Vercel, scroll infinito, categorías)
affiliate/link_builder.py ← URLs afiliado por tienda (Amazon/Awin/Tradedoubler)
analytics/tracker.py     ← FastAPI analytics puerto 8080: /r/{id} + /stats
scrapers/tradedoubler_feed.py ← Feeds Tradedoubler (MediaMarkt/PCBox/ToniPons)
scrapers/pss_email.py    ← Lector Gmail IMAP para eventos PSS
discovery/               ← scoring.py + emotional_layer.py (hooks con Haiku)
.env                     ← Variables de entorno (NO subir a Git)
```

---

## Pipeline — Constantes clave en flipazo_main.py (~líneas 36-180)

- `DESCUENTO_MINIMO = 40` — % mínimo para cualquier deal
- `PRECIO_MINIMO = 25.0` / `PRECIO_MAXIMO = 800.0` (bici: 3500)
- `BENEFICIO_NETO_MINIMO = 20.0` — € mínimo para reventa
- `DEDUP_TTL_HORAS = 96` — TTL deduplicación (4 días)
- `CICLO_FLASH_MIN = 60` / `CICLO_COMPLETO_MIN = 120`
- `_SCORE_AUTO_APROBAR = 70` / `_SCORE_AUTO_DESCARTAR = 22`
- `RATIO_PRECIO_REF_INFLADO = 1.25` — detección precios inflados vs CCC

**Filtrado:** `PALABRAS_PROHIBIDAS` con frases específicas (NO subcadenas genéricas: `"café en grano"` ✓, `"café"` ✗). `_TALLA_RE` filtra ropa con tallas de letra (S/M/L/XL) pero permite tallas numéricas (zapatos).

**Scoring:** Pre-scorer local (sin IA) → zona gris (22-69 pts) → Claude Haiku. Discovery layer: `asignar_tags()` + `generar_hooks_batch()` (Haiku) enriquecen deals nuevos con tags emocionales y hook/social_context.

**Deduplicación:** SQLite, PRIMARY KEY `deal_id` = MD5(`tienda:asin_o_titulo`), TTL 96h.

**CCC:** `_scrape_ccc(asin)` → `(precio_min, precio_promedio)`. Si `precio_original > promedio × 1.25` → referencia inflada → recalcular o descartar.

---

## Tiendas

| Tienda | Fuente | Estado |
|---|---|---|
| Amazon | 16 categorías + /deals (filtro ≥40% en URL) | ✅ |
| MediaMarkt | Playwright + Feed TD (fid=24915, ~544 deals/día) | ✅ |
| PCBox | Feed TD (fid=50247, campo `PreviousPrice`) | ✅ |
| Toni Pons | Feed TD (fid=118025, ~248 deals/día, calzado) | ✅ |
| PcComponentes | 5 URLs Playwright (networkidle, slugs ≥2 guiones) | ✅ |
| Decathlon | 7 categorías Playwright | ✅ |
| Worten | 5 secciones Playwright | ❌ Sin implementar (no existe scrape_worten) |
| El Corte Inglés | 10 secciones Playwright | ⚠️ Cloudflare, circuit breaker 60min |
| Mammoth Bikes | 10 outlet pages (precios ES: `1.234,56 €`) | ✅ |
| Private Sport Shop | Gmail IMAP + Playwright | ⚠️ Cloudflare, circuit breaker |
| Beep | Feed TD (fid=51903) | ❌ PreviousPrice = MSRP → falsos descuentos |
| ToysRus | Feed TD (fid=21529) + histórico propio (`scrapers/toysrus_feed.py`, tablas `toysrus_precios`/`toysrus_productos`, clave EAN) | ✅ Armado; publica solo bajadas reales ≥40% → 0 ahora (juguetes bajan máx ~20%; saldrá en liquidaciones Reyes/BF). NO añadir a `_FEEDS_HISTORIAL` (duplicado) |
| Tiendanimal | Feed TD (fid=50625, `sale_price`) → Mascotas | ✅ Directo; 0 ≥40% ahora (falta oferta) |
| Carrefour | Feed AWIN (fid 76395,98228 = "Carrefour Supermercado Online") | ⚠️ Marketplace B2B ruidoso → **modo histórico** (`_SOLO_HISTORICO`) + allowlist de consumo (`_CARREFOUR_KEEP`) − blocklist (`_CARREFOUR_SKIP`) en `awin_feed.py`. Publica bajadas reales propias (~2 sem) |

**TD feeds:** Caché 23h en memoria. `offer["priceHistory"][0]["price"]["value"]` = precio actual. `TD_PUBLISHER_ID` debe ser el SITE ID (3481714), NO el publisher ID (2468812).

---

## API y Frontend

**api.py endpoints:**
- `GET /api/deals?limit=50&offset=0&tipo=OFERTA&tienda=Amazon`
- `GET /api/deals/count`
- `GET /r/{deal_id}?canal=web` → redirect afiliado + tracking

**index.html:** `USE_MOCK` activo solo en `file://` o localhost. `PAGE_SIZE = 24`. Categorías asignadas client-side por regex. `pollNew()` cada 5 min. IntersectionObserver en `#js-sentinel`.

**Afiliados (`affiliate/link_builder.py`):**
- Amazon → `?tag=flipazo-21`
- MediaMarkt/PCBox/Beep/ToysRus/Billabong/Cole Haan/Element/Elliotti/Beauty Corner → Tradedoubler deep link (`TD_PUBLISHER_ID=3481714`)
- PcComponentes/ECI/PSS → Awin (MIDs pendientes aprobación)
- Mammoth Bikes → Awin si configurado, sino URL directa

---

## Estado del desarrollo (mayo 2026)

| Módulo | Estado |
|---|---|
| Pipeline principal | ✅ Producción |
| Todos los scrapers activos | ✅ (ver tabla Tiendas) |
| Scoring Claude Haiku | ✅ Pre-scorer + zona gris |
| Discovery (hooks + tags) | ✅ Haiku, solo deals nuevos |
| Detección descuentos falsos CCC | ✅ |
| Análisis Wallapop | ✅ Solo ARBITRAJE |
| Publisher Telegram | ✅ Canal público |
| API FastAPI + Analytics | ✅ |
| Frontend Vercel | ✅ Scroll infinito, categorías, hamburguesa |
| Afiliados Amazon + TD | ✅ |
| Afiliados Awin | ⚠️ Solo MediaMarkt fallback activo |
| Threads (@flipazo.es) | ✅ Auto-publica cada deal; token auto-renovado 24h |
| Canal premium Telegram | 🔲 Pendiente |
| Bot Telegram + Stripe | 🔲 Pendiente |
| WhatsApp | ⚠️ Código listo, faltan credenciales WA_TOKEN |

---

## Scheduled trigger y skills

- **Trigger semanal:** `trig_01Um43n8top2mkvYsiFqzVpM` — análisis estático de `flipazo_main.py`. Gestión: https://claude.ai/code/scheduled/trig_01Um43n8top2mkvYsiFqzVpM
- **Skill `scraper-monitor`:** `.claude/skills/scraper-monitor/` — diagnóstico y reparación del pipeline. Invocar con `/scraper-monitor`
- **Skill `ui-ux-pro-max`:** `.claude/skills/ui-ux-pro-max/` — sistemas de diseño UI/UX
- **Agente `threads-storyteller`:** `.claude/agents/threads-storyteller.md` — redacta y publica HILOS narrativos (tuiteratura) en Threads @flipazo.es. Universo "el precio real de las cosas". Borrador → aprobación → publica encadenado. Las historias son a mano; los deals los publica el pipeline (`_threads_elegible`, score ≥70)

---

## Próximos pasos

1. **Canal premium + Stripe** — `/premium` bot → Stripe Checkout → webhook añade usuario al canal privado
2. **Nuevas fuentes TD** — HP Store (fid=38866), Esdemarca (fid=116972, 107k productos), Quiksilver/Roxy
3. **Resolver ECI y PSS** — feeds Awin cuando se aprueben los MIDs
4. **Más scrapers Playwright** — Sprinter, Zalando Outlet, Garmin Store

---

## Errores conocidos y soluciones

| Error | Causa | Solución |
|---|---|---|
| 0 productos MediaMarkt | Cookie consent / Category IDs caducados | Loop accept cookies + selector dual `/es/product/` + búsquedas por `?sort=discountPercentage_desc` |
| 0 productos PcComponentes | React SPA no hidratada | `wait_for_load_state("networkidle")` + slugs ≥2 guiones |
| Amazon solo una categoría | URLs sin filtro descuento | `rh=p_n_pct-off-with-tax%3A2388626011` en todas las URLs |
| Deal republicado al día siguiente | TTL dedup 24h | Cambiado a 96h |
| Imagen NULL en BD | Amazon lazy loading base64 | Saltar `data:` en src, usar `data-src`/`srcset` |
| Wallapop precio muy alto | Claude estimaba cerca de Amazon | Prompt: marcas premium 60-70%, chinas 40-55% |
| scp al directorio incorrecto | `/root/flipazo-deploy/` | SIEMPRE `/home/flipazo/app/` |
| Cafetera bloqueada | `"café"` como subcadena | Frases específicas: `"café en grano"`, `"café molido"` |
| Descuento falso precio inflado | Amazon sube precio ref. | CCC avg: si `precio_original > avg×1.25` → recalcular o descartar |
| Ropa tallas S/M/L publicada | Sin filtro tallas | `_TALLA_RE` — tallas letra bloqueadas, numéricas permitidas |
| TD feeds 0 deals | Precio en `priceHistory[0]` no en `price.value` | `offer["priceHistory"][0]["price"]["value"]` |
| TD_PUBLISHER_ID incorrecto | Confusión publisher vs site ID | Site ID 3481714 (parámetro `&a=` del deep link) |
| Protector solar de marca no se publica | `"protector"` (genérica) en `PALABRAS_PROHIBIDAS` cazaba "protector **solar**" por subcadena (estaba para protectores de pantalla) | Frases específicas: `"protector de pantalla"`, `"protector de cristal"`, `"protector de cámara"`. Dermocosmética de marca (`_MARCAS_DERMO`) eximida de los bloqueos de cosmética genérica |
| Título de deal no coincide con el producto/URL | Cross-check Amazon emparejaba por SKU coincidente (`_MODELO_RE` tomaba "H-6706" como modelo; un botín y unas fundas de cojín compartían ese código) → título de uno, URL/precio del otro | Cross-check solo para tiendas de electrónica (`_CROSSCHECK_AMAZON_TIENDAS`); `_MODELO_RE` exige ≥2 letras antes del guion; guard `_titulos_comparten_termino` (deben compartir ≥1 palabra real) |
