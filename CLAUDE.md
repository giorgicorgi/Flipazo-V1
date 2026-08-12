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

- `DESCUENTO_MINIMO = 40` — % mínimo para cualquier deal. **Innegociable: nunca se baja de 40.**
  Única excepción: gran electrodoméstico (≥300€, `_GRAN_ELECTRO_RE`) desde el 30%, y solo porque
  la card lleva la nota *"No llega al −40%, pero en un electrodoméstico así es un buen ahorro"*.
  Subirlo por encima de 40 sí vale (Esdemarca/Desigual/Toni Pons al 50%). Para limitar el VOLUMEN
  de una tienda se usa un tope por ciclo (`_TD_CAP_POR_TIENDA`, `_AWIN_CAP`), **nunca el umbral**:
  confundirlos tuvo a Toni Pons 100 días apagada (le pusieron 60% para que no inundara el canal)
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
| ~~Privé by Zalando~~ | Feed AWIN | ❌ **Retirada 12-ago-2026**: dieron de baja su programa de AWIN. Dejó de aparecer en el feed el 28-jun y nunca publicó un solo deal. Quedan 188.022 filas huérfanas en `price_history` |
| ~~PcComponentes~~ (afiliación) | Programa propio de recomendadores (UTM), **no AWIN** | ⚠️ La afiliación funciona (los 19 deals vivos llevan `utm_source`). Lo que está roto es el **scraping**: Cloudflare, 29 días sin publicar. AWIN nunca les aceptó |
| Carrefour | Feed AWIN (fid 76395,98228 = "Carrefour Supermercado Online") | ⚠️ Marketplace B2B ruidoso → **modo histórico** (`_SOLO_HISTORICO`) + allowlist de consumo (`_CARREFOUR_KEEP`) − blocklist (`_CARREFOUR_SKIP`) en `awin_feed.py`. Publica bajadas reales propias (~2 sem) |
| Foot Locker | Feed AWIN 2 (fid 78257, `AWIN_FEED_URL_2`) | ✅ **Publicable directo**: `product_price_old` real en 8.185/49.742 filas. 48k productos ≥25€, ~628 con ≥40%. Nike/Adidas/Jordan/New Balance/Puma → `calzado`. Repite modelo por talla: `_clave_familia` las agrupa |
| TodoConsolas | Feed AWIN 2 (fid 101515, `AWIN_FEED_URL_2`) | ⚠️ **Modo histórico**: `product_price_old` vacío en las 24.596 filas; `rrp_price` es PVP inflado (mediana −15%, trampa Beep). 6.225 productos ≥25€. Videojuegos/consolas/merch → `tecnologia` |

**TD feeds:** Caché 23h en memoria. `offer["priceHistory"][0]["price"]["value"]` = precio actual. `TD_PUBLISHER_ID` debe ser el SITE ID (3481714), NO el publisher ID (2468812).

**TD vouchers (cupones):** `TRADEDOUBLER_VOUCHER_TOKEN` es el token de tipo **VOUCHERS/SITE** (rango Flipazo 3481714) — el de productos da 403 y el de PUBLISHER es otro distinto. El panel de TD (Gestión de anuncios → Vouchers) es la fuente de verdad para contrastar: la columna Status dice EN VIVO / NO INICIADO y las fechas son las mismas que devuelve la API.

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
| Boletín "Para ti" por correo | ⚠️ Código y cron listos (`scripts/newsletter_para_ti.py`, `0 8 * * *`). Frecuencia a elección: diario/alternos/semanal/días concretos. **Falta el proveedor SMTP**: sale por el Gmail de la verificación, que no sirve para boletines (≈500/día, sin bajas ni rebotes → spam). Configurar `SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS` (Brevo: `smtp-relay.brevo.com:587`) y no hay que tocar código |
| Canal premium Telegram | 🔲 Pendiente |
| Bot Telegram + Stripe | 🔲 Pendiente |
| WhatsApp | ⚠️ Código listo, faltan credenciales WA_TOKEN |

