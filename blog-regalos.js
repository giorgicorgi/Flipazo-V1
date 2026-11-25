/* Flipazo — bloque "regalos por presupuesto" del artículo de regalos.
   Pinta las ofertas vivas de ahora mismo agrupadas por precio. Así la guía nunca
   recomienda un precio que ya no existe: lo que se ve es lo que hay hoy. */
(function () {
  "use strict";
  var el = document.getElementById("regalos-vivos");
  if (!el) return;
  var API = "https://api.flipazo.es";
  var TRAMOS = [
    { t: "Menos de 25 €", min: 0, max: 25 },
    { t: "De 25 a 50 €", min: 25, max: 50 },
    { t: "De 50 a 100 €", min: 50, max: 100 },
    { t: "Más de 100 €", min: 100, max: 1e9 }
  ];
  var POR_TRAMO = 4, POR_TIENDA = 2;

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmt(n) {
    return (Number(n) || 0).toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";
  }
  function tarjeta(d) {
    var orig = Number(d.precio_original) || 0, precio = Number(d.precio_actual) || 0;
    var pct = Number(d.descuento_pct) || 0;
    return '<a class="deal-mini" href="' + API + "/r/" + encodeURIComponent(d.id) +
      '?canal=blog_regalos" target="_blank" rel="noopener sponsored">' +
      '<div class="deal-mini__img">' + (d.imagen_url ? '<img src="' + esc(d.imagen_url) +
      '" alt="" loading="lazy" onerror="this.style.display=\'none\'">' : "") + "</div>" +
      '<div class="deal-mini__info">' +
      (d.tienda ? '<span class="deal-mini__store">' + esc(d.tienda) + "</span>" : "") +
      '<p class="deal-mini__title">' + esc((d.titulo || "").slice(0, 70)) + "</p>" +
      '<div class="deal-mini__prices">' +
      (orig > precio ? '<span class="deal-mini__orig">' + fmt(orig) + "</span>" : "") +
      '<span class="deal-mini__now">' + fmt(precio) + "</span>" +
      (pct > 0 ? '<span class="deal-mini__pct">-' + pct + "%</span>" : "") +
      "</div></div></a>";
  }

  el.innerHTML = '<p class="regalos-vivos__cargando">Cargando las ofertas de hoy…</p>';
  fetch(API + "/api/deals?limit=500&offset=0")
    .then(function (r) { return r.ok ? r.json() : []; })
    .then(function (deals) {
      deals = (Array.isArray(deals) ? deals : []).filter(function (d) {
        return !d.expirado && d.imagen_url && Number(d.precio_actual) > 0;
      });
      deals.sort(function (a, b) {
        return (b.deal_score || 0) - (a.deal_score || 0) || (b.descuento_pct || 0) - (a.descuento_pct || 0);
      });
      var html = TRAMOS.map(function (tr) {
        var elegidos = [], porTienda = {};
        deals.forEach(function (d) {
          var p = Number(d.precio_actual);
          if (elegidos.length >= POR_TRAMO || p < tr.min || p >= tr.max) return;
          if ((porTienda[d.tienda] || 0) >= POR_TIENDA) return;
          porTienda[d.tienda] = (porTienda[d.tienda] || 0) + 1;
          elegidos.push(d);
        });
        if (!elegidos.length) return "";
        return '<h3 class="regalos-vivos__tramo">' + tr.t + "</h3>" +
          '<div class="regalos-vivos__grid">' + elegidos.map(tarjeta).join("") + "</div>";
      }).join("");
      el.innerHTML = html ? html + '<p class="regalos-vivos__nota">Ofertas vivas ahora mismo, con al menos un 40 % de descuento frente a su precio habitual. Cambian cada día.</p>' : "";
    })
    .catch(function () { el.innerHTML = ""; });
})();
