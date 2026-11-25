#!/usr/bin/env python3
"""
scripts/blog/build.py — Genera blog/<slug>.html y posts.json desde scripts/blog/posts/*.md

Por qué existe: los artículos eran HTML escrito a mano y se habían roto solos
(párrafos enteros metidos dentro de <h3>, fechas de 2025 en una web que empezó en
abril de 2026, el mismo método contado de tres formas). Ahora cada artículo es un
.md con cabecera, y la página, el JSON-LD, el FAQ y la entrada de posts.json salen
de ahí. Editar el .md y volver a ejecutar:

    python3 scripts/blog/build.py            # todos
    python3 scripts/blog/build.py <slug>     # uno

Formato del .md:
    titulo: …            (H1 y <title>)
    seo_titulo: …        (opcional: <title> distinto del H1)
    descripcion: …       (meta description)
    entradilla: …
    categoria: …
    publicado: 2026-06-26
    actualizado: 2026-11-26
    cta_titulo: …  cta_texto: …  cta_boton: …  cta_url: /
    ---
    cuerpo en markdown sencillo: ## h2, ### h3, párrafos, - listas, 1. listas,
    **negrita**, *cursiva*, [texto](url), > destacado, tablas | a | b |,
    y una sección "## Preguntas frecuentes" con ### pregunta + respuesta → FAQPage.
    [[widget:nombre]] inserta un bloque dinámico (ver WIDGETS).

Los .md viven en scripts/ a propósito: .vercelignore excluye scripts/, así que no
se publican; solo se publica el HTML generado.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
FUENTES = Path(__file__).resolve().parent / "posts"
SITE = "https://www.flipazo.es"

# Bloques dinámicos que se pintan en el navegador con datos de la API.
WIDGETS = {
    "regalos": '<div class="regalos-vivos" id="regalos-vivos" aria-live="polite"></div>\n'
               '<script src="/blog-regalos.js" defer></script>',
}


def inline(t: str) -> str:
    t = html.escape(t, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: f'<a href="{m.group(2)}"'
                         + (' target="_blank" rel="noopener"' if m.group(2).startswith("http") else "")
                         + f">{m.group(1)}</a>", t)
    return t


def bloques(md: str) -> list[tuple[str, object]]:
    """Parte el cuerpo en bloques (tipo, contenido)."""
    out, buf = [], []
    lineas = md.strip("\n").split("\n")
    i = 0

    def vaciar():
        if buf:
            out.append(("p", " ".join(x.strip() for x in buf)))
            buf.clear()

    while i < len(lineas):
        ln = lineas[i]
        s = ln.strip()
        if not s:
            vaciar(); i += 1; continue
        if s.startswith("[[widget:") and s.endswith("]]"):
            vaciar(); out.append(("widget", s[9:-2])); i += 1; continue
        m = re.match(r"^(#{2,3})\s+(.*)$", s)
        if m:
            vaciar(); out.append(("h" + str(len(m.group(1))), m.group(2))); i += 1; continue
        if s.startswith("> "):
            vaciar(); txt = []
            while i < len(lineas) and lineas[i].strip().startswith(">"):
                txt.append(lineas[i].strip()[1:].strip()); i += 1
            out.append(("destacado", " ".join(txt))); continue
        if re.match(r"^[-*]\s+", s):
            vaciar(); items = []
            while i < len(lineas) and re.match(r"^\s*[-*]\s+", lineas[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lineas[i])); i += 1
            out.append(("ul", items)); continue
        if re.match(r"^\d+\.\s+", s):
            vaciar(); items = []
            while i < len(lineas) and re.match(r"^\s*\d+\.\s+", lineas[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lineas[i])); i += 1
            out.append(("ol", items)); continue
        if s.startswith("|"):
            vaciar(); filas = []
            while i < len(lineas) and lineas[i].strip().startswith("|"):
                celdas = [c.strip() for c in lineas[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in celdas):
                    filas.append(celdas)
                i += 1
            out.append(("tabla", filas)); continue
        buf.append(ln); i += 1
    vaciar()
    return out


def render(bls) -> tuple[str, list[tuple[str, str]]]:
    """HTML del cuerpo + lista de (pregunta, respuesta) para el FAQ."""
    partes, faq = [], []
    en_faq = False
    pregunta = None
    for tipo, c in bls:
        if tipo == "h2" and c.strip().lower() == "preguntas frecuentes":
            en_faq = True
            continue
        if en_faq:
            if tipo == "h3":
                pregunta = c
                faq.append([c, ""])
            elif tipo == "p" and pregunta:
                faq[-1][1] = (faq[-1][1] + " " + c).strip()
            elif tipo == "h2":
                en_faq = False
            if en_faq:
                continue
        if tipo in ("h2", "h3"):
            partes.append(f"<{tipo}>{inline(c)}</{tipo}>")
        elif tipo == "p":
            partes.append(f"<p>{inline(c)}</p>")
        elif tipo == "destacado":
            partes.append(f'<div class="dato-destacado">{inline(c)}</div>')
        elif tipo in ("ul", "ol"):
            partes.append(f"<{tipo}>" + "".join(f"<li>{inline(x)}</li>" for x in c) + f"</{tipo}>")
        elif tipo == "tabla":
            cab, *resto = c
            t = "<table><thead><tr>" + "".join(f"<th>{inline(x)}</th>" for x in cab) + "</tr></thead><tbody>"
            for f in resto:
                t += "<tr>" + "".join(f"<td>{inline(x)}</td>" for x in f) + "</tr>"
            partes.append('<div class="tabla-scroll">' + t + "</tbody></table></div>")
        elif tipo == "widget":
            partes.append(WIDGETS[c])
    if faq:
        f = ['<div class="faq-bloque"><p class="faq-bloque__title">Preguntas frecuentes</p>']
        for q, a in faq:
            f.append(f'<div class="faq-item"><p class="faq-q">{inline(q)}</p><p class="faq-a">{inline(a)}</p></div>')
        partes.append("".join(f) + "</div>")
    return "\n        ".join(partes), [(q, a) for q, a in faq]


def texto_plano(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return re.sub(r"\*+", "", s)


def leer(path: Path) -> tuple[dict, str]:
    cab, cuerpo = path.read_text(encoding="utf-8").split("\n---\n", 1)
    meta = {}
    for ln in cab.strip().split("\n"):
        k, v = ln.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, cuerpo


PLANTILLA = Path(__file__).resolve().parent / "plantilla.html"


def construir(path: Path) -> dict:
    meta, cuerpo = leer(path)
    slug = path.stem
    url = f"{SITE}/blog/{slug}"
    cuerpo_html, faq = render(bloques(cuerpo))
    palabras = len(re.sub(r"<[^>]+>", " ", cuerpo_html).split())
    titulo = meta["titulo"]
    seo = meta.get("seo_titulo") or titulo
    desc = meta["descripcion"]
    ld = [{
        "@context": "https://schema.org", "@type": "Article",
        "headline": titulo, "description": desc,
        "author": {"@type": "Organization", "name": "Flipazo", "url": SITE},
        "publisher": {"@type": "Organization", "name": "Flipazo", "url": SITE,
                      "logo": {"@type": "ImageObject", "url": f"{SITE}/flipazo-logo.png"}},
        "datePublished": meta["publicado"], "dateModified": meta.get("actualizado", meta["publicado"]),
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
    }]
    if faq:
        ld.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": texto_plano(q),
             "acceptedAnswer": {"@type": "Answer", "text": texto_plano(a)}} for q, a in faq]})
    e = lambda s: html.escape(s, quote=True)
    pagina = PLANTILLA.read_text(encoding="utf-8")
    for k, v in {
        "{{SEO_TITULO}}": e(seo + " — Flipazo"),
        "{{DESCRIPCION}}": e(desc),
        "{{URL}}": url,
        "{{JSONLD}}": "\n".join(
            '<script type="application/ld+json">\n' + json.dumps(x, ensure_ascii=False, indent=2) + "\n</script>"
            for x in ld),
        "{{TITULO}}": e(titulo),
        "{{CATEGORIA}}": e(meta.get("categoria", "Guía")),
        "{{ENTRADILLA}}": inline(meta["entradilla"]),
        "{{LECTURA}}": str(max(1, round(palabras / 220))),
        "{{CUERPO}}": cuerpo_html,
        "{{CTA_TITULO}}": e(meta.get("cta_titulo", "Las ofertas de hoy, ya comprobadas.")),
        "{{CTA_TEXTO}}": e(meta.get("cta_texto", "Cada una la hemos comparado con su precio habitual antes de publicarla.")),
        "{{CTA_BOTON}}": e(meta.get("cta_boton", "Ver ofertas de hoy →")),
        "{{CTA_URL}}": e(meta.get("cta_url", "/")),
    }.items():
        pagina = pagina.replace(k, v)
    assert "{{" not in pagina, f"{slug}: marcador sin rellenar"
    (RAIZ / "blog" / f"{slug}.html").write_text(pagina, encoding="utf-8")
    return {
        "slug": slug, "titulo": titulo, "resumen": meta["entradilla"],
        "meta_description": desc, "og_title": seo + " — Flipazo",
        "tags": meta.get("categoria", ""),
        "created_at": meta["publicado"] + "T00:00:00Z",
        "updated_at": meta.get("actualizado", meta["publicado"]) + "T00:00:00Z",
        "imagen_url": None, "contenido": cuerpo.strip(),
    }


def main(args: list[str]) -> None:
    fuentes = sorted(FUENTES.glob("*.md"))
    if args:
        fuentes = [f for f in fuentes if f.stem in args]
    pj = RAIZ / "posts.json"
    posts = {p["slug"]: p for p in json.loads(pj.read_text(encoding="utf-8"))}
    for f in fuentes:
        posts[f.stem] = construir(f)
        print(f"✓ blog/{f.stem}.html")
    # Los retirados (redirigidos en vercel.json) salen del índice.
    retirados = set(json.loads((Path(__file__).resolve().parent / "retirados.json").read_text()))
    lista = [p for s, p in posts.items() if s not in retirados]
    lista.sort(key=lambda p: p["updated_at"], reverse=True)
    pj.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ posts.json ({len(lista)} artículos)")


if __name__ == "__main__":
    main(sys.argv[1:])
