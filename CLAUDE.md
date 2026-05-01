# Flipazo — Contexto del Proyecto para Claude Code

> Este archivo se carga automáticamente al abrir el proyecto.
> Refleja el estado REAL y actual del código, no aspiracional.

---

## Qué es Flipazo

Canal de deals automatizado para España. Encuentra ofertas con descuento ≥40% sobre precio histórico, las filtra con IA y las publica en Telegram y web propia (flipazo.es).

**Modelo de negocio:** freemium (deals públicos) + premium 3,90€/mes (tiempo real, canal privado).  
**Estado:** En producción en Hetzner. Pipeline completo funcionando. Pendiente: premium/Stripe, WhatsApp, Threads.

---

## Infraestructura de producción

| Componente | Detalle |
|---|---|
| Servidor | Hetzner VPS — IP `204.168.199.253` |
| Usuario deploy | `flipazo` |
| Directorio app | `/home/flipazo/app/` ← **SIEMPRE subir aquí con scp** |
| Python env | `/home/flipazo/app/venv/bin/python` |
| Base de datos | SQLite en `/home/flipazo/app/flipazo_deals.db` |
| Sesiones Playwright | `/home/flipazo/app/sesion_flipazo_completo/` (ciclo completo) y `sesion_flipazo_flash/` (flash) |
| Web frontend | Vercel (auto-deploy desde GitHub push a `main`) |
| Repo GitHub | `https://github.com/giorgicorgi/Flipazo-V1` |
| Git en servidor | `/home/flipazo/app/` es un repo git que trackea `origin/main` |
| Auto-deploy | `/home/flipazo/app/auto-deploy.sh` — cron cada 10 min, detecta commits nuevos y reinicia servicios |
| Log de deploys | `/home/flipazo/app/deploy.log` |

### Servicios systemd

```
flipazo.service           → pipeline principal (flipazo_main.py)
flipazo-analytics.service → servidor analytics (uvicorn analytics.tracker:app --port 8080)
```

### Comandos de operación

```bash
# Deploy preferido: git commit + push → auto-deploy en ≤10 min
git add flipazo_main.py && git commit -m "fix: ..." && git push origin main

# Deploy inmediato (bypass auto-deploy): scp directo + restart
scp /Users/jorgeu/Desktop/Flipazo/flipazo_main.py root@204.168.199.253:/home/flipazo/app/flipazo_main.py
ssh root@204.168.199.253 "systemctl restart flipazo.service"

# Ver log de auto-deploys
ssh root@204.168.199.253 "tail -30 /home/flipazo/app/deploy.log"

# Restart del servicio
ssh root@204.168.199.253 "systemctl restart flipazo.service"

# Ver logs en tiempo real
ssh root@204.168.199.253 "journalctl -u flipazo.service -f --no-pager"

# Ver últimas líneas
ssh root@204.168.199.253 "journalctl -u flipazo.service --no-pager -n 100"

# DB: ver deals recientes (usar Python, sqlite3 CLI no está instalado)
ssh root@204.168.199.253 "/home/flipazo/app/venv/bin/python -c \"
import sqlite3; con = sqlite3.connect('/home/flipazo/app/flipazo_deals.db')
for r in con.execute('SELECT titulo[:60], publicado_en FROM deals_publicados ORDER BY publicado_en DESC LIMIT 10').fetchall(): print(r)
\""
```

---

## Estructura de archivos real

```
/Users/jorgeu/Desktop/Flipazo/          ← directorio local
├── flipazo_main.py                     ← MONOLITO PRINCIPAL (todo el pipeline)
├── api.py                              ← FastAPI: /api/deals + /r/{id} redirect
├── index.html                          ← Frontend web (Vercel, estilo periódico)
├── aviso-legal.html                    ← Legal (sin datos personales, solo Flipazo)
├── privacidad.html                     ← Política de privacidad
├── cookies.html                        ← Política de cookies
├── affiliate/
│   └── link_builder.py                 ← URLs de afiliado por tienda (Amazon/Awin)
├── analytics/
│   └── tracker.py                      ← FastAPI analytics: /r/{id} + /stats
├── scrapers/
│   ├── pss_email.py                    ← Lector Gmail IMAP para eventos PSS
│   └── tradedoubler_feed.py            ← Feeds producto Tradedoubler (MediaMarkt/PCBox)
├── flipazo.service                     ← Systemd unit para el pipeline
├── flipazo_analytics.service           ← Systemd unit para el servidor analytics
├── .env                                ← Variables de entorno (NO subir a Git)
├── .env.example                        ← Plantilla de variables
├── requirements.txt                    ← Dependencias Python
└── setup_servidor.sh                   ← Script de configuración inicial del VPS
```

