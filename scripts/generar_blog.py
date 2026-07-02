#!/usr/bin/env python3
"""
scripts/generar_blog.py — Prerrenderiza artículos del blog a HTML estático.

Los artículos del blog se sirven en Vercel como ficheros estáticos en
`blog/<slug>.html` (NO por el rewrite de blog.html, que es un fallback poco
fiable con cleanUrls). Cada post de `posts.json` necesita su fichero prerender.

Este script genera `blog/<slug>.html` para los posts que AÚN NO tienen fichero
(los existentes están hechos a mano con FAQ/tablas y no se sobrescriben).

Uso:
    python3 scripts/generar_blog.py           # genera solo los que faltan
    python3 scripts/generar_blog.py --force    # regenera todos (¡pisa los hechos a mano!)
"""
import html as _html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTS = os.path.join(ROOT, "posts.json")
BLOG_DIR = os.path.join(ROOT, "blog")
BASE = "https://www.flipazo.es"


# ── Markdown mínimo → HTML (subconjunto usado en los posts) ────────────────
def _inline(t: str) -> str:
    t = _html.escape(t, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    return t


def _md_to_html(md: str) -> str:
    out, i = [], 0
    blocks = re.split(r"\n\s*\n", md.strip())
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        lines = b.split("\n")
        if b.startswith("### "):
            out.append(f"<h3>{_inline(b[4:].strip())}</h3>")
        elif b.startswith("## "):
            out.append(f"<h2>{_inline(b[3:].strip())}</h2>")
        elif b.startswith("> "):
            txt = " ".join(l[2:].strip() if l.startswith("> ") else l.strip().lstrip('>').strip() for l in lines)
            out.append(f'<div class="dato-destacado">{_inline(txt)}</div>')
        elif all(l.strip().startswith(("- ", "* ")) for l in lines):
            items = "".join(f"<li>{_inline(l.strip()[2:].strip())}</li>" for l in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{_inline(' '.join(l.strip() for l in lines))}</p>")
    return "\n        ".join(out)


def _readtime(md: str) -> int:
    return max(2, round(len(md.split()) / 200))


def _tpl(p: dict) -> str:
    slug = p["slug"]
    url = f"{BASE}/blog/{slug}"
    titulo = p["titulo"]
    desc = p.get("meta_description") or p.get("resumen") or ""
    og_title = p.get("og_title") or f"{titulo} — Flipazo"
    resumen = p.get("resumen") or desc
    cat = (p.get("tags") or "Guía").split(",")[0].strip()
    fpub = (p.get("created_at") or "")[:10]
    fmod = (p.get("updated_at") or p.get("created_at") or "")[:10]
    body = _md_to_html(p.get("contenido") or "")
    e = lambda s: _html.escape(s or "", quote=True)
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">

  <title>{e(titulo)} — Flipazo</title>
  <meta name="description" content="{e(desc)}">
  <link rel="canonical" href="{url}">

  <meta property="og:title" content="{e(og_title)}">
  <meta property="og:description" content="{e(desc)}">
  <meta property="og:type" content="article">
  <meta property="og:url" content="{url}">
  <meta property="og:site_name" content="Flipazo">

  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{e(og_title)}">
  <meta name="twitter:description" content="{e(desc)}">

  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Article",
    "headline": {json.dumps(titulo, ensure_ascii=False)},
    "description": {json.dumps(desc, ensure_ascii=False)},
    "author": {{ "@type": "Organization", "name": "Flipazo", "url": "{BASE}" }},
    "publisher": {{
      "@type": "Organization", "name": "Flipazo", "url": "{BASE}",
      "logo": {{ "@type": "ImageObject", "url": "{BASE}/flipazo-logo.png" }}
    }},
    "datePublished": "{fpub}",
    "dateModified": "{fmod}",
    "mainEntityOfPage": {{ "@type": "WebPage", "@id": "{url}" }}
  }}
  </script>

  <link rel="icon" type="image/png" sizes="512x512" href="/favicon.png">
  <link rel="apple-touch-icon" href="/favicon.png">
  <script>(function(){{ var t = localStorage.getItem('flipazo_theme')||'light'; if(t==='dark') document.documentElement.setAttribute('data-theme','dark'); }})();</script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;0,900;1,700&family=Nunito:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/blog-article.css">
</head>
<body>

