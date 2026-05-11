"""
discovery — capa de descubrimiento emocional para Flipazo.

Transforma deals con descuento en deals que despiertan curiosidad:
  - Deal Score 0-100 (heurística determinista, sin coste IA)
  - Tags emocionales (deterministas a partir de heurísticas)
  - Hook + social context (Claude Haiku, batched, cacheado en DB)

API pública:
  from discovery import calcular_deal_score, asignar_tags, generar_hooks_batch
"""

from .scoring import calcular_deal_score, asignar_tags
from .emotional_layer import generar_hooks_batch

__all__ = ["calcular_deal_score", "asignar_tags", "generar_hooks_batch"]