**Archivos legacy / sin uso activo:**
- `backfill_images.py` — script puntual ya ejecutado
- `debug_pss.py` — script de debug
- `debug_*.png` — screenshots de debug del scraper

---

## flipazo_main.py — Arquitectura completa

Todo el pipeline vive en un solo archivo (~2100 líneas). Estructura interna:

### Constantes clave (líneas ~36-180)

```python
DESCUENTO_MINIMO        = 40     # % mínimo para cualquier deal (subido de 35 → 40)
DESCUENTO_OFERTA_MINIMO = 40     # % mínimo para ofertas puras (subido de 35 → 40)
PRECIO_MINIMO           = 25.0   # € mínimo (excluye accesorios baratos)
PRECIO_MAXIMO           = 800.0  # € máximo general
PRECIO_MAXIMO_BICI      = 3500.0 # € máximo para Mammoth Bikes
BENEFICIO_NETO_MINIMO   = 20.0   # € ganancia mínima para deals de reventa
DEDUP_TTL_HORAS         = 96     # horas antes de poder republicar el mismo deal (4 días)
CICLO_FLASH_MIN         = 60     # intervalo ciclo rápido (Amazon)
CICLO_COMPLETO_MIN      = 120    # intervalo ciclo completo (todas las tiendas)

_SCORE_AUTO_APROBAR     = 70     # ≥70 → publicar sin Claude
_SCORE_AUTO_DESCARTAR   = 22     # <22  → descartar sin Claude
SCORE_OFERTA_MINIMO     = 58     # umbral Claude para OFERTA

RATIO_PRECIO_REF_INFLADO = 1.25  # Si precio_original > 125% del promedio hist. → ref. inflada
```

### Filtrado de productos (`_es_producto_valido`)

Dos capas de filtrado:

1. **`PALABRAS_PROHIBIDAS`** — subcadenas exactas en título (lowercase). Incluye frases específicas como `"café en grano"`, `"café molido"`, `"té verde"` (NO subcadenas genéricas como `"café"` o `"té"` que bloquearían cafeteras/termos).

2. **`_TALLA_RE`** — regex para ropa con tallas de letra (S/M/L/XL/XXL/XS). Permite tallas numéricas (zapatos 42, pantalón 32/32, etc.):
   ```python
   _TALLA_RE = re.compile(
       r'\bTalla\s+(?:XS|XXS|XXXL|XXL|XL|[SML])\b'
       r'|\bsize[:\s]+(?:XS|XXS|XXXL|XXL|XL|[SML])\b',
       re.IGNORECASE
   )
   ```

### Tiendas scrapeadas

| Tienda | URLs fuente | Estado |
|---|---|---|
| Amazon | 16 categorías + /deals | ✅ Funcional (filtro ≥40% off en URLs) |
| MediaMarkt | 6 búsquedas Playwright + feed TD | ✅ Playwright (bloqueado esporádicamente) + ✅ Feed TD (544 deals/día) |
| PCBox | Feed Tradedoubler (fid=50247) | ✅ Feed TD activo — precio original en campo `PreviousPrice` |
| Beep | Feed Tradedoubler (fid=51903) | ❌ Desactivado — PreviousPrice = MSRP fabricante, no precio 30d |
| PcComponentes | 5 URLs (ofertas + componentes + portátiles) | ✅ (networkidle wait, slugs ≥2 guiones) |
| Decathlon | 7 categorías | ✅ |
| Worten | 5 secciones | ✅ |
| El Corte Inglés | 10 secciones | ⚠️ Bloqueada frecuentemente (circuit breaker 60min) |
| Mammoth Bikes | 10 outlet pages | ✅ (precios ES: `1.234,56 €`) |
| Private Sport Shop | Via Gmail IMAP + Playwright | ⚠️ Bloqueada — circuit breaker activo |
| ToysRus | Feed TD disponible (fid=21529) | ❌ Feed sin precio original — descuento incalculable |