<header class="site-header">
  <div class="site-header__inner">
    <a href="/" class="site-logo" title="Volver al inicio" aria-label="Flipazo — inicio">
      <img src="/flipazo-logo.png" alt="Flipazo" height="49" class="site-logo__img"
           onerror="this.style.display='none';this.nextElementSibling.style.display='block'">
      <svg viewBox="0 0 218 52" xmlns="http://www.w3.org/2000/svg" height="38" aria-hidden="true" style="display:none">
        <text x="2" y="43" font-family="'Nunito', sans-serif" font-size="50" font-weight="900" fill="#F52834" letter-spacing="-0.5">Flipazo</text>
      </svg>
    </a>
    <nav class="header-nav" aria-label="Navegación principal">
      <a href="/" class="header-nav__link">Ofertas</a>
      <a href="/blog" class="header-nav__link header-nav__link--active">Blog</a>
      <a href="/cuenta" class="header-nav__link">Mi&nbsp;Cuenta</a>
      <button id="theme-toggle" class="theme-toggle" onclick="toggleTheme()" aria-label="Cambiar tema">☾</button>
    </nav>
  </div>
  <nav class="blog-subnav" aria-label="Ubicación">
    <a href="/">Inicio</a>
    <span class="blog-subnav__sep">›</span>
    <a href="/blog">Blog</a>
    <span class="blog-subnav__sep">›</span>
    <span class="blog-subnav__current">{e(titulo)}</span>
  </nav>
</header>

<main class="art-main">
  <div class="art-container">
    <article>
      <p class="art-category">{e(cat)}</p>
      <h1 class="art-title">{e(titulo)}</h1>
      <p class="art-entradilla">{e(resumen)}</p>
      <p class="art-readtime">⏱ {_readtime(p.get('contenido') or '')} min de lectura</p>
      <hr class="art-divider">

      <div class="art-body">
        {body}

        <div class="art-cta">
          <strong>Nosotros ya hemos hecho el filtro por ti.</strong>
          <p>Cada oferta que publicamos ha pasado por nuestro proceso de verificación. Tú solo decides si la quieres.</p>
          <a class="art-cta__btn" href="/">Ver ofertas verificadas →</a>
        </div>
      </div>
    </article>
    <aside class="art-aside" aria-label="Chollos recientes">
      <div class="art-aside__head">
        <span class="art-aside__kicker">🔥 En directo</span>
        <h2 class="art-aside__title">Chollos recientes</h2>
      </div>
      <div class="deal-mini-list" id="blog-deals">
        <div class="deal-mini deal-mini--skeleton"></div>
        <div class="deal-mini deal-mini--skeleton"></div>
        <div class="deal-mini deal-mini--skeleton"></div>
        <div class="deal-mini deal-mini--skeleton"></div>
      </div>
      <a href="/" class="art-aside__cta">Ver todas las ofertas →</a>
    </aside>
  </div>
</main>

<footer class="footer">
  <div class="footer__bottom">
    <div class="footer__top">
      <nav class="footer__links" aria-label="Legal">
        <a href="/sobre">Sobre</a>
        <a href="/faq">FAQ</a>
        <a href="/privacidad">Privacidad</a>
        <a href="/cookies">Cookies</a>
        <a href="/aviso-legal">Aviso Legal</a>
        <a href="mailto:hola@flipazo.es">Contacto</a>
        <a href="https://t.me/flipazo" target="_blank" rel="noopener">Telegram</a>
      </nav>
    </div>
    <p class="footer__affiliate">Algunos enlaces son de afiliado: si compras a través de ellos podemos recibir una comisión sin coste adicional para ti. Participamos en Amazon Associates (tag: flipazo-21), Tradedoubler y Awin.</p>
    <p class="footer__copy">© 2026 Flipazo · Barcelona, España · Precios sujetos a cambio sin previo aviso</p>
  </div>
</footer>

<script>
  function toggleTheme() {{
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const next = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('flipazo_theme', next);
    document.getElementById('theme-toggle').textContent = next === 'dark' ? '☀' : '☾';
  }}
  (function(){{ const t = document.documentElement.getAttribute('data-theme')||'light'; const b = document.getElementById('theme-toggle'); if(b) b.textContent = t==='dark'?'☀':'☾'; }})();
</script>
<script src="/blog-sidebar.js" defer></script>
</body>
</html>
"""


def main():
    force = "--force" in sys.argv
    posts = json.load(open(POSTS, encoding="utf-8"))
    gen, skip = [], []
    for p in posts:
        path = os.path.join(BLOG_DIR, f"{p['slug']}.html")
        if os.path.exists(path) and not force:
            skip.append(p["slug"])
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(_tpl(p))
        gen.append(p["slug"])
    print(f"Generados ({len(gen)}): {gen}")
    print(f"Saltados por ya existir ({len(skip)}): {skip}")


if __name__ == "__main__":
    main()
