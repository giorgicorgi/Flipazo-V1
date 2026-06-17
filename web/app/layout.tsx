import type { Metadata } from 'next'
import '@/styles/globals.css'

export const metadata: Metadata = {
  title: 'Flipazo — Las Mejores Ofertas de España',
  description:
    'Canal automatizado de ofertas reales para España. Descuentos verificados superiores al 40% en electrónica, deportes, hogar y más. Alertas en tiempo real por Telegram.',
  keywords: 'chollos, ofertas, descuentos, amazon, mediamarkt, spain, deals',
  openGraph: {
    title: 'Flipazo — Las Mejores Ofertas de España',
    description: 'Descuentos verificados superiores al 40%. Actualizados automáticamente cada hora.',
    url: 'https://flipazo.es',
    siteName: 'Flipazo',
    locale: 'es_ES',
    type: 'website',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Flipazo — Chollos reales para España',
  },
  metadataBase: new URL('https://flipazo.es'),
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  )
}
