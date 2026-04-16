---
name: scraper-monitor
description: "Daily automated health check for the Flipazo scraper pipeline. Runs every day at 7am: reads server logs, detects broken scrapers (0 products, circuit breakers, JSON errors, dedup loops), diagnoses root causes by reading flipazo_main.py, applies targeted fixes, deploys automatically, and verifies the fix. Use this skill when asked to check scraper health, diagnose why a store isn't producing deals, or run the daily pipeline audit."
---

# Scraper Monitor — Flipazo Daily Health Agent

Automated daily audit of the Flipazo deal-hunting pipeline running on Hetzner VPS `204.168.199.253`. This agent checks logs, detects failures, reads code, applies fixes, deploys, and verifies — without requiring manual intervention.

---

## Infrastructure Reference

| Item | Value |
|---|---|
| Server | `root@204.168.199.253` |
| App directory | `/home/flipazo/app/` |
| Python venv | `/home/flipazo/app/venv/bin/python` |
| Service | `flipazo.service` |
| Logs | `journalctl -u flipazo.service` |
| Local source | `/Users/jorgeu/Desktop/Flipazo/` |
| DB | `/home/flipazo/app/flipazo_deals.db` |

---

## Step 1 — Pull logs from the last 24 hours

```bash
ssh root@204.168.199.253 "journalctl -u flipazo.service --no-pager --since '24 hours ago'"
```

Save the output mentally. You will parse it for the signals below.

---

## Step 2 — Run diagnostics on each signal

Check every signal in this table. Mark each as ✅ OK, ⚠️ Warning, or ❌ Critical.

### Signal checklist

| Signal | How to detect in logs | Severity |
|---|---|---|
| **Amazon yield** | Count `⚡ OFERTA Amazon` lines per cycle. If <3 across all cycles → investigate | ⚠️ |
| **MediaMarkt yield** | `✅ N ofertas de MediaMarkt` — if N=0 for 2+ cycles → broken | ❌ |
| **PcComponentes yield** | `✅ N ofertas de PcComponentes` — if N=0 for 2+ cycles → broken | ❌ |
| **Decathlon yield** | `✅ N ofertas de Decathlon` — if N=0 → broken | ❌ |
| **Worten yield** | `✅ N ofertas de Worten` — if N=0 → broken | ❌ |
| **Mammoth yield** | `✅ N ofertas de Mammoth Bikes` — if N=0 → broken | ❌ |
| **ECI circuit breaker** | `[ElCorteIngles] Circuit breaker activo` — normal, expected | ✅ |
| **PSS circuit breaker** | `[PSS] circuit breaker` or 2381-char pages — normal | ✅ |
| **Claude JSON error** | `Error parseando respuesta Claude` — scoring broken | ❌ |
| **All dedup** | `N deal(s) ya publicados` + `Publicando 0 deals` every cycle — expected if <96h | ⚠️ |
| **CCC false positives** | `❌ Descuento falso` on >50% of products → ratio too strict | ⚠️ |
| **Service crash** | `flipazo.service: Main process exited` or no log for >3h | ❌ |
| **Telegram failures** | `Error enviando Telegram` → check bot token or chat ID | ❌ |

---

## Step 3 — Deep-dive per broken scraper

For each scraper marked ❌, run the specific diagnosis below.

### Amazon (0 deals / only one category producing)

1. Check if the `rh=p_n_pct-off-with-tax%3A2388626011` filter is in `AMAZON_SEARCH_URLS` — if missing, add it to all search URLs.
2. Check if `_extraer_de_busqueda` logs show product parsing errors.
3. If the `/deals` page returns "Las ofertas por tiempo limitado" with 0 products extracted — check the `_extraer_de_deals` function for selector changes.

Read the relevant section:
```bash
grep -n "AMAZON_SEARCH_URLS\|_extraer_de_busqueda\|_extraer_de_deals" /Users/jorgeu/Desktop/Flipazo/flipazo_main.py | head -30
```

### MediaMarkt (0 products)

Symptoms and fixes:
- **Cookie banner blocking**: Check that `_aceptar_mediamarkt_cookies` is accepting all 6 selectors.
- **Selector changed**: Product links use `a[href*="/es/product/"]` — verify with screenshot.
- **Timeout too short**: If scraper completes in <5s per URL, page didn't load. Increase `timeout=20000`.

Enable debug screenshot:
```bash
ssh root@204.168.199.253 "DEBUG_SCREENSHOTS=true systemctl restart flipazo.service"
```
Then pull screenshot: `scp root@204.168.199.253:/home/flipazo/app/debug_mediamarkt.png /tmp/`

### PcComponentes (0 products)

Symptoms and fixes:
- **SPA not hydrated**: `wait_for_load_state('networkidle')` must be called after page load.
- **Slug filter too strict**: Minimum guiones filter (`≥2`) — verify in JS evaluate block.
- **URLs changed**: Try navigating to `pccomponentes.com/ofertas-especiales?sort=discount` manually via curl to see if page still exists.

Check the scraper code:
```bash
grep -n "networkidle\|guiones\|≥" /Users/jorgeu/Desktop/Flipazo/flipazo_main.py
```

### Mammoth Bikes (0 products)

- If 0 products scraped (not just 0 published): outlet URL slugs may have changed. Each URL `o-NNNN` is hardcoded — verify they still return products.
- If products found but all deduped (<96h): normal. Add new outlet category URLs.
- Known outlet URL pattern: `https://www.mammothbikes.com/outlet/{category}/o-{id}`

