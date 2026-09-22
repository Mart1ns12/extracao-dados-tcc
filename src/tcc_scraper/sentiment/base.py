"""Interface de scoring de sentimento.

Abstrair o modelo tem uma razao pratica: o FinBERT da ProsusAI e treinado em ingles,
mas metade do corpus deste projeto e em portugues. Duas implementacoes atendem a
mesma interface e o router escolhe por idioma, sem que o resto do pipeline saiba
qual modelo rodou.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SentimentScore:
    prob_pos: float
    prob_neu: float
    prob_neg: float

    @property
    def score(self) -> float:
        """Score FinBERT do dataset: positivo - negativo, no intervalo [-1, 1]."""
        return self.prob_pos - self.prob_neg


class SentimentScorer(ABC):
    """Contrato de um modelo de sentimento financeiro."""

    model_name: str
    lang: str

    @abstractmethod
    def score_batch(self, texts: list[str]) -> list[SentimentScore]:
        """Pontua textos em lote, preservando a ordem da entrada."""

    def score(self, text: str) -> SentimentScore:
        return self.score_batch([text])[0]
