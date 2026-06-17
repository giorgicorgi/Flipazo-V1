const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'https://flipazo.es'
const PAGE_SIZE = 24

export interface Deal {
  id: string
  titulo: string
  precio_actual: number
  precio_original: number
  descuento_pct: number
  tienda: string
  tipo: 'ARBITRAJE' | 'REVENTA' | 'OFERTA' | 'DESCARTAR'
  imagen_url: string
  score_ai: number
  precio_wallapop: number
  beneficio_neto: number
  publicado_en?: string
  razonamiento?: string
}

export interface FetchDealsOptions {
  offset?: number
  limit?: number
  tipo?: string
  tienda?: string
}

export async function fetchDeals(opts: FetchDealsOptions = {}): Promise<Deal[]> {
  const params = new URLSearchParams({
    limit: String(opts.limit ?? PAGE_SIZE),
    offset: String(opts.offset ?? 0),
  })
  if (opts.tipo) params.set('tipo', opts.tipo)
  if (opts.tienda) params.set('tienda', opts.tienda)

  const res = await fetch(`${API_BASE}/api/deals?${params}`, {
    next: { revalidate: 60 },
  })
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

export function formatPrice(price: number): string {
  return price.toLocaleString('es-ES', { style: 'currency', currency: 'EUR' })
}

export function formatSavings(original: number, current: number): string {
  return formatPrice(original - current)
}

export function getAffiliateUrl(deal: Deal): string {
  return `${API_BASE}/r/${deal.id}?canal=web`
}

export const PAGE_SIZE_EXPORT = PAGE_SIZE
