"""Roteamento de textos para o modelo de sentimento do idioma correto."""

from __future__ import annotations

import logging

from tcc_scraper.sentiment.base import SentimentScore, SentimentScorer
from tcc_scraper.sentiment.finbert import finbert_en, finbert_pt

logger = logging.getLogger(__name__)

SUPPORTED = ("pt", "en")


def detect_lang(text: str, fallback: str = "pt") -> str:
    """Detecta o idioma de um texto curto.

    O idioma vem do locale da consulta ao Google News, mas nem sempre bate: uma
    busca pt-BR devolve materia de agencia internacional em ingles. Detectar pelo
    texto evita mandar ingles ao modelo portugues e vice-versa.
    """
    if not text or not text.strip():
        return fallback
    try:
        import py3langid

        lang, _ = py3langid.classify(text)
    except Exception:  # pragma: no cover - detector nunca deve derrubar o pipeline
        return fallback
    return lang if lang in SUPPORTED else fallback


class SentimentRouter:
    """Agrupa textos por idioma e delega ao scorer correspondente.

    Agrupar importa por desempenho: alternar entre dois modelos BERT a cada texto
    desperdicaria o batching, que e o que torna a inferencia em CPU viavel.
    """

    def __init__(self, scorers: dict[str, SentimentScorer] | None = None) -> None:
        self.scorers = scorers or {"pt": finbert_pt(), "en": finbert_en()}

    def model_for(self, lang: str) -> SentimentScorer:
        return self.scorers.get(lang) or next(iter(self.scorers.values()))

    def score_texts(
        self, texts: list[str], langs: list[str] | None = None
    ) -> list[tuple[SentimentScore, str]]:
        """Pontua textos e devolve (score, nome_do_modelo) na ordem da entrada."""
        if not texts:
            return []
        if langs is None:
            langs = [detect_lang(t) for t in texts]

        buckets: dict[str, list[int]] = {}
        for i, lang in enumerate(langs):
            buckets.setdefault(lang if lang in self.scorers else "pt", []).append(i)

        results: list[tuple[SentimentScore, str] | None] = [None] * len(texts)
        for lang, indices in buckets.items():
            scorer = self.model_for(lang)
            scored = scorer.score_batch([texts[i] for i in indices])
            for i, score in zip(indices, scored, strict=True):
                results[i] = (score, scorer.model_name)

        return [r for r in results if r is not None]
