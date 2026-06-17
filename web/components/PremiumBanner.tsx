'use client'

import Link from 'next/link'

export function PremiumBanner() {
  return (
    <div
      className="border-b"
      style={{
        backgroundColor: '#0a0a0a',
        borderColor: '#1a1a1a',
      }}
    >
      <div className="max-w-7xl mx-auto px-4 py-3 flex flex-col sm:flex-row items-center justify-between gap-3 text-center sm:text-left">
        <div className="flex items-center gap-3">
          <span
            className="hidden sm:flex items-center justify-center w-8 h-8 font-bold text-black text-sm rounded-full flex-shrink-0"
            style={{ backgroundColor: '#ffe112' }}
          >
            +
          </span>
          <div>
            <span className="font-semibold text-white text-sm">
              Algunos deals están bloqueados.{' '}
            </span>
            <span className="text-gray-400 text-sm">
              Los descuentos &gt;50% son exclusivos para miembros Premium.
            </span>
          </div>
        </div>
        <Link
          href="/premium"
          className="flex-shrink-0 px-4 py-1.5 text-sm font-bold text-black rounded transition-opacity hover:opacity-90 cursor-pointer"
          style={{ backgroundColor: '#ffe112' }}
        >
          Ver Premium — 3,90€/mes
        </Link>
      </div>
    </div>
  )
}
