import { Deal, getAffiliateUrl } from '@/lib/api'

interface DealCardProps {
  deal: Deal
}

function fmtPrice(n: number): string {
  return n.toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' €'
}

function timeAgo(iso?: string): string {
  if (!iso) return ''
  const date  = new Date(iso)
  const diff  = Math.floor((Date.now() - date.getTime()) / 60000)
  let rel: string
  if (diff < 1)         rel = 'Ahora mismo'
  else if (diff < 60)   rel = `Hace ${diff} min`
  else if (diff < 1440) rel = `Hace ${Math.floor(diff / 60)}h`
  else                  rel = `Hace ${Math.floor(diff / 1440)}d`

  const ahora  = new Date()
  const mismoD = date.toDateString() === ahora.toDateString()
  const ayerD  = new Date(ahora.getTime() - 86400000).toDateString() === date.toDateString()
  const hora   = date.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
  const diaStr = mismoD ? 'Hoy' : ayerD ? 'Ayer'
               : date.toLocaleDateString('es-ES', { day: 'numeric', month: 'short' })
  return `${rel} · ${diaStr} ${hora}`
}

export function DealCard({ deal }: DealCardProps) {
  const isReventa = deal.tipo === 'REVENTA' || deal.tipo === 'ARBITRAJE'
  const badgeClass = isReventa ? 'deal__badge deal__badge--reventa' : 'deal__badge deal__badge--oferta'
  const badgeLabel = isReventa ? '♻ Reventa' : '⚡ Oferta'
  const buyUrl     = getAffiliateUrl(deal)
  const ahorro     = deal.precio_original > deal.precio_actual ? deal.precio_original - deal.precio_actual : 0

  const wallapopQuery = encodeURIComponent((deal.titulo || '').split(' ').slice(0, 4).join(' '))
  const wallapopUrl   = `https://es.wallapop.com/app/search?keywords=${wallapopQuery}`

  return (
    <article className="deal">
      {/* Badge tipo */}
      <span className={badgeClass}>{badgeLabel}</span>

      {/* Imagen */}
      {deal.imagen_url && (
        <div className="deal__image-wrap">
          {deal.descuento_pct > 0 && (
            <span className="deal__discount-badge">−{deal.descuento_pct}%</span>
          )}
          <a href={buyUrl} target="_blank" rel="noopener sponsored">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={deal.imagen_url}
              alt=""
              loading="lazy"
            />
          </a>
        </div>
      )}

      {/* Tienda */}
      <p className="deal__store">{deal.tienda}</p>

      {/* Título */}
      <h2 className="deal__title">{deal.titulo}</h2>

      {/* Precio */}
      <div className="deal__pricing">
        {deal.precio_original > 0 && (
          <span className="deal__original">{fmtPrice(deal.precio_original)}</span>
        )}
        <span className="deal__current">{fmtPrice(deal.precio_actual)}</span>
        {deal.descuento_pct > 0 && (
          <span className="deal__pct">−{deal.descuento_pct}%</span>
        )}
      </div>

      {/* Ahorro */}
      {ahorro > 1 && (
        <p className="deal__savings">↓ Ahorras {fmtPrice(ahorro)}</p>
      )}

      {/* Box Reventa / Wallapop */}
      {isReventa && deal.precio_wallapop > 0 && (
        <div className="deal__reventa">
          <span className="deal__reventa-title">💰 Deal de Reventa</span>
          <div className="deal__reventa-math">
            <span>Compra ahora: <strong>{fmtPrice(deal.precio_actual)}</strong></span>
            <span>
              Precio estimado en Wallapop: <strong>~{fmtPrice(deal.precio_wallapop)}</strong>
              {' · '}
              <a href={wallapopUrl} target="_blank" rel="noopener"
                style={{ color: 'var(--badge-rev)', fontSize: 11 }}>
                verificar →
              </a>
            </span>
            <span className="deal__reventa-profit">
              Ganancia neta estimada: +{fmtPrice(deal.beneficio_neto)}
            </span>
          </div>
        </div>
      )}

      {/* Razonamiento */}
      {deal.razonamiento && (
        <p className="deal__reason">"{deal.razonamiento}"</p>
      )}

      {/* CTAs */}
      <div className="deal__cta">
        <a href={buyUrl} target="_blank" rel="noopener sponsored" className="btn btn--primary">
          Comprar →
        </a>
        {isReventa && deal.precio_wallapop > 0 && (
          <a href={wallapopUrl} target="_blank" rel="noopener" className="btn btn--secondary">
            Wallapop →
          </a>
        )}
      </div>

      {/* Timestamp */}
      <p className="deal__time">{timeAgo(deal.publicado_en)}</p>
    </article>
  )
}
