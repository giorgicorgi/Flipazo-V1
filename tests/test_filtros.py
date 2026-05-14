"""
Tests de regresión — filtros de calidad de Flipazo.

Cada test documenta un falso positivo real detectado en producción o un
invariante que no debe romperse nunca. Añadir un caso aquí cuando se
descubra un nuevo error de filtrado.

Ejecutar:
    python tests/test_filtros.py          # sin dependencias externas
    python -m pytest tests/ -v            # con pytest (mismo resultado)

Los imports pesados (playwright, requests, scrapers, etc.) se mockean antes
de importar flipazo_main, por lo que este test funciona sin entorno de prod.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

# ── Mock de dependencias pesadas ───────────────────────────────────────────
# Debe ejecutarse ANTES de importar flipazo_main.
_MOCKS = [
    "requests",
    "dotenv",
    "playwright",
    "playwright.async_api",
    "affiliate",
    "affiliate.link_builder",
    "scrapers",
    "scrapers.pss_email",
    "scrapers.tradedoubler_feed",
    "scrapers.decathlon_feed",
    "scrapers.toysrus_feed",
    "scrapers.beep_feed",
    "discovery",
]
for _m in _MOCKS:
    sys.modules.setdefault(_m, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flipazo_main import (  # noqa: E402
    _es_producto_valido,
    _precio_aceptable,
    PALABRAS_PROHIBIDAS,
    DESCUENTO_MINIMO,
    PRECIO_MINIMO,
    PRECIO_MAXIMO,
    PRECIO_MINIMO_LC,
    DESCUENTO_LC_MINIMO,
)


# ══════════════════════════════════════════════════════════════════
# FALSOS POSITIVOS CONOCIDOS — deben ser RECHAZADOS
# ══════════════════════════════════════════════════════════════════

class TestFalsosPositivosProduccion(unittest.TestCase):
    """
    Productos reales que se publicaron por error. Cada test cita la causa raíz.
    """

    def test_cecotec_ventilador_descuento_imposible(self):
        """
        Cecotec EnergySilence 560 Max Smart: 569€ → 34,90€ (94% off).
        Causa: error de dato en feed MediaMarkt (precio ref. incorrecto).
        Fix: tope global descuento_pct >= 90.
        """
        self.assertFalse(_es_producto_valido("Cecotec EnergySilence 560 Max Smart", 94))

    def test_cartucho_hp_solo_sin_frase_completa(self):
        """
        HP 924 Cartucho Negro XL: 76% off.
        Causa: PALABRAS_PROHIBIDAS tenía 'cartucho de tinta' (frase completa)
               pero no 'cartucho' solo. El título no incluía 'de tinta'.
        Fix: añadir 'cartucho' como término independiente.
        """
        self.assertFalse(_es_producto_valido("HP 924 Cartucho Negro XL", 76))

    def test_toner_hp_sin_tilde(self):
        """
        TONER HP LaserJet 143A Original: 79% off.
        Causa: PALABRAS_PROHIBIDAS tenía 'tóner' con tilde.
               Al hacer .lower() 'TONER' → 'toner' ≠ 'tóner'.
        Fix: añadir 'toner' sin tilde.
        """
        self.assertFalse(_es_producto_valido("TONER HP LaserJet 143A Original", 79))

    def test_breville_junta_recambio_99pct(self):
        """
        'Cafetera express - Breville VCF126X': era una junta/gasket, no la cafetera.
        Causa: 'junta' no estaba en PALABRAS_PROHIBIDAS; además 99% off no tenía tope.
        Fix: añadir 'junta' + tope global 90%.
        """
        self.assertFalse(
            _es_producto_valido("Cafetera Express Breville VCF126X Junta Tórica Repuesto", 99)
        )

    def test_accesorio_generico(self):
        """La palabra 'accesorio' ya bloqueaba; verificar que sigue activo."""
        self.assertFalse(_es_producto_valido("Accesorio Universal para Aspirador Dyson", 45))

    def test_cartucho_tinta_frase_completa_sigue_bloqueando(self):
        """El término original 'cartucho de tinta' también debe seguir bloqueando."""
        self.assertFalse(_es_producto_valido("Cartucho de Tinta Canon PG-545 Negro", 55))

    def test_repuesto_electrodomestico(self):
        self.assertFalse(_es_producto_valido("Repuesto Filtro Aspirador Dyson V11", 60))

    def test_reacondicionado_bloqueado(self):
        self.assertFalse(_es_producto_valido("iPhone 13 Reacondicionado 128 GB", 50))


# ══════════════════════════════════════════════════════════════════
# TOPE DE DESCUENTO IMPOSIBLE
# ══════════════════════════════════════════════════════════════════

class TestDescuentoImposible(unittest.TestCase):
    """descuento_pct >= 90 siempre es un error de dato en el feed."""

    def test_exactamente_90_rechazado(self):
        self.assertFalse(_es_producto_valido("Samsung Galaxy Tab S9 Tablet Android", 90))

    def test_91_rechazado(self):
        self.assertFalse(_es_producto_valido("Sony WH-1000XM5 Auriculares Bluetooth", 91))

    def test_99_rechazado(self):
        self.assertFalse(_es_producto_valido("Dyson V15 Aspiradora sin Cable", 99))

    def test_89_aceptado_si_pasa_otros_filtros(self):
        """89% es alto pero dentro del rango permitido (ej. outlet de temporada)."""
        self.assertTrue(_es_producto_valido("Sony WH-1000XM5 Auriculares Bluetooth", 89))


# ══════════════════════════════════════════════════════════════════
# CECOTEC — umbral propio 60-89%
# ══════════════════════════════════════════════════════════════════

class TestCecotecUmbral(unittest.TestCase):

    def test_cecotec_55_rechazado(self):
        self.assertFalse(_es_producto_valido("Cecotec Conga 7090 Robot Aspirador", 55))

    def test_cecotec_exactamente_60_aceptado(self):
        """La condición es 'descuento_pct < 60', así que 60 exacto sí pasa."""
        self.assertTrue(_es_producto_valido("Cecotec Conga 7090 Robot Aspirador", 60))

    def test_cecotec_61_aceptado(self):
        self.assertTrue(_es_producto_valido("Cecotec Conga 7090 Robot Aspirador", 61))

    def test_cecotec_89_aceptado(self):
        self.assertTrue(_es_producto_valido("Cecotec Conga 7090 Robot Aspirador", 89))

    def test_cecotec_90_rechazado_por_tope_global(self):
        self.assertFalse(_es_producto_valido("Cecotec Conga 7090 Robot Aspirador", 90))


# ══════════════════════════════════════════════════════════════════
# PRODUCTOS VÁLIDOS — no deben ser bloqueados por error
# ══════════════════════════════════════════════════════════════════

class TestValidosNoDebenBloquearse(unittest.TestCase):
    """Regresión inversa: asegura que los filtros no son demasiado agresivos."""

    def test_auriculares_sony(self):
        self.assertTrue(
            _es_producto_valido("Sony WH-1000XM5 Auriculares Inalámbricos Bluetooth", 45)
        )

    def test_cafetera_delonghi(self):
        """'cafetera' no es palabra prohibida (es el producto, no un repuesto)."""
        self.assertTrue(
            _es_producto_valido("DeLonghi Dedica Arte Cafetera Express Manual EC885", 42)
        )

    def test_tv_samsung(self):
        self.assertTrue(
            _es_producto_valido("Samsung Crystal UHD 55 Pulgadas Smart TV 4K", 45)
        )

    def test_proyector_xiaomi(self):
        """
        Xiaomi Smart Projector L1 Pro.
        Causa original: no aparecía en categoría Tecnología en el frontend
        (ver _RX.tecnologia en index.html — 'proyector' faltaba).
        Este test cubre que el backend no lo bloquea.
        """
        self.assertTrue(
            _es_producto_valido("Xiaomi Smart Projector L1 Pro 1080p Full HD", 45)
        )

    def test_zapatillas_nike(self):
        self.assertTrue(
            _es_producto_valido("Nike Air Max 270 Zapatillas Running Hombre", 52)
        )

    def test_robot_aspirador_premium(self):
        self.assertTrue(
            _es_producto_valido("Roborock S8 MaxV Ultra Robot Aspirador con Mopa", 47)
        )

    def test_consola_ps5(self):
        self.assertTrue(
            _es_producto_valido("Sony PlayStation 5 Consola Slim Digital Edition", 40)
        )

    def test_macbook_portátil(self):
        self.assertTrue(
            _es_producto_valido("Apple MacBook Air 13 M2 256 GB SSD 8 GB RAM", 42)
        )


# ══════════════════════════════════════════════════════════════════
# PALABRAS PROHIBIDAS — términos críticos deben estar en la lista
# ══════════════════════════════════════════════════════════════════

class TestPalabrasProhibidasCriticas(unittest.TestCase):
    """
    Prueba de presencia: si estos términos desaparecen de PALABRAS_PROHIBIDAS
    (por una edición accidental), el test falla inmediatamente.
    """

    def _assert_prohibida(self, termino: str):
        self.assertIn(
            termino,
            PALABRAS_PROHIBIDAS,
            msg=f"'{termino}' debe estar en PALABRAS_PROHIBIDAS",
        )

    def test_cartucho(self):
        self._assert_prohibida("cartucho")

    def test_toner_sin_tilde(self):
        self._assert_prohibida("toner")

    def test_junta(self):
        self._assert_prohibida("junta")

    def test_accesorio(self):
        self._assert_prohibida("accesorio")

    def test_repuesto(self):
        self._assert_prohibida("repuesto")

    def test_reacondicionado(self):
        self._assert_prohibida("reacondicionado")

    def test_recambio(self):
        self._assert_prohibida("recambio")


# ══════════════════════════════════════════════════════════════════
# PRECIO ACEPTABLE — rangos y low-cost
# ══════════════════════════════════════════════════════════════════

class TestPrecioAceptable(unittest.TestCase):

    def test_precio_normal_descuento_minimo(self):
        self.assertTrue(_precio_aceptable(PRECIO_MINIMO, DESCUENTO_MINIMO))

    def test_precio_maximo_aceptado(self):
        self.assertTrue(_precio_aceptable(PRECIO_MAXIMO, 40))

    def test_precio_sobre_maximo_rechazado(self):
        self.assertFalse(_precio_aceptable(PRECIO_MAXIMO + 1, 50))

    def test_precio_bajo_minimo_standard_low_cost_ok(self):
        """Productos low-cost (8-24.99€) aceptados con ≥ DESCUENTO_LC_MINIMO."""
        self.assertTrue(_precio_aceptable(PRECIO_MINIMO_LC, DESCUENTO_LC_MINIMO))

    def test_precio_muy_bajo_rechazado(self):
        """Por debajo de PRECIO_MINIMO_LC (8€) → siempre rechazado."""
        self.assertFalse(_precio_aceptable(PRECIO_MINIMO_LC - 0.01, 80))

    def test_descuento_insuficiente_rechazado(self):
        self.assertFalse(_precio_aceptable(50.0, DESCUENTO_MINIMO - 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
