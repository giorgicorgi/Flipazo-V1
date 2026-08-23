"""
analytics/bots.py — ¿este clic lo hizo una persona?

Por qué existe (23-ago-2026): el contador de conversión de /mis-links daba un
número sin sentido. De los 265 "visitantes" de /go/threads, **207 eran el
crawler de Meta** que previsualiza el enlace cada vez que se renderiza el perfil
en Threads. El denominador estaba inflado 4,6 veces y nadie lo veía, porque la
tabla `clicks` solo guardaba IP y fecha.

Dos señales, porque ninguna basta sola:

  · **User-Agent** — fiable y estable, pero solo existe en los clics registrados
    a partir de hoy (la columna es nueva).
  · **Rango de IP** — funciona hacia atrás sobre el histórico, pero Meta cambia
    sus rangos y hay que revisarlos de vez en cuando.

Se usa en el registro (para marcar) y en la lectura (para descartar).
"""

from __future__ import annotations

import ipaddress

# Rangos publicados de Meta/Facebook. Es el crawler que previsualiza enlaces en
# Threads/Instagram/WhatsApp: golpea el enlace de bio sin que nadie haya clicado.
_META_CIDR = (
    "69.63.176.0/20", "173.252.64.0/18", "31.13.24.0/21", "66.220.144.0/20",
    "69.171.224.0/19", "74.119.76.0/22", "103.4.96.0/22", "129.134.0.0/16",
    "157.240.0.0/16", "204.15.20.0/22", "179.60.192.0/22", "185.60.216.0/22",
    "45.64.40.0/22", "102.132.96.0/20", "163.114.128.0/17", "199.201.64.0/22",
)

_REDES_BOT: list = []
for _c in _META_CIDR:
    try:
        _REDES_BOT.append(ipaddress.ip_network(_c))
    except ValueError:
        pass

# Fragmentos de User-Agent. En minúsculas; basta con que aparezca uno.
# ⚠️ Nada de subcadenas genéricas sueltas — la misma lección que "café" cazando
# cafeteras y "protector" cazando protector solar. «bingpreview» sí, «preview» no.
_UA_BOT = (
    "facebookexternalhit", "meta-externalagent", "facebookcatalog",
    "bot", "crawler", "spider", "slurp", "bingpreview",
    "whatsapp", "telegrambot", "twitterbot", "discordbot", "applebot",
    "embedly", "quora link preview", "outbrain", "pinterest", "vkshare",
    "python-requests", "python-urllib", "curl/", "wget/", "go-http-client",
    "okhttp", "axios/", "node-fetch", "scrapy", "httpx", "aiohttp",
    "headlesschrome", "phantomjs", "puppeteer", "playwright",
    "semrush", "ahrefs", "mj12", "dotbot", "petalbot", "bytespider",
    "gptbot", "claudebot", "perplexity", "ccbot", "dataforseo", "monitoring",
)


def motivo_bot(ip: str | None, user_agent: str | None = None) -> str | None:
    """Devuelve por qué se considera bot, o None si parece una persona.

    El motivo se devuelve en texto para poder auditar el filtro: si algún día
    descarta de más, se ve exactamente qué regla lo hizo.
    """
    ua = (user_agent or "").lower().strip()
    if ua:
        for frag in _UA_BOT:
            if frag in ua:
                return f"user-agent contiene «{frag}»"

    if ip:
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            return "IP malformada"
        if addr.is_loopback:
            return "localhost (el propio servidor)"
        if addr.is_private or addr.is_link_local or addr.is_reserved:
            return "IP no pública"
        for red in _REDES_BOT:
            if addr in red:
                return f"rango de Meta ({red})"
    return None


def es_bot(ip: str | None, user_agent: str | None = None) -> bool:
    """True si el clic no lo hizo una persona."""
    return motivo_bot(ip, user_agent) is not None
