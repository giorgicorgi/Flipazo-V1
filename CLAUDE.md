# Flipazo — Contexto del Proyecto para Claude Code

> Este archivo se carga automáticamente al abrir el proyecto.
> Refleja el estado REAL y actual del código, no aspiracional.

---

## Qué es Flipazo

Canal de deals automatizado para España. Encuentra ofertas con descuento ≥35% sobre precio histórico, las filtra con IA y las publica en Telegram y web propia (flipazo.es).

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

### Servicios systemd

```
flipazo.service           → pipeline principal (flipazo_main.py)
flipazo-analytics.service → servidor analytics (uvicorn analytics.tracker:app --port 8080)
```

### Comandos de operación

```bash
# Deploy de código (SIEMPRE a /home/flipazo/app/, NO a /root/)
scp flipazo_main.py root@204.168.199.253:/home/flipazo/app/flipazo_main.py
scp affiliate/link_builder.py root@204.168.199.253:/home/flipazo/app/affiliate/link_builder.py

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
├── affiliate/
│   └── link_builder.py                 ← URLs de afiliado por tienda (Amazon/Awin)
├── analytics/
│   └── tracker.py                      ← FastAPI analytics: /r/{id} + /stats
├── scrapers/
│   └── pss_email.py                    ← Lector Gmail IMAP para eventos PSS
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
DESCUENTO_MINIMO        = 35     # % mínimo para cualquier deal
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
```

### Tiendas scrapeadas

| Tienda | URLs fuente | Estado |
|---|---|---|
| Amazon | 16 categorías + /deals | ✅ Funcional |
| MediaMarkt | 6 búsquedas por descuento | ✅ (wait_for_selector 12s) |
| PcComponentes | 4 URLs campañas | ✅ (regex slugs corregida) |
| Decathlon | 7 categorías | ✅ |
| Worten | 5 secciones | ✅ |
| El Corte Inglés | 10 secciones | ⚠️ Bloqueada frecuentemente (circuit breaker 60min) |
| Mammoth Bikes | 6 outlet pages | ✅ (precios ES: `1.234,56 €`) |
| Private Sport Shop | Via Gmail IMAP + Playwright | ⚠️ Bloqueada — circuit breaker activo |

### Categorías Amazon (AMAZON_SEARCH_URLS)

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
    → scrape_mediamarkt()
    → scrape_pccomponentes()
    → scrape_decathlon()
    → scrape_worten()
    → scrape_elcorteingles()
    → scrape_mammoth()
    → scrape_privatesportshop()  ← extrae URLs de Gmail, luego Playwright
  → verificar_historial_ccc()   ← CamelCamelCamel para precio histórico
  → score_con_claude()          ← Haiku: pre-scorer local + zona gris
  → obtener_precio_wallapop()   ← solo deals tipo ARBITRAJE
  → publicar en Telegram
  → DeduplicacionDB.marcar_publicado()
