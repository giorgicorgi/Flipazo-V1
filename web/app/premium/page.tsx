import type { Metadata } from 'next'
import Link from 'next/link'

export const metadata: Metadata = {
  title: 'Flipazo Premium — Próximamente',
  description: 'Canal premium de Flipazo. Muy pronto.',
}

export default function PremiumPage() {
  return (
    <div style={{ maxWidth: 640, margin: '80px auto', padding: '0 40px', fontFamily: 'var(--serif)' }}>
      {/* Masthead minimal */}
      <div style={{ borderBottom: '3px solid #111', paddingBottom: 18, marginBottom: 40 }}>
        <Link href="/" style={{ fontFamily: 'var(--serif)', fontWeight: 900, fontSize: 32, letterSpacing: '-0.02em', textTransform: 'uppercase' }}>
          Flipazo
        </Link>
      </div>

      <p style={{ fontFamily: 'var(--serif-sc)', fontSize: 11, letterSpacing: '0.18em', color: '#444', marginBottom: 24 }}>
        Próximamente
      </p>

      <h1 style={{ fontFamily: 'var(--serif)', fontWeight: 900, fontSize: 'clamp(32px, 6vw, 52px)', lineHeight: 1.1, marginBottom: 24 }}>
        Acceso anticipado a los mejores chollos
      </h1>

      <p style={{ fontFamily: 'var(--mono)', fontSize: 13, color: '#444', lineHeight: 1.8, marginBottom: 32 }}>
        Estamos preparando un canal Premium con alertas en tiempo real, acceso prioritario a
        los deals de mayor descuento y un canal privado de Telegram. Por{' '}
        <strong>3,90 €/mes</strong>, sin permanencia.
      </p>

      <p style={{ fontFamily: 'var(--mono)', fontSize: 13, color: '#444', lineHeight: 1.8, marginBottom: 48 }}>
        Si quieres ser de los primeros en acceder, escríbenos a{' '}
        <a href="mailto:hola@flipazo.es" style={{ color: '#111', textDecoration: 'underline' }}>
          hola@flipazo.es
        </a>{' '}
        o únete al canal gratuito de Telegram para estar al tanto.
      </p>

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <a
          href="https://t.me/flipazo"
          target="_blank"
          rel="noopener noreferrer"
          className="btn btn--primary"
        >
          Canal Telegram →
        </a>
        <Link href="/" className="btn btn--secondary">
          ← Volver a las ofertas
        </Link>
      </div>

      <div style={{ marginTop: 80, borderTop: '1px solid #e0e0e0', paddingTop: 24 }}>
        <nav style={{ display: 'flex', gap: 20, flexWrap: 'wrap' }}>
          {[
            ['Privacidad', '/privacidad.html'],
            ['Cookies', '/cookies.html'],
            ['Aviso Legal', '/aviso-legal.html'],
          ].map(([label, href]) => (
            <a key={href} href={href}
              style={{ fontFamily: 'var(--mono)', fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase', color: '#444' }}>
              {label}
            </a>
          ))}
        </nav>
      </div>
    </div>
  )
}
