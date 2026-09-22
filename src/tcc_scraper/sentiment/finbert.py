"""Implementacoes do FinBERT para ingles e portugues.

ProsusAI/finbert     -- ingles, o modelo citado em docs/fontes_dataset.xlsx.
lucas-leme/FinBERT-PT-BR -- portugues do mercado financeiro brasileiro, treinado
                            sobre 1,4 milhao de textos financeiros em pt-BR.

A planilha ja advertia (nota 5) que o ProsusAI nao serve para portugues. Traduzir
~130 mil manchetes seria caro e introduziria ruido; usar um modelo nativo em pt e
mais barato e mais fiel.

Ambos sao BertForSequenceClassification, entao a mesma classe atende aos dois --
o que muda e o nome do modelo e a ordem dos rotulos, que NAO e a mesma.
"""

from __future__ import annotations

import logging

from tcc_scraper.config import get_settings
from tcc_scraper.sentiment.base import SentimentScore, SentimentScorer

logger = logging.getLogger(__name__)

MAX_TOKENS = 512
DEFAULT_BATCH_SIZE = 32


class HuggingFaceFinBERT(SentimentScorer):
    """Scorer baseado em um checkpoint FinBERT do HuggingFace.

    torch e transformers sao importados sob demanda: sao ~2 GB de dependencia e
    quem so coleta dados nao deveria precisar instala-los.
    """

    def __init__(self, model_name: str, lang: str, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self.model_name = model_name
        self.lang = lang
        self.batch_size = batch_size
        self._model = None
        self._tokenizer = None
        self._label_index: dict[str, int] = {}

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - depende do extra opcional
            raise RuntimeError(
                "Scoring exige os extras de sentimento: pip install -e '.[sentiment]'"
            ) from exc

        cache = str(get_settings().hf_cache_dir)
        logger.info("carregando %s (cache: %s)", self.model_name, cache)
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name, cache_dir=cache)
        self._model = AutoModelForSequenceClassification.from_pretrained(
            self.model_name, cache_dir=cache
        )
        self._model.eval()
        self._torch = torch

        # A ordem dos rotulos difere entre checkpoints. Ler id2label do proprio
        # modelo evita trocar positivo por negativo silenciosamente -- um erro que
        # nao quebra nada e inverteria o sinal do dataset inteiro.
        id2label = self._model.config.id2label
        self._label_index = {str(v).lower(): int(k) for k, v in id2label.items()}
        logger.info("rotulos de %s: %s", self.model_name, self._label_index)

    def _index_of(self, *aliases: str) -> int:
        for alias in aliases:
            if alias in self._label_index:
                return self._label_index[alias]
        raise ValueError(
            f"{self.model_name}: nenhum rotulo em {aliases}; disponiveis: {self._label_index}"
        )

    def score_batch(self, texts: list[str]) -> list[SentimentScore]:
        if not texts:
            return []
        self._load()

        i_pos = self._index_of("positive", "positivo")
        i_neu = self._index_of("neutral", "neutro")
        i_neg = self._index_of("negative", "negativo")

        scores: list[SentimentScore] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = texts[start : start + self.batch_size]
            encoded = self._tokenizer(
                chunk,
                padding=True,
                truncation=True,
                max_length=MAX_TOKENS,
                return_tensors="pt",
            )
            with self._torch.no_grad():
                logits = self._model(**encoded).logits
            probs = self._torch.nn.functional.softmax(logits, dim=-1)
            for row in probs:
                scores.append(
                    SentimentScore(
                        prob_pos=float(row[i_pos]),
                        prob_neu=float(row[i_neu]),
                        prob_neg=float(row[i_neg]),
                    )
                )
        return scores


def finbert_en(**kwargs) -> HuggingFaceFinBERT:
    return HuggingFaceFinBERT("ProsusAI/finbert", lang="en", **kwargs)


def finbert_pt(**kwargs) -> HuggingFaceFinBERT:
    return HuggingFaceFinBERT("lucas-leme/FinBERT-PT-BR", lang="pt", **kwargs)