---

## Scheduled trigger y skills

- **Trigger semanal:** `trig_01Um43n8top2mkvYsiFqzVpM` — análisis estático de `flipazo_main.py`. Gestión: https://claude.ai/code/scheduled/trig_01Um43n8top2mkvYsiFqzVpM
- **Skill `scraper-monitor`:** `.claude/skills/scraper-monitor/` — diagnóstico y reparación del pipeline. Invocar con `/scraper-monitor`
- **Skill `ui-ux-pro-max`:** `.claude/skills/ui-ux-pro-max/` — sistemas de diseño UI/UX
- **Agente `tiktok-guionista`:** `.claude/agents/tiktok-guionista.md` — guiones de TikTok/Reels listos para grabar, con estructura Hook · Clímax · Desenlace. Dos formatos: *vibe coding* (pantalla de flipazo.es + recorte del creador contando cómo se construyó con Claude) y *la oferta que me he encontrado*. Saca precios y cifras de la BD de producción: **nada inventado**. No publica, entrega el guion
- **Agente `threads-storyteller`:** `.claude/agents/threads-storyteller.md` — redacta y publica HILOS narrativos (tuiteratura) en Threads @flipazo.es. Universo "el precio real de las cosas". Borrador → aprobación → publica encadenado. Las historias son a mano; los deals los publica el pipeline (`_threads_elegible`, score ≥70)

---

## Procedimiento para añadir una tienda nueva

**Obligatorio hacerlo entero.** Saltarse el paso 1 es lo que dejó a El Corte Inglés
2 semanas sin publicar (ver tabla de errores).

1. **Analizar el feed ANTES de integrarlo** — descargarlo y medir, sin suponer:
   - Qué comercios trae, cuántas filas cada uno y **en qué orden** (los feeds AWIN
     concatenan comercios: uno grande empuja a los de detrás).
   - `product_price_old`: ¿es mayor que `search_price` en una parte significativa?
     ¿o viene vacío / igual al precio? ¿`rrp_price` es PVP inflado (trampa Beep)?
   - Cuántos productos superan los umbrales reales (≥25€, ≥40%): ese es el rendimiento.
   - Marcas y categorías dominantes (¿es catálogo de consumo o material profesional?).
2. **Decidir el modo según el paso 1:**
   - `product_price_old` fiable → `_PUBLICABLE`: se publica directo con el filtro de descuento.
   - Sin precio de referencia usable → `_SOLO_HISTORICO`: se registra el precio diario en
     `price_history` y se publican solo las bajadas que detectemos nosotros (`price_drop.py`).
3. **Feed nuevo = URL nueva** (`AWIN_FEED_URL_2..5` en `.env`), no ampliar la existente:
   fallos aislados y ningún comercio desplaza a otro. La apikey **nunca** va a git.
4. **Cablear:** `_MERCHANT_MAP` + `_PUBLICABLE`/`_SOLO_HISTORICO` en `awin_feed.py`;
   `_TIENDAS_FEED_CONFIABLE` en `flipazo_main.py` si el descuento lo verificamos nosotros.
5. **Web:** añadir la tienda a `_KNOWN_STORES` en `index.html` (si no, solo aparece en el
   filtro cuando casualmente tenga deals en el primer lote) y a `_STORE_LABELS` si el
   nombre del feed no es presentable. Opcional: `_MH_STORES` para el carrusel de Explorar.
