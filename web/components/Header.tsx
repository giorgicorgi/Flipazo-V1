'use client'

import Link from 'next/link'
import { useState, useEffect } from 'react'
import { CATEGORIES, Category } from '@/lib/categories'

interface HeaderProps {
  activeCategory: Category
  onCategoryChange: (cat: Category) => void
  dealCount: number
  hasMore: boolean
  sectionTitle: string
}

export function Header({ activeCategory, onCategoryChange, dealCount, hasMore, sectionTitle }: HeaderProps) {
  const [catOpen, setCatOpen] = useState(false)
  const [dateStr, setDateStr] = useState('')

  useEffect(() => {
    const opts: Intl.DateTimeFormatOptions = {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
    }
    setDateStr(new Date().toLocaleDateString('es-ES', opts).toUpperCase())
  }, [])

  function handleCatClick(cat: Category) {
    onCategoryChange(cat)
    setCatOpen(false)
  }

  return (
    <>
      {/* ── MASTHEAD ─────────────────────────────────────────── */}
      <header className="masthead">
        <p className="masthead__eyebrow">El canal de ofertas más flipante de España</p>
        <Link href="/">
          <h1 className="masthead__name">Flipazo</h1>
        </Link>
        <hr className="masthead__rule" />
        <div className="masthead__meta">
          <span>{dateStr || '—'}</span>
          <span className="masthead__tagline">Ofertas reales. Descuentos verificados.</span>
          <span>
            <span className="live-dot" />
            Actualización automática
          </span>
          <button
            className={`hamburger${catOpen ? ' open' : ''}`}
            aria-label="Categorías"
            aria-expanded={catOpen}
            onClick={() => setCatOpen((v) => !v)}
          >
            <span />
            <span />
            <span />
          </button>
        </div>
      </header>

      {/* ── CATEGORY BAR ─────────────────────────────────────── */}
      <nav className={`cat-bar${catOpen ? ' open' : ''}`} aria-label="Categorías">
        {CATEGORIES.map((cat) => (
          <button
            key={cat}
            className={`cat-pill${activeCategory === cat ? ' active' : ''}`}
            onClick={() => handleCatClick(cat)}
            aria-pressed={activeCategory === cat}
          >
            {cat}
          </button>
        ))}
      </nav>

      {/* ── SECTION HEADER ───────────────────────────────────── */}
      <div className="section-header">
        <span className="section-header__title">{sectionTitle}</span>
        <hr className="section-header__line" />
        <span className="section-header__count">
          {dealCount > 0
            ? `${dealCount} oferta${dealCount !== 1 ? 's' : ''}${hasMore ? '+' : ''}`
            : 'Cargando…'}
        </span>
      </div>
    </>
  )
}