```

### Scoring (score_con_claude)

**Etapa 1 — Pre-scorer local (sin IA):**
- Descuento ≥65% → +40 pts; ≥55% → +30; ≥45% → +20; ≥35% → +10
- Marca conocida → +30 pts (whitelist extensa: Apple, Sony, Nike, Braun, Xiaomi, Lefant, etc.)
- Precio 30-400€ → +15 pts; 400-600€ → +8 pts
- Historial CCC: precio en mínimo histórico → +15; penaliza si inflado

**Etapa 2 — Claude Haiku (solo zona gris 22-69 pts):**
- Prompt `PROMPT_SCORING` → JSON con `score_reventa`, `score_oferta`, `precio_wallapop_estimado`
- `tipo`: `"ARBITRAJE"` | `"OFERTA"` | `"DESCARTAR"`
- Wallapop: estimaciones conservadoras: marcas premium 60-70% del precio Amazon; marcas chinas/desconocidas 40-55%

### Deduplicación (SQLite)

- Tabla `deals_publicados` — PRIMARY KEY: `deal_id` (MD5 de `tienda:asin_o_titulo`)
- `INSERT OR REPLACE` — actualiza el deal si ya existía
- TTL: 96h. `ya_publicado()` devuelve True si publicado en las últimas 96h

### Marcas conocidas (whitelist _MARCAS_CONOCIDAS)

Apple, Samsung, Sony, LG, Philips, Bosch, Dyson, Nike, Adidas, New Balance, Jordan, Asics, Puma, Reebok, LEGO, Nintendo, PlayStation, Xbox, Bose, AirPods, Jabra, Sennheiser, Nespresso, DeLonghi, Tefal, Rowenta, Braun, Siemens, Oral-B, Remington, Wahl, Panasonic, Shark, Bissell, Kärcher, Kenwood, Dior, Chanel, Armani, Calvin Klein, Lacoste, North Face, Roborock, Roomba, iRobot, Lefant, Dreame, Ecovacs, Eufy, Cecotec, Xiaomi, Redmi, Kindle, GoPro, Garmin, Fitbit, Polar, Makita, DeWalt, Milwaukee, Stanley, Canon, Nikon, HP, Dell, Lenovo, Asus, Acer, Microsoft, Logitech, Razer, G-Shock, Casio, Seiko, Citizen

### Palabras prohibidas (PALABRAS_PROHIBIDAS)

Excluye accesorios baratos, alimentación, champús, ropa básica (vaqueros, camisetas, etc.), zapatillas gama baja (Tanjun, Revolution, etc.), multipacks genéricos.

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

Frontend estilo periódico. Variables clave en el JS:
```javascript
const USE_MOCK = false;                        // false = datos reales de la API
const API_URL  = "https://flipazo.es/api/deals";
```

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
"Amazon"          → https://www.amazon.es/dp/{asin}?tag=flipazo-21
"MediaMarkt"      → Awin deep link (MEDIAMARKT_AWIN_MID=6907)
"PcComponentes"   → Awin deep link (PCCOMPONENTES_AWIN_MID — pendiente)
"ElCorteIngles"   → Awin deep link (ELCORTEINGLES_AWIN_MID — pendiente)
"PrivateSportShop"→ Awin deep link (PRIVATESPORTSHOP_AWIN_MID — pendiente)
"Mammoth Bikes"   → Awin deep link si MAMMOTH_AWIN_MID configurado; si no, URL directa
cualquier otra    → URL directa (sin perder el deal)
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
MEDIAMARKT_AWIN_MID=6907
PCCOMPONENTES_AWIN_MID=            # pendiente aprobación
ELCORTEINGLES_AWIN_MID=            # pendiente aprobación
MAMMOTH_AWIN_MID=                  # pendiente solicitar

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
| Scraper Amazon | ✅ Funcional | 16 categorías + /deals |
| Scraper MediaMarkt | ✅ Funcional | URLs de búsqueda por descuento |
| Scraper PcComponentes | ✅ Funcional | Regex corregida (slugs) |
| Scraper Decathlon | ✅ Funcional | |
| Scraper Worten | ✅ Funcional | |
| Scraper El Corte Inglés | ⚠️ Inestable | Cloudflare — circuit breaker 60min |
| Scraper Mammoth Bikes | ✅ Funcional | 6 outlet pages, precios ES format |
| Scraper PSS | ⚠️ Bloqueado | Cloudflare duro (2381 chars) |
| Scoring Claude Haiku | ✅ Funcional | Pre-scorer local + zona gris |
| Análisis Wallapop | ✅ Funcional | Solo deals ARBITRAJE |
| Publisher Telegram | ✅ Funcional | Canal público solamente |
| Deduplicación SQLite | ✅ Funcional | TTL 96h (4 días) |
| API FastAPI | ✅ Funcional | /api/deals + /r/{id} |
| Frontend Vercel | ✅ Funcional | index.html, auto-deploy desde GitHub |
| Analytics tracker | ✅ Funcional | Puerto 8080, clicks en SQLite |
| Afiliados Amazon | ✅ Activo | tag flipazo-21 |
| Afiliados Awin | ⚠️ Parcial | Solo MediaMarkt activo |
| Canal premium Telegram | 🔲 Pendiente | |
| Bot Telegram + Stripe | 🔲 Pendiente | |
| WhatsApp publisher | 🔲 Pendiente | |
| Threads publisher | 🔲 Pendiente | |

---

## Próximos pasos prioritarios

1. **Canal premium Telegram + Stripe** (deadline ~15 abril)
   - Comando `/premium` en bot → Stripe Checkout link
   - Webhook Stripe → añadir/quitar usuario de canal privado
   - Publicar en canal privado sin delay cuando `tipo == ARBITRAJE`

2. **Resolver PSS y El Corte Inglés**
   - PSS: pide feed XML/CSV a su equipo de afiliados, o usar Awin feed
   - ECI: intentar con sesión de usuario o feed Awin

3. **Más fuentes de deals**
   - Añadir tiendas: Sprinter, Zalando Outlet, Running Room, Garmin Store

4. **Supabase** (si se necesita escalar SQLite)
   - Tablas: deals, clicks, users, subscriptions

---

## Convenciones de código

- **Lenguaje:** Python 3.11, f-strings, type hints
- **Logs:** emojis de prefijo → ✅ éxito, ❌ error, ⚠️ advertencia, 🔍 búsqueda, 📤 publicación, 📡 scraping
- **Variables/funciones:** inglés. Comentarios y logs: español
- **Env vars:** siempre `os.getenv()`, nunca hardcodeadas
- **Nuevo scraper:** seguir patrón `_scrape_tienda_generica()` o el de `scrape_mediamarkt()`
- **Nueva tienda en afiliados:** añadir en `affiliate/link_builder.py` + var en `.env`
- **Deploy:** `scp` a `/home/flipazo/app/` + `systemctl restart flipazo.service`
- **Frontend:** editar `index.html` → `git add index.html && git commit && git push` → Vercel auto-despliega

---

## Errores conocidos y soluciones

| Error | Causa | Solución |
|---|---|---|
| 0 productos en MediaMarkt | Category IDs caducados | Usar búsquedas `?sort=discountPercentage_desc` |
| 0 productos en PcComponentes | Regex buscaba `-p\d+\.html` | Filtrar slugs con ≥4 guiones |
| Deal republicado al día siguiente | TTL dedup era 24h | Cambiado a 96h |
| Imagen NULL en BD | Amazon lazy loading (base64 placeholder) | Saltar `data:` en src, usar `data-src`/`srcset` |
| ECI siempre "Access Denied" | Cloudflare | Circuit breaker 60min, se reintenta sólo |
| PSS páginas de 2381 chars | Cloudflare | Circuit breaker, pendiente solución |
| Wallapop precio muy alto | Claude estimaba cerca de precio Amazon | Prompt actualizado: marcas premium 60-70%, chinas 40-55% |
| Código no se ejecuta en servidor | scp a `/root/flipazo-deploy/` (incorrecto) | SIEMPRE usar `/home/flipazo/app/` |
