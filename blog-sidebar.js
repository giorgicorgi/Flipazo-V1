/* Flipazo — sidebar de chollos para artículos del blog.
   Carga automática de las ofertas más recientes (la API ordena por publicado_en DESC). */
(function () {
  "use strict";
  var el = document.getElementById("blog-deals");
  if (!el) return;

  var API = "https://api.flipazo.es/api/deals?limit=8&offset=0";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function fmt(n) {
    return (Number(n) || 0).toLocaleString("es-ES", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    }) + " €";
  }

  fetch(API)
    .then(function (r) { return r.ok ? r.json() : []; })
    .then(function (deals) {
      if (!Array.isArray(deals) || !deals.length) { el.innerHTML = ""; return; }
      el.innerHTML = deals.slice(0, 6).map(function (d) {
        var precio = Number(d.precio_actual) || 0;
        var orig = Number(d.precio_original) || 0;
        var pct = Number(d.descuento_pct) || 0;
        var img = d.imagen_url
          ? '<img src="' + esc(d.imagen_url) + '" alt="" loading="lazy" onerror="this.style.display=\'none\'">'
          : "";
        return '' +
          '<a class="deal-mini" href="' + esc(d.url_affiliate || "#") + '" target="_blank" rel="noopener sponsored">' +
            '<div class="deal-mini__img">' + img + "</div>" +
            '<div class="deal-mini__info">' +
              (d.tienda ? '<span class="deal-mini__store">' + esc(d.tienda) + "</span>" : "") +
              '<p class="deal-mini__title">' + esc((d.titulo || "").slice(0, 70)) + "</p>" +
              '<div class="deal-mini__prices">' +
                (orig > precio ? '<span class="deal-mini__orig">' + fmt(orig) + "</span>" : "") +
                '<span class="deal-mini__now">' + fmt(precio) + "</span>" +
                (pct > 0 ? '<span class="deal-mini__pct">-' + pct + "%</span>" : "") +
              "</div>" +
            "</div>" +
          "</a>";
      }).join("");
    })
    .catch(function () { el.innerHTML = ""; });
})();