### Categorías Amazon (AMAZON_SEARCH_URLS)

Todas las URLs de búsqueda llevan `rh=p_n_pct-off-with-tax%3A2388626011` para filtrar directamente a ≥40% de descuento en Amazon.es:

```
electronics, computers, videogames, shoes (Nike/Jordan/Adidas),
beauty (perfumes), LEGO, diy (Bosch/DeWalt/Makita/Kärcher),
auriculares (Sony/Bose/AirPods), watches, kitchen, appliances,
toys (Playmobil/Hasbro), photo, sports,
hpc (Braun/Philips/Oral-B — afeitadoras, cepillos eléctricos),
kitchen (Rowenta/Shark/Tefal — pequeño electrodoméstico),
deals (amazon.es/deals — usa _extraer_de_deals, no _extraer_de_busqueda)
```

### Pipeline de ejecución

```
CICLO FLASH (cada 60 min):
  scrape_amazon_deals() → solo Amazon → scoring → Wallapop → Telegram

CICLO COMPLETO (cada 120 min):
  scrape_todas_las_tiendas()
    → scrape_amazon_deals()
    → scrape_mediamarkt()        ← Playwright (bloqueado si "Un momento…")
    → scrape_pccomponentes()
    → scrape_decathlon()
    → scrape_worten()
    → scrape_elcorteingles()
    → scrape_mammoth()
    → scrape_barrabes()
    → scrape_privatesportshop()  ← extrae URLs de Gmail, luego Playwright
    → fetch_tradedoubler_productos()  ← caché 23h; MediaMarkt+PCBox vía API REST
  → verificar_historial_ccc()   ← CamelCamelCamel (solo productos Amazon con ASIN)
  → score_con_claude()          ← Haiku: pre-scorer local + zona gris
  → obtener_precio_wallapop()   ← solo deals tipo ARBITRAJE
  → publicar en Telegram
  → DeduplicacionDB.marcar_publicado()
```

### Verificación CCC y detección de descuentos falsos (`verificar_con_ccc`)

`_scrape_ccc(asin)` devuelve `tuple[float, float]` → `(precio_minimo, precio_promedio)`.

`verificar_con_ccc` aplica **dos checks** en orden:

1. **Detección de referencia inflada:** Si `precio_original > precio_promedio_hist × 1.25`, la referencia de Amazon está inflada artificialmente. Se calcula el descuento real contra el promedio histórico:
   - Si descuento real < `DESCUENTO_MINIMO` → ❌ descartar ("Descuento falso")
   - Si descuento real ≥ `DESCUENTO_MINIMO` → corregir `precio_original` y `descuento_pct` con valores honestos

2. **Check precio actual vs mínimo histórico:** `precio_actual / precio_min_hist ≤ RATIO_HISTORICO_MAX` → publicar. Si supera el ratio → ❌ descartar.

### Scoring (score_con_claude)

**Etapa 1 — Pre-scorer local (sin IA):**
- Descuento ≥65% → +40 pts; ≥55% → +30; ≥45% → +20; ≥40% → +10
- Marca conocida → +30 pts (whitelist extensa: Apple, Sony, Nike, Braun, Xiaomi, Lefant, etc.)
- Precio 30-400€ → +15 pts; 400-600€ → +8 pts
- Historial CCC: precio en mínimo histórico → +15; penaliza si inflado

**Etapa 2 — Claude Haiku (solo zona gris 22-69 pts):**
- Prompt `PROMPT_SCORING` → JSON con `score_reventa`, `score_oferta`, `precio_wallapop_estimado`
- `tipo`: `"ARBITRAJE"` | `"OFERTA"` | `"DESCARTAR"`
- Wallapop: estimaciones conservadoras: marcas premium 60-70% del precio Amazon; marcas chinas/desconocidas 40-55%
- El prompt indica explícitamente que `descuento_pct` y `precio_original` ya fueron validados contra CCC antes de llegar al scoring

