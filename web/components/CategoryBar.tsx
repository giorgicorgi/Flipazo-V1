'use client'

import { CATEGORIES, Category } from '@/lib/categories'

interface CategoryBarProps {
  active: Category
  onChange: (cat: Category) => void
}

export function CategoryBar({ active, onChange }: CategoryBarProps) {
  return (
    <div className="sticky top-[calc(var(--header-h,80px))] z-40 section-rule"
      style={{ backgroundColor: 'var(--color-bg)', borderBottom: '1px solid var(--color-border)' }}>
      <div className="max-w-7xl mx-auto px-4">
        <div className="flex items-center gap-2 overflow-x-auto scrollbar-hide py-3">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              onClick={() => onChange(cat)}
              className={`cat-pill ${active === cat ? 'active' : ''}`}
              aria-pressed={active === cat}
            >
              {cat}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