Check DB for last Mammoth deal:
```bash
ssh root@204.168.199.253 "/home/flipazo/app/venv/bin/python -c \"
import sqlite3; con = sqlite3.connect('/home/flipazo/app/flipazo_deals.db')
rows = con.execute(\\\"SELECT titulo, publicado_en FROM deals_publicados WHERE tienda='Mammoth Bikes' ORDER BY publicado_en DESC LIMIT 5\\\").fetchall()
for r in rows: print(r)
\""
```

### Claude JSON parse error

Symptom: `Error parseando respuesta Claude: Expecting ',' delimiter: line N`

Root cause: Claude Haiku returned malformed JSON (truncated or with a special character). 

Fix: Add a JSON repair step in `score_con_claude`. Read the current JSON parsing block:
```bash
grep -n "json.loads\|Error parseando\|PROMPT_SCORING" /Users/jorgeu/Desktop/Flipazo/flipazo_main.py | head -20
```

Apply fix — wrap `json.loads` with a cleanup that strips trailing content after the last `}`:
```python
# Before:
resultado = json.loads(respuesta_texto)

# After:
import re as _re
respuesta_limpia = respuesta_texto[:respuesta_texto.rfind('}')+1] if '}' in respuesta_texto else respuesta_texto
resultado = json.loads(respuesta_limpia)
```

### Service crash / no logs for >3 hours

```bash
ssh root@204.168.199.253 "systemctl status flipazo.service"
ssh root@204.168.199.253 "journalctl -u flipazo.service --no-pager -n 50"
```

If crashed: check for Python import errors, missing env vars, or disk space:
```bash
ssh root@204.168.199.253 "df -h /home && cat /home/flipazo/app/.env | grep -v '^#' | grep '=$'"
```

---

## Step 4 — Apply fixes

For each issue found:

1. **Read the exact broken section** in `/Users/jorgeu/Desktop/Flipazo/flipazo_main.py` using the Read tool (use line numbers from grep output).
2. **Apply a surgical fix** with the Edit tool — change only what's needed.
3. **Do NOT refactor surrounding code** — only fix the root cause.

Common fix patterns:

```python
# Fix: URL filter ID changed → update AMAZON_SEARCH_URLS
# Fix: networkidle missing → add try/except wait_for_load_state block
# Fix: slug filter too strict → reduce guiones threshold from 4→2
# Fix: JSON parse → strip after last }
# Fix: outlet URL dead → find new URL by checking mammothbikes.com/outlet
# Fix: selector changed → update CSS selector string
```

---

## Step 5 — Deploy

After every fix, deploy immediately:

```bash
scp /Users/jorgeu/Desktop/Flipazo/flipazo_main.py root@204.168.199.253:/home/flipazo/app/flipazo_main.py
ssh root@204.168.199.253 "systemctl restart flipazo.service"
```

Confirm startup:
```bash
ssh root@204.168.199.253 "journalctl -u flipazo.service --no-pager -n 10"
```

Expected first lines: `🚀 Flipazo iniciado` followed by `📡 Categoría: electronics ...`

---

## Step 6 — Verify (wait for next cycle)

After restart, the flash cycle runs in ~60 minutes. Check results:

```bash
ssh root@204.168.199.253 "journalctl -u flipazo.service --no-pager -n 100 | grep -E '(✅|❌|⚡|📢|OFERTA|deals nuevos)'"
```

For each previously broken scraper, verify `✅ N ofertas de {Tienda}` now shows N > 0.

---

## Step 7 — Report

Produce a summary with this structure:

```
## Flipazo Scraper Health Report — {DATE}

### Status Summary
| Scraper | Status | Products (last cycle) | Action taken |
|---|---|---|---|
| Amazon | ✅ OK | 12 | None |
| MediaMarkt | ✅ OK | 4 | None |
| PcComponentes | ❌ → ✅ Fixed | 0 → 7 | Added networkidle wait |
| Decathlon | ✅ OK | 3 | None |
| Worten | ✅ OK | 2 | None |
| Mammoth Bikes | ⚠️ Dedup | 0 (38 found, all deduped) | None needed |
| ECI | ⚠️ Blocked | — | Normal (Cloudflare) |
| PSS | ⚠️ Blocked | — | Normal (Cloudflare) |

### Fixes Applied
- [list of code changes + rationale]

### Deals Published (last 24h)
- Total: N
- By store: Amazon N, MediaMarkt N, ...

### Pending Issues
- [any issues that couldn't be auto-fixed]
```

Send the report as an admin Telegram message if `TELEGRAM_ADMIN_CHAT_ID` is set:
```bash
ssh root@204.168.199.253 "/home/flipazo/app/venv/bin/python -c \"
import os, requests
from dotenv import load_dotenv
load_dotenv('/home/flipazo/app/.env')
token = os.getenv('TELEGRAM_TOKEN')
chat  = os.getenv('TELEGRAM_ADMIN_CHAT_ID')
msg   = '''[INSERT REPORT SUMMARY HERE]'''
if token and chat:
    requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'})
\""
```

---

## Key constraints

- **Only fix what is broken** — do not refactor working scrapers.
- **Always read before editing** — use Read tool with exact line numbers from grep.
- **One fix per issue** — do not batch multiple changes into one deploy if they're unrelated.
- **PALABRAS_PROHIBIDAS safety** — use specific phrases, never generic substrings (e.g. `"café en grano"` not `"café"`).
- **After any edit to flipazo_main.py**: always run scp + systemctl restart. Never leave it undeployed.
- **Never change thresholds** (DESCUENTO_MINIMO, RATIO_PRECIO_REF_INFLADO, etc.) without user confirmation — these are business rules.