6. **Verificar de verdad:** ejecutar el feed, comprobar categoría asignada
   (`_inferir_categoria`), que el enlace de afiliado sea el `aw_deep_link`, que
   `_clave_familia` agrupe variantes (tallas/colores) para no inundar el canal, y que la
   tienda salga en el filtro de la web.

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
| Descuento falso enorme por precio-por-unidad Amazon | Amazon muestra el precio por unidad (`101,88€ / l`, `52,33€/100 ml`, `X€/kg`) junto al precio; el scraper lo tomaba como `precio_original` → deal ISDIN a -75% (real -15%). El check anti-unidad exigía número tras la barra y no cazaba `/ l` (sin cantidad) | `_extraer_precios_busqueda` + `_extraer_de_deals`: regex de precio-por-unidad con cantidad OPCIONAL + más unidades (l/kg/unidad/metro/pieza/lavado/cápsula) + tolerancia RELATIVA 1% (redondeo). Si `precio_original` = precio/unidad → se descarta → desc 0 → rechazado por <40% |
| Descuento enorme en un RECAMBIO (Vela Tribord 5S, Decathlon, 10-ago-2026) | Se aceptó `InitialPrice` del feed como "precio antes" para marcas propias. En los recambios ese campo es el precio del **producto padre**: la vela salió a 399,99€ "antes 2.469,99€" (el velero completo), y sus vecinos —rueda 9,99€, orza 77,99€, mástil 369,99€— heredaban lo mismo. De 11 deals, en 9 nunca habíamos observado ese precio anterior; uno anunciaba 119,99€ "antes 249,99€" llevando 55 días a 49,99€ | `InitialPrice` NO se usa jamás como referencia. Solo vale nuestro histórico (`decathlon_precios`). Contraste obligatorio antes de fiarse de un "precio antes" de feed: ¿lo hemos observado alguna vez? |
| Deal de marketplace con descuento real pero precio malo (Logitech MX Master 2S, Carrefour, 6-ago-2026) | En un marketplace el vendedor fija el precio **y** el "PVP". Estuvo listado a 303-343€ dos semanas y bajó a 179€: `price_drop` detectó un −41% correcto contra su propio histórico… siendo 179€ un 41% **más caro** que en Amazon (127€). El `rrp_price` del feed (258€) también estaba inflado | En `_TIENDAS_MARKETPLACE` la bajada propia NO basta: se exige confirmación de precio en Amazon (`_CROSSCHECK_AMAZON_TIENDAS`) o no se publica. Al convertir a deal de Amazon, NO arrastrar la referencia del marketplace |
| Gmail marcaba `DMARC: FAIL` pese a que DKIM pasaba y alineaba (11-ago-2026) | Había **dos** registros `_dmarc.flipazo.es`: el de GoDaddy (`p=quarantine`) y uno que añadió Brevo (`p=none`). Dos registros = `permerror` → Gmail no autentica nada del dominio. Explicaba también el aviso de Brevo y que el correo cayera en Promociones y no en Spam (con `permerror` no se aplica el `p=quarantine`) | Un solo registro DMARC. **Y comprobarlo desde el servidor, no desde el portátil**: muchas redes interceptan el puerto 53 y responden desde su caché aunque preguntes por IP al autoritativo (se nota por la falta del flag `aa` y el TTL descontado). Ese día una máquina en España veía 2 registros y el servidor 1, preguntando ambas a 97.74.101.32. Vigilante: `scripts/vigilar_dns.py`, cron `15 7 * * *` |
| Ningún correo de verificación de cuenta llegaba nunca | `api.py::_send_email` usaba `SMTP_SSL("smtp.gmail.com", 465)` y **Hetzner bloquea la salida por el 465**. La conexión moría por timeout y el fallo era mudo (solo un `print`) | Puerto **587 + STARTTLS**. Host/puerto/credenciales desde `.env` (`SMTP_HOST/PORT/USER/PASS/MAIL_FROM`). Comprobar con `/dev/tcp` antes de dar por bueno un SMTP: 465 bloqueado, 587 abierto |
| Los cupones de AWIN (los únicos con código) no salían nunca | `awin_promotions.py` aplica un tope de 6 promos por tienda **en orden de llegada**, y AWIN devuelve los cupones AL FINAL de cada anunciante. Con 19 promos de Voghion, las 6 primeras (sin código) llenaban el cupo y sus 4 cupones se descartaban siempre | Ordenar por "tiene código" ANTES de aplicar el tope. **Dato clave de la API**: con `membership: "notJoined"` (camelCase; `notjoined` da 400) se ven las promos de programas no unidos pero **el código viene siempre a `null`** — 0 de 1.000. Para publicar un cupón hay que estar unido al programa |
| "Precio antes" inventado por un pico del feed (Mesa de jardín Saba, Brico Depot, 8-ago-2026) | El precio de referencia era "el más alto sostenido ≥3 días" en la ventana. Bastaba con que un valor erróneo aguantara 3 días para coronarse: la mesa salió a 29,95€ "antes 189€" (−84%) cuando llevaba 14 días a 35€ y los 189€ iban y venían a rachas | La referencia debe ser el precio **dominante**: ≥40% de los días observados (`PRICE_DROP_REF_CUOTA`) además de sostenido. Y se descartan las series en **yoyó** (el precio bajo ya existía antes de que la referencia dejara de estar vigente) |
| Deal con descuento correcto al publicarlo que meses después miente | Un descuento contra el histórico **caduca solo**: si el producto lleva 27 días al precio de oferta, ese ES su precio y la referencia vieja se sale de la ventana. El verificador solo miraba si el precio subía, nunca si la referencia seguía siendo cierta → 38 deals vivos de ECI/Adidas/Deporte Outlet anunciando "−50%" sobre precios de hace un mes | `hist_pid` guarda la clave del producto en `price_history` al publicar, y `revalidar_publicados()` (price_drop.py) caduca cada día lo que ya no se sostiene. Corre solo cuando el feed AWIN se refresca de verdad (`ultimo_fetch_cacheado`), no cada ciclo |
| Cupón publicado antes de poder usarse ("NO INICIADO" en el panel de TD, 12-ago-2026) | Los dos fetchers de cupones descartaban los caducados pero **no miraban `startDate`**. Tres cupones estaban en la web y en Telegram días antes de existir: GARMIN15JULIOMM (empezaba el 14), Resuinsa −20% (el 17), Flash SALES de Desigual (el 21). Un código que aún no vale es peor que no tener cupón | Filtro de inicio en `tradedoubler_vouchers.py` y `awin_promotions.py`, y en el envío a Telegram. **`/api/promociones` filtra por FECHA, no por el `estado` guardado**: así el cupón del día 16 se activa solo, sin depender de que el pipeline lo recapture. La tarjeta muestra la vigencia; ojo: las redes cierran a medianoche y mandan el fin como las **00:00 del día siguiente** → restar 1 s antes de formatear o se anuncia un día de más |
| La misma oferta dos veces en desktop: hero grande + "Deal del momento" (11-ago-2026) | La home de Explorar pintaba el whero dando por hecho que el hero de arriba estaba oculto. Lo está solo al entrar: cualquier `updateHero()` posterior —`pollNew` cada 5 min, `loadMore`, `renderGrid`— lo resucita, y desde ahí se duplica | Un destacado por viewport: en desktop el hero (`updateHero()` al final de `renderMobileHome`), en móvil el whero (allí no hay hero). Los sitios que ocultaban el hero al entrar en Explorar ya no lo hacen |
| Una tienda del feed AWIN deja de dar deals de golpe (le pasó a ECI el 20-jul-2026) | El feed (1,9 M filas) se parseaba **directo del socket** y el bucle hace trabajo por fila → consumidor lento → AWIN cortaba la conexión (`ProtocolError`) a las 150-260k filas. Inofensivo mientras la tienda estuviera al principio, **hasta que entró Carrefour (864k filas) y empujó a ECI a la fila 1.024.428** | Descargar el `.gz` entero a disco y parsear después (`awin_feed.py`, ~98 MB / 60 s). ⚠️ **Al añadir un comercio grande al feed, comprobar el orden**: `MIN/MAX(fecha)` por tienda en `price_history` delata al instante qué tienda dejó de registrarse |