### Deduplicación (SQLite)

- Tabla `deals_publicados` — PRIMARY KEY: `deal_id` (MD5 de `tienda:asin_o_titulo`)
- `INSERT OR REPLACE` — actualiza el deal si ya existía
- TTL: 96h. `ya_publicado()` devuelve True si publicado en las últimas 96h

### Marcas conocidas (whitelist _MARCAS_CONOCIDAS)

Apple, Samsung, Sony, LG, Philips, Bosch, Dyson, Nike, Adidas, New Balance, Jordan, Asics, Puma, Reebok, LEGO, Nintendo, PlayStation, Xbox, Bose, AirPods, Jabra, Sennheiser, Nespresso, DeLonghi, Tefal, Rowenta, Braun, Siemens, Oral-B, Remington, Wahl, Panasonic, Shark, Bissell, Kärcher, Kenwood, Dior, Chanel, Armani, Calvin Klein, Lacoste, North Face, Roborock, Roomba, iRobot, Lefant, Dreame, Ecovacs, Eufy, Cecotec, Xiaomi, Redmi, Kindle, GoPro, Garmin, Fitbit, Polar, Makita, DeWalt, Milwaukee, Stanley, Canon, Nikon, HP, Dell, Lenovo, Asus, Acer, Microsoft, Logitech, Razer, G-Shock, Casio, Seiko, Citizen, **Breville, Sage**

### Palabras prohibidas (PALABRAS_PROHIBIDAS)

Excluye accesorios baratos, alimentación, champús, ropa básica (vaqueros, camisetas, etc.), zapatillas gama baja (Tanjun, Revolution, etc.), multipacks genéricos.

**IMPORTANTE:** Usar frases específicas, NO palabras genéricas como subcadenas. Ejemplo correcto: `"café en grano"`, `"café molido"` — NO `"café"` (bloquearía cafeteras). Mismo principio con `"té verde"` en vez de `"té"`.

---

## Dataclass Producto

```python
@dataclass
class Producto:
    titulo: str
    asin: str              # ASIN para Amazon; URL completa para otras tiendas
    precio_actual: float
    precio_original: float
    descuento_pct: int
    tienda: str = "Amazon"
    tipo: str = "PENDIENTE"        # "ARBITRAJE" | "OFERTA" | "DESCARTAR"
    precio_historico_min: float = 0.0
    score_ai: int = 0
    score_liquidez: int = 0
    score_oferta: int = 0
    resale_viable: bool = False
    precio_wallapop: float = 0.0
    razonamiento: str = ""
    imagen_url: str = ""

    @property
    def beneficio_neto(self) -> float:
        # precio_wallapop × 0.87 - precio_actual - 7 (comisión 13% + envío/embalaje 7€)

    @property
    def url_affiliate(self) -> str:
        # build_affiliate_url(self.tienda, self.asin)
```

---

## API y Frontend

### api.py (FastAPI)

Endpoints en producción:
- `GET /api/deals?limit=50&offset=0&tipo=OFERTA&tienda=Amazon` → JSON de deals
- `GET /api/deals/count` → total de deals
- `GET /r/{deal_id}?canal=web` → redirect afiliado + tracking click
- `GET /health`

### analytics/tracker.py (FastAPI, puerto 8080)

- `GET /r/{deal_id}?canal=telegram` → redirect + click en SQLite
- `GET /stats/{deal_id}` → clicks por canal
- `GET /stats` → top 20 deals por clicks últimas 72h

### index.html (Vercel)

Frontend estilo periódico con scroll infinito y categorías. Variables clave en el JS:
```javascript
const USE_MOCK = false;                        // false = datos reales de la API
const API_URL  = "https://flipazo.es/api/deals";
const PAGE_SIZE = 24;                          // deals por batch de scroll infinito
```

