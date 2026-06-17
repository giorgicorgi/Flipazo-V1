import { DealGrid } from '@/components/DealGrid'
import { CookieBanner } from '@/components/CookieBanner'

export default function HomePage() {
  return (
    <>
      <DealGrid />

      {/* ── ABOUT ─────────────────────────────────────────────── */}
      <section className="about">
        <div className="about__block">
          <h3>Qué es Flipazo</h3>
          <p>
            Canal automatizado de ofertas reales para España. Nuestro sistema analiza cientos
            de productos cada hora y publica solo los descuentos verificados superiores al 40%
            en electrónica, deportes, hogar y más.
          </p>
        </div>
        <div className="about__block">
          <h3>Síguenos en Telegram</h3>
          <p>
            Recibe las alertas en tiempo real directamente en tu móvil.<br /><br />
            <a href="https://t.me/flipazo" target="_blank" rel="noopener">
              → Unirse al canal de Telegram
            </a>
          </p>
        </div>
        <div className="about__block">
          <h3>Contacto</h3>
          <p>
            <a href="mailto:hola@flipazo.es">hola@flipazo.es</a><br /><br />
            <a href="/privacidad.html">Política de privacidad</a><br />
            <a href="/cookies.html">Política de cookies</a><br />
            <a href="/aviso-legal.html">Aviso legal y afiliados</a>
          </p>
        </div>
      </section>

      {/* ── FOOTER ────────────────────────────────────────────── */}
      <footer className="footer">
        <div className="footer__top">
          <span className="footer__brand">Flipazo</span>
          <nav className="footer__links" aria-label="Legal">
            <a href="/privacidad.html">Privacidad</a>
            <a href="/cookies.html">Cookies</a>
            <a href="/aviso-legal.html">Aviso Legal</a>
            <a href="mailto:hola@flipazo.es">Contacto</a>
          </nav>
        </div>
        <p className="footer__affiliate">
          Algunos enlaces son de afiliado: si compras a través de ellos podemos recibir una
          comisión sin coste adicional para ti. Participamos en Amazon Associates (tag: flipazo-21)
          y Awin. Los precios y la disponibilidad pueden cambiar.
        </p>
        <p className="footer__copy">
          © 2025 Flipazo · Barcelona, España · Precios sujetos a cambio sin previo aviso
        </p>
      </footer>

      <CookieBanner />
    </>
  )
}
