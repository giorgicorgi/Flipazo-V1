'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'

export function CookieBanner() {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const stored = localStorage.getItem('flipazo_consent')
    if (!stored) {
      const timer = setTimeout(() => setVisible(true), 800)
      return () => clearTimeout(timer)
    }
  }, [])

  function accept(level: 'essential' | 'all') {
    const prefs = { essential: true, analytics: level === 'all', ts: Date.now() }
    localStorage.setItem('flipazo_consent', JSON.stringify(prefs))
    setVisible(false)
  }

  return (
    <div
      id="cookie-banner"
      className={visible ? '' : 'hidden'}
      role="dialog"
      aria-label="Aviso de cookies"
    >
      <p className="cookie-banner__text">
        Usamos cookies propias (esenciales) y de terceros (analítica, afiliados) para mejorar el
        servicio.{' '}
        <Link href="/cookies.html">Más información</Link>
      </p>
      <div className="cookie-banner__actions">
        <button className="cookie-btn cookie-btn--reject" onClick={() => accept('essential')}>
          Solo esenciales
        </button>
        <button className="cookie-btn cookie-btn--accept" onClick={() => accept('all')}>
          Aceptar todas
        </button>
      </div>
    </div>
  )
}