**Arquitectura JS:**
- `allDeals[]` — todos los deals cargados en memoria
- `currentOffset` — offset para paginación
- `activeCategory` — categoría activa (por defecto `"todas"`)
- `lastSeenRowid` — para detección de deals nuevos en `pollNew()`
- `loadMore()` — carga siguiente batch y filtra por categoría activa
- `renderGrid()` — re-renderiza desde `allDeals[]` filtrando por `activeCategory`
- `pollNew()` — cada 5 min, antepone deals nuevos al principio
- `IntersectionObserver` sobre `#js-sentinel` con `rootMargin: 300px`

**Categorías (9 total):** Todas, Tecnología, Herramientas, Deportes, Calzado, Hogar, Belleza, Juguetes, Moda — asignadas client-side por regex sobre `titulo` y tienda.

**Menú hamburguesa:** Botón `☰` en masthead que abre `.cat-bar` (panel deslizante via `max-height` CSS transition) con pills por categoría.

Componentes del deal card:
- `deal__image-wrap` con `deal__discount-badge` (% sobre imagen)
- `deal__pricing`: precio original tachado + precio actual (30px) + `deal__pct` (% en rojo)
- `deal__savings`: "↓ Ahorras X€" en verde
- `deal__reventa`: caja verde con math de Wallapop (solo tipo ARBITRAJE)
- `deal__time`: timestamp relativo + absoluto

---

## affiliate/link_builder.py

```python
build_affiliate_url(tienda, asin_or_url) → str

# Tiendas soportadas:
"Amazon"           → https://www.amazon.es/dp/{asin}?tag=flipazo-21
"MediaMarkt"       → Tradedoubler deep link (prioritario) / Awin fallback
"Beep"             → Tradedoubler deep link (TD_PID=347347)
"ToysRus"          → Tradedoubler deep link (TD_PID=211811)
"Billabong"        → Tradedoubler deep link
"Cole Haan"        → Tradedoubler deep link
"Element Brand"    → Tradedoubler deep link
"Elliotti"         → Tradedoubler deep link
"The Beauty Corner"→ Tradedoubler deep link
"PcComponentes"    → Awin deep link (PCCOMPONENTES_AWIN_MID — pendiente)
"ElCorteIngles"    → Awin deep link (ELCORTEINGLES_AWIN_MID — pendiente)
"PrivateSportShop" → Awin deep link (PRIVATESPORTSHOP_AWIN_MID — pendiente)
"Mammoth Bikes"    → Awin deep link si MAMMOTH_AWIN_MID configurado; si no, URL directa
cualquier otra     → URL directa (sin perder el deal)

# Tradedoubler deep link formato:
# https://clk.tradedoubler.com/click?p={PROGRAM_ID}&a={TD_PUBLISHER_ID}&url={encoded_url}
# TD_PUBLISHER_ID = site ID 3481714 (NO el publisher ID 2468812)
```

---

## Variables de entorno (.env en /home/flipazo/app/.env)

```bash
# Claude API
ANTHROPIC_API_KEY=sk-ant-...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID_PUBLIC=...        # Canal público
TELEGRAM_CHAT_ID_PREMIUM=...       # Canal privado (pendiente)
TELEGRAM_ADMIN_CHAT_ID=...         # Chat personal para alertas de error

# Amazon
AMAZON_AFFILIATE_TAG=flipazo-21

# Awin (afiliados no-Amazon)
AWIN_PUBLISHER_ID=...
MEDIAMARKT_AWIN_MID=6907           # fallback si TD no configurado
PCCOMPONENTES_AWIN_MID=            # pendiente aprobación
ELCORTEINGLES_AWIN_MID=            # pendiente aprobación
MAMMOTH_AWIN_MID=                  # pendiente solicitar

# Tradedoubler (publisher ID 2468812, site ID 3481714)
# IMPORTANTE: TD_PUBLISHER_ID debe ser el SITE ID (3481714), no el publisher ID
TD_PUBLISHER_ID=3481714
TRADEDOUBLER_TOKEN=                # token tipo PRODUCTS/SITE — Ajustes → Tokens en panel TD
MEDIAMARKT_TD_PID=270504
BEEP_TD_PID=347347
TOYSRUS_TD_PID=211811              # deep link OK, feed sin precio original

# Gmail IMAP (para PSS newsletter)
EMAIL_ADDRESS=flipazo.newsletter@gmail.com
EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
PSS_EMAIL_SENDER=thomas@ese.privatesportshop.com

# Stripe (pendiente)
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID=price_...

# Tracking
REDIRECT_BASE_URL=https://flipazo.es
DEBUG_SCREENSHOTS=false
```

