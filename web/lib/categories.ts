import { Deal } from './api'

export const CATEGORIES = [
  'Todas',
  'Tecnología',
  'Herramientas',
  'Deportes',
  'Calzado',
  'Hogar',
  'Belleza',
  'Juguetes',
  'Moda',
] as const

export type Category = (typeof CATEGORIES)[number]

type CategoryDef = {
  id: string
  test: (d: Deal) => boolean
}

const CATS: CategoryDef[] = [
  {
    id: 'Tecnología',
    test: (d) =>
      /smartphone|móvil|iphone|galaxy\b|tablet|ipad|portátil|laptop|macbook|pc gaming|monitor\b|televisor|\btv\b|oled|qled|auricular|cascos|airpods|wh-?1000|bose q|kindle|cámara|gopro|smartwatch|consola\b|ps5|playstation|xbox|nintendo|switch\b|ssd|disco duro|\bram\b|gpu|rtx|procesador|impresora|router|logitech|razer|corsair|steelseries/i.test(
        d.titulo
      ) || ['PcComponentes', 'MediaMarkt', 'Worten'].includes(d.tienda),
  },
  {
    id: 'Herramientas',
    test: (d) =>
      /taladro|sierra\b|destornillador|amoladora|lijadora|compresor|karcher|bosch\b|dewalt|makita|milwaukee|stanley\b|llave inglesa|multímetro/i.test(
        d.titulo
      ),
  },
  {
    id: 'Deportes',
    test: (d) =>
      /running|trail\b|fitness|gimnasio|mancuerna|natación|fútbol|balón|raqueta|garmin|polar\b|fitbit|under armour|giro\b|shimano|alpinestars|ciclismo|mountain bike|\bmtb\b|bicicleta|\bbici\b|sillín/i.test(
        d.titulo
      ) || ['Mammoth Bikes', 'Decathlon', 'PrivateSportShop'].includes(d.tienda),
  },
  {
    id: 'Calzado',
    test: (d) =>
      /zapatilla|zapato|\bbota\b|sandalia|sneaker|\bnike\b|\badidas\b|\bpuma\b|reebok|new balance|asics|jordan\b|air max|ultraboost|salomon|hoka\b|on running/i.test(
        d.titulo
      ),
  },
  {
    id: 'Hogar',
    test: (d) =>
      /aspirador|robot aspirador|roomba|irobot|roborock|lefant|dreame|ecovacs|eufy|freidora|cafetera|nespresso|delonghi|thermomix|airfryer|\bplancha\b|lavadora|secadora|frigorífico|microondas|\bhorno\b|lavavajillas|dyson|rowenta|tefal|cecotec|shark\b|bissell|kenwood|magimix|breville|sage\b/i.test(
        d.titulo
      ),
  },
  {
    id: 'Belleza',
    test: (d) =>
      /perfume|eau de parfum|eau de toilette|\bcolonia\b|afeitadora|maquinilla|oral.?b|braun serie|oneblade|depiladora|plancha.*pelo|rizador/i.test(
        d.titulo
      ),
  },
  {
    id: 'Juguetes',
    test: (d) =>
      /\blego\b|playmobil|hasbro|mattel|hot wheels|barbie|juguete|muñeca|\bfunko\b|\bnerf\b/i.test(
        d.titulo
      ),
  },
  {
    id: 'Moda',
    test: (d) =>
      /\bbolso\b|mochila|maleta\b|lacoste|ralph lauren|\bdior\b|\bchanel\b|armani|calvin klein|tommy hilfiger/i.test(
        d.titulo
      ),
  },
]

export function categorizeDeals(deals: Deal[], category: Category): Deal[] {
  if (category === 'Todas') return deals
  const cat = CATS.find((c) => c.id === category)
  return cat ? deals.filter(cat.test) : deals
}
