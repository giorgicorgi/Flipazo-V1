/** @type {import('next').NextConfig} */
const nextConfig = {
  images: {
    remotePatterns: [
      { protocol: 'https', hostname: 'm.media-amazon.com' },
      { protocol: 'https', hostname: 'images-na.ssl-images-amazon.com' },
      { protocol: 'https', hostname: 'assets.mmsrg.com' },
      { protocol: 'https', hostname: 'www.pccomponentes.com' },
      { protocol: 'https', hostname: 'www.decathlon.es' },
      { protocol: 'https', hostname: '**.worten.es' },
      { protocol: 'https', hostname: '**.elcorteingles.es' },
      { protocol: 'https', hostname: '**.mammothbikes.com' },
    ],
  },
}

module.exports = nextConfig