---

## Estado actual del desarrollo (abril 2026)

| Módulo | Estado | Notas |
|---|---|---|
| Pipeline principal | ✅ Producción | flipazo_main.py — monolito |
| Scraper Amazon | ✅ Funcional | 16 categorías + /deals; filtro ≥40% off en URL |
| Scraper MediaMarkt | ✅ Funcional | Cookie accept + selector dual + timeout 20s |
| Scraper PcComponentes | ✅ Funcional | networkidle wait + slugs ≥2 guiones + 5 URLs |
| Scraper Decathlon | ✅ Funcional | |
| Scraper Worten | ✅ Funcional | |
| Scraper El Corte Inglés | ⚠️ Inestable | Cloudflare — circuit breaker 60min |
| Scraper Mammoth Bikes | ✅ Funcional | 10 outlet pages (cascos, componentes, accesorios añadidos) |
| Scraper PSS | ⚠️ Bloqueado | Cloudflare duro (2381 chars) |
| Scoring Claude Haiku | ✅ Funcional | Pre-scorer local + zona gris |
| Detección descuentos falsos | ✅ Funcional | CCC avg vs precio_original × 1.25 |
| Filtro ropa con tallas | ✅ Funcional | Regex tallas de letra (S/M/L/XL), no numéricas |
| Análisis Wallapop | ✅ Funcional | Solo deals ARBITRAJE |
| Publisher Telegram | ✅ Funcional | Canal público solamente |
| Deduplicación SQLite | ✅ Funcional | TTL 96h (4 días) |
| API FastAPI | ✅ Funcional | /api/deals + /r/{id} |
| Frontend Vercel | ✅ Funcional | Scroll infinito + categorías + hamburguesa |
| Analytics tracker | ✅ Funcional | Puerto 8080, clicks en SQLite |
| Feed TD MediaMarkt | ✅ Activo | `scrapers/tradedoubler_feed.py` — 544 deals/día, caché 23h |
| Feed TD PCBox | ✅ Activo | fid=50247, ~11 deals/día ≥35%, monitores/cajas/componentes PC |
| Feed TD Beep | ❌ Desactivado | PreviousPrice = MSRP fabricante → falsos descuentos sistemáticos |
| Feed TD ToysRus | ❌ Sin precio original | fid=21529 sin campo precio ref → descuento incalculable |
| Afiliados Amazon | ✅ Activo | tag flipazo-21 |
| Afiliados Tradedoubler | ✅ Activo | MediaMarkt, PCBox, Beep, ToysRus, Billabong, Cole Haan, Element, Elliotti, Beauty Corner |
| Afiliados Awin | ⚠️ Parcial | Solo MediaMarkt fallback activo |
| Páginas legales | ✅ Actualizado | Sin datos personales — solo "Flipazo, Barcelona, España" |
| Auto-deploy servidor | ✅ Activo | cron cada 10 min → git pull + restart si hay commits nuevos |
| Skill scraper-monitor | ✅ Activo | `.claude/skills/scraper-monitor/` |
| Scheduled trigger 7am | ✅ Activo | `trig_01Um43n8top2mkvYsiFqzVpM` — análisis estático diario |
| Canal premium Telegram | 🔲 Pendiente | |
| Bot Telegram + Stripe | 🔲 Pendiente | |
| WhatsApp publisher | 🔲 Pendiente | |
| Threads publisher | 🔲 Pendiente | |

---

## Próximos pasos prioritarios

1. **Canal premium Telegram + Stripe**
   - Comando `/premium` en bot → Stripe Checkout link
   - Webhook Stripe → añadir/quitar usuario de canal privado
   - Publicar en canal privado sin delay cuando `tipo == ARBITRAJE`

2. **Resolver PSS y El Corte Inglés**
   - PSS: pide feed XML/CSV a su equipo de afiliados, o usar Awin feed
   - ECI: usar feed Awin cuando se apruebe el merchant ID

