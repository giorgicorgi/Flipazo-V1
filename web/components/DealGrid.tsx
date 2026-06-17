'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Deal, fetchDeals, PAGE_SIZE_EXPORT } from '@/lib/api'
import { Category, categorizeDeals } from '@/lib/categories'
import { DealCard } from './DealCard'
import { Header } from './Header'

export function DealGrid() {
  const [allDeals, setAllDeals]         = useState<Deal[]>([])
  const [offset, setOffset]             = useState(0)
  const [loading, setLoading]           = useState(false)
  const [hasMore, setHasMore]           = useState(true)
  const [activeCategory, setActiveCategory] = useState<Category>('Todas')
  const [lastSeenId, setLastSeenId]     = useState(0)
  const sentinelRef = useRef<HTMLDivElement>(null)

  const filteredDeals = categorizeDeals(allDeals, activeCategory)

  const sectionTitle = activeCategory === 'Todas'
    ? 'Últimas ofertas'
    : activeCategory

  const loadMore = useCallback(async () => {
    if (loading || !hasMore) return
    setLoading(true)
    try {
      const batch = await fetchDeals({ limit: PAGE_SIZE_EXPORT, offset })
      if (batch.length < PAGE_SIZE_EXPORT) setHasMore(false)
      if (batch.length === 0) return

      if (offset === 0 && batch.length > 0) {
        setLastSeenId(Math.max(...batch.map((d) => Number(d.id) || 0)))
      }

      setAllDeals((prev) => {
        const ids = new Set(prev.map((d) => d.id))
        return [...prev, ...batch.filter((d) => !ids.has(d.id))]
      })
      setOffset((prev) => prev + batch.length)
    } catch (e) {
      console.error('Error cargando deals:', e)
    } finally {
      setLoading(false)
    }
  }, [loading, hasMore, offset])

  // Poll para deals nuevos cada 5 min
  useEffect(() => {
    const poll = async () => {
      try {
        const batch = await fetchDeals({ limit: 10, offset: 0 })
        if (!batch.length) return
        const maxId = Math.max(...batch.map((d) => Number(d.id) || 0))
        if (maxId <= lastSeenId) return
        const ids = new Set(allDeals.map((d) => d.id))
        const nuevos = batch.filter((d) => (Number(d.id) || 0) > lastSeenId && !ids.has(d.id))
        if (!nuevos.length) return
        setLastSeenId(maxId)
        setAllDeals((prev) => [...nuevos, ...prev])
      } catch {}
    }
    const timer = setInterval(poll, 5 * 60 * 1000)
    return () => clearInterval(timer)
  }, [allDeals, lastSeenId])

  // IntersectionObserver scroll infinito
  useEffect(() => {
    const sentinel = sentinelRef.current
    if (!sentinel) return
    const observer = new IntersectionObserver(
      (entries) => { if (entries[0].isIntersecting) loadMore() },
      { rootMargin: '300px' }
    )
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [loadMore])

  // Si quedan pocas visibles cargar más
  useEffect(() => {
    if (filteredDeals.length < 8 && hasMore && !loading) loadMore()
  }, [activeCategory]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <Header
        activeCategory={activeCategory}
        onCategoryChange={setActiveCategory}
        dealCount={filteredDeals.length}
        hasMore={hasMore}
        sectionTitle={sectionTitle}
      />

      <main className="deals-grid" id="js-grid">
        {filteredDeals.length === 0 && !loading ? (
          <div className="empty">Sin ofertas en esta categoría</div>
        ) : (
          filteredDeals.map((deal) => <DealCard key={deal.id} deal={deal} />)
        )}

        {/* Skeleton al cargar */}
        {loading && Array.from({ length: 6 }).map((_, i) => (
          <div key={`sk-${i}`} className="skeleton-card">
            <div className="skeleton-line" style={{ height: 14, width: '40%', marginBottom: 12 }} />
            <div className="skeleton-line" style={{ aspectRatio: '1/1', marginBottom: 14 }} />
            <div className="skeleton-line" style={{ height: 10, width: '30%', marginBottom: 8 }} />
            <div className="skeleton-line" style={{ height: 17, width: '80%', marginBottom: 6 }} />
            <div className="skeleton-line" style={{ height: 17, width: '60%', marginBottom: 14 }} />
            <div className="skeleton-line" style={{ height: 30, width: '45%' }} />
          </div>
        ))}
      </main>

      {loading && (
        <div className="load-indicator">Cargando más ofertas…</div>
      )}

      <div ref={sentinelRef} style={{ height: 1 }} aria-hidden="true" />
    </>
  )
}
