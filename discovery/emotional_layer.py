"""
discovery/emotional_layer.py — Generación de hooks emocionales con Claude Haiku.

Una sola llamada por batch de hasta BATCH deals (cost-efficient).
Llamada async. Modifica los `Producto` in place añadiendo `hook` y `social_context`.

Diseño cost-aware:
  - Haiku 4.5 (modelo barato)
  - 15 deals por llamada → 1 call cubre un ciclo típico
  - System prompt corto (no aplica caching, < 1024 tokens)
  - Output JSON sin verbosidad — solo {id, hook, social}
  - Error-safe: cualquier fallo deja los campos vacíos y sigue
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flipazo_main import Producto


HAIKU_MODEL = "claude-haiku-4-5-20251001"
BATCH       = 15
MAX_TOKENS  = 1800

_SYSTEM_PROMPT = """Eres editor de Flipazo, un canal español de descubrimiento de deals.
Tu estilo es como Reddit o TikTok descubriendo algo cool: natural, directo, sin marketing barato.

Para cada producto que recibas, devuelves un objeto JSON con:

- "id": entero, el id que te paso
- "hook": titular en español max 80 caracteres. Captura POR QUÉ vale la pena mirarlo.
  PROHIBIDO empezar con: "Increíble", "Oferta", "Brutal", "Descuento", "Top", "Chollo".
  No repitas el nombre completo del producto. Sugiere VALOR, COMPARACIÓN o CONTEXTO.
  Ejemplos válidos:
  • "Cubre 4 habitaciones mejor que muchos routers de 300€"
  • "El Dyson Supersonic, ahora a precio de secador normal"
  • "Bose con noise cancelling sin pagar AirPods Max"
  • "Lo recomiendan en cada hilo de Reddit de WiFi"
  • "Un robot aspirador con mapeo láser a precio de Cecotec"

- "social": frase contextual max 55 caracteres. Por qué importa AHORA, no qué es.
  Ejemplos válidos:
  • "Este precio no suele durar"
  • "Volvió a bajar tras semanas"
  • "El precio más bajo en meses"
  • "Lo más guardado de la semana"
  • "Internet está obsesionado con este"
  • "Para los que dudaron la última vez"

DEVUELVE SOLO un array JSON válido, sin markdown ni texto extra:
[{"id":0,"hook":"...","social":"..."},{"id":1,"hook":"...","social":"..."}]"""


def _truncar(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0]


async def _llamar_haiku(batch: list["Producto"]) -> dict[int, dict]:
    """Llama Haiku con un batch y devuelve {idx: {hook, social}}."""
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        print("⚠️  anthropic SDK no instalado — saltando emotional_layer")
        return {}

    payload = [
        {
            "id":           i,
            "titulo":       _truncar(p.titulo, 80),
            "tienda":       p.tienda,
            "precio":       round(p.precio_actual or 0, 2),
            "precio_orig":  round(p.precio_original or 0, 2),
            "desc_pct":     p.descuento_pct or 0,
            "tipo":         p.tipo,
        }
        for i, p in enumerate(batch)
    ]

    client = AsyncAnthropic()
    resp = await client.messages.create(
        model      = HAIKU_MODEL,
        max_tokens = MAX_TOKENS,
        system     = _SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )

    text = resp.content[0].text.strip() if resp.content else ""

    # Sanitizar fences ```json o ``` por si los emite
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

    parsed = json.loads(text)
    if not isinstance(parsed, list):
        return {}

    out: dict[int, dict] = {}
    for item in parsed:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            out[item["id"]] = {
                "hook":   str(item.get("hook")   or "")[:120],
                "social": str(item.get("social") or "")[:80],
            }
    return out


async def generar_hooks_batch(deals: list["Producto"]) -> int:
    """
    Enriquece cada deal con `hook` y `social_context` (in place).
    Devuelve el número de deals efectivamente enriquecidos.

    Falla silenciosamente: si la API no está configurada o cae, deja los
    campos vacíos y los callers usan el título como fallback en el frontend.
    """
    if not deals:
        return 0

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("⚠️  ANTHROPIC_API_KEY no configurada — saltando emotional_layer")
        return 0

    enriquecidos = 0
    for i in range(0, len(deals), BATCH):
        chunk = deals[i:i + BATCH]
        try:
            result = await _llamar_haiku(chunk)
            for idx, item in result.items():
                if 0 <= idx < len(chunk):
                    chunk[idx].hook           = item["hook"]
                    chunk[idx].social_context = item["social"]
                    enriquecidos += 1
        except Exception as e:
            print(f"⚠️  emotional_layer batch {i//BATCH + 1}: {e}")

    if enriquecidos:
        print(f"✨ Hooks generados: {enriquecidos}/{len(deals)} deals")
    return enriquecidos