3. **Más fuentes TD — feeds disponibles con precio original**
   - HP Store (fid=38866, 964 productos) — campo precio original a verificar
   - Esdemarca (fid=116972, 107k productos) — moda/deportes de marca
   - Quiksilver/Roxy/DC Shoes/Element (feeds TD disponibles) — calzado y moda deportiva
   - ToysRus: resolver precio original (pedir feed con `strike_price` al merchant)

4. **Más fuentes de deals (Playwright)**
   - Añadir tiendas: Sprinter, Zalando Outlet, Garmin Store

4. **Supabase** (si se necesita escalar SQLite)
   - Tablas: deals, clicks, users, subscriptions

---

## scrapers/tradedoubler_feed.py — Feeds de producto Tradedoubler

Módulo independiente (sin imports de `flipazo_main` — evita import circular). Se llama desde `scrape_todas_las_tiendas` vía `asyncio.to_thread`.

### Feeds activos

| Tienda | fid | Productos | Campo precio original | Deals/día ≥37% |
|---|---|---|---|---|
| MediaMarkt | 24915 | ~17k | `strike_price` | ~544 |
| PCBox | 50247 | ~24k | `PreviousPrice` | ~11/día ≥35% |
| Beep | 51903 | ~23k | `PreviousPrice` (MSRP) | ❌ desactivado — falsos descuentos |
| ToysRus | 21529 | ~32k | ❌ no existe | ❌ descuento incalculable |

### Estructura del offer en el JSON

```python
offer = product["offers"][0]
precio_actual   = offer["priceHistory"][0]["price"]["value"]  # string "99.99"
product_url     = offer["productUrl"]   # ya es deep link TD con tracking
availability    = offer["availability"] # "in stock" | "pre order"
```

### Precio original según tienda

```python
fields = product.get("fields", {})
# MediaMarkt: campo "strike_price"
# Beep:       campo "PreviousPrice"
# Código: _get_field(fields, "strike_price") or _get_field(fields, "PreviousPrice")
```

### Caché y automatización

- **Caché 23h en memoria** (`_cache`, `_last_fetch`) — descarga 1 vez/día
- Primera ejecución del día: ~2-3 min descargando feeds (~40k productos)
- Ciclos siguientes: retorna caché instantáneamente
- No requiere cron adicional — el ciclo completo cada 120 min lo gestiona

### Sin verificación CCC

Los productos TD no tienen ASIN → no se puede verificar contra CamelCamelCamel. El `strike_price`/`PreviousPrice` de retailers establecidos (MediaMarkt, Beep) está regulado por Directiva EU 2011/83 (precio más bajo de los últimos 30 días). Validación de calidad delegada al scoring de Claude Haiku.

---

## Convenciones de código

- **Lenguaje:** Python 3.11, f-strings, type hints
- **Logs:** emojis de prefijo → ✅ éxito, ❌ error, ⚠️ advertencia, 🔍 búsqueda, 📤 publicación, 📡 scraping
- **Variables/funciones:** inglés. Comentarios y logs: español
- **Env vars:** siempre `os.getenv()`, nunca hardcodeadas
- **Nuevo scraper:** seguir patrón `_scrape_tienda_generica()` o el de `scrape_mediamarkt()`
- **Nueva tienda en afiliados:** añadir en `affiliate/link_builder.py` + var en `.env`
- **Deploy preferido:** `git commit + push` → auto-deploy en ≤10 min vía cron en servidor
- **Deploy inmediato:** `scp` a `/home/flipazo/app/` + `systemctl restart flipazo.service`
- **Frontend:** editar `index.html` → `git add index.html && git commit && git push` → Vercel auto-despliega

---

## Auto-deploy y monitorización automática

### Pipeline de deploy automático

```
git push origin main
      ↓
  [cron cada 10 min en servidor]
  /home/flipazo/app/auto-deploy.sh
      ↓
  git fetch origin main
  ¿commits nuevos?
    NO → exit (silencioso)
    SÍ → git pull
         ├─ flipazo_main.py / affiliate/ / scrapers/ → restart flipazo.service
         └─ api.py / analytics/                      → restart flipazo-analytics.service
  log → /home/flipazo/app/deploy.log
```

### Scheduled trigger — auditoría diaria 7am

- **ID:** `trig_01Um43n8top2mkvYsiFqzVpM`
- **Horario:** 7:00am Madrid (5am UTC) — todos los días
- **Qué hace:** clona el repo, ejecuta 7 checks estáticos en `flipazo_main.py`, aplica fixes si los encuentra, hace commit+push → el auto-deploy lo recoge en ≤10 min
- **Gestión:** https://claude.ai/code/scheduled/trig_01Um43n8top2mkvYsiFqzVpM

### Skills disponibles

| Skill | Ubicación | Uso |
|---|---|---|
| `scraper-monitor` | `.claude/skills/scraper-monitor/` | Diagnóstico y reparación del pipeline. Invocar con `/scraper-monitor` |
| `ui-ux-pro-max` | `.claude/skills/ui-ux-pro-max/` | Generación de sistemas de diseño UI/UX |

---

## Errores conocidos y soluciones

| Error | Causa | Solución |
|---|---|---|
| 0 productos en MediaMarkt | Category IDs caducados | Usar búsquedas `?sort=discountPercentage_desc` |
| 0 productos en MediaMarkt (v2) | Cookie consent bloqueaba carga | Loop de accept cookies (6 selectores) + timeout 20s + selector dual `/es/product/` |
| 0 productos en PcComponentes | Regex buscaba `-p\d+\.html` | Filtrar slugs con ≥4 guiones |
| 0 productos en PcComponentes (v2) | React SPA no hidratada al evaluar DOM | `wait_for_load_state("networkidle")` + reducir filtro a ≥2 guiones |
| Amazon solo produce una categoría | URLs sin filtro de descuento — sort por popularidad devuelve items sin oferta real | Añadir `rh=p_n_pct-off-with-tax%3A2388626011` a todas las URLs de búsqueda |
| Deal republicado al día siguiente | TTL dedup era 24h | Cambiado a 96h |
| Imagen NULL en BD | Amazon lazy loading (base64 placeholder) | Saltar `data:` en src, usar `data-src`/`srcset` |
| ECI siempre "Access Denied" | Cloudflare | Circuit breaker 60min, se reintenta sólo |
| PSS páginas de 2381 chars | Cloudflare | Circuit breaker, pendiente solución |
| Wallapop precio muy alto | Claude estimaba cerca de precio Amazon | Prompt actualizado: marcas premium 60-70%, chinas 40-55% |
| Código no se ejecuta en servidor | scp a `/root/flipazo-deploy/` (incorrecto) | SIEMPRE usar `/home/flipazo/app/` |
| Cafetera bloqueada por "café" | PALABRAS_PROHIBIDAS usaba `"café"` como subcadena | Reemplazar por frases específicas: `"café en grano"`, `"café molido"`, etc. |
| Descuento falso Rowenta/infladoPrecio | Amazon sube ref. de €31→€37, actual €28 parece -40% | `_scrape_ccc` devuelve avg histórico; si `precio_original > avg×1.25` → recalcular o descartar |
| Ropa con tallas S/M/L publicada | Sin filtro de tallas | `_TALLA_RE` filtra tallas de letra; tallas numéricas (zapatos) se permiten |
| TD feeds devuelven 0 deals | Precio actual estaba en `priceHistory[0].price.value` no en `price.value` | Corregido en `tradedoubler_feed.py` — usar `offer["priceHistory"][0]["price"]["value"]` |
| Beep: 0 deals pese a tener descuentos | Campo precio original es `PreviousPrice`, no `strike_price` | `_get_field` busca `strike_price` OR `PreviousPrice` como fallback |
| ToysRus: siempre 0 deals | Feed sin campo de precio original | Retirado de `_FEEDS` — no hay forma de calcular descuento real |
| TD_PUBLISHER_ID incorrecto | Confusión publisher ID (2468812) vs site ID (3481714) | `TD_PUBLISHER_ID` debe ser el site ID 3481714 — es el parámetro `&a=` del deep link |
