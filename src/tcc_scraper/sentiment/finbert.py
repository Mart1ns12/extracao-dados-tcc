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
import re

from tcc_scraper.config import get_settings
from tcc_scraper.sentiment.base import SentimentScore, SentimentScorer

logger = logging.getLogger(__name__)

MAX_TOKENS = 512

_DEVICE_CUDA_RE = re.compile(r"^cuda(:\d+)?$")


def resolver_device(preferido: str, cuda_disponivel: bool) -> str:
    """Escolhe o device da inferencia sem importar torch (testavel sem ele).

    auto -> cuda se disponivel, senao cpu. cpu -> cpu. cuda ou cuda:N exige CUDA:
    sem ele levanta RuntimeError, porque quem pediu GPU nao deve ganhar CPU em silencio.
    """
    valor = (preferido or "").strip().lower()
    if valor == "auto":
        return "cuda" if cuda_disponivel else "cpu"
    if valor == "cpu":
        return "cpu"
    if _DEVICE_CUDA_RE.match(valor):
        if not cuda_disponivel:
            raise RuntimeError(
                f"SENTIMENT_DEVICE={preferido} pede GPU, mas o torch instalado nao tem CUDA "
                "disponivel. Instale o torch com CUDA ou use SENTIMENT_DEVICE=auto ou cpu."
            )
        return valor
    raise ValueError(
        f"SENTIMENT_DEVICE invalido: {preferido!r}; use auto, cuda, cuda:N ou cpu"
    )


class HuggingFaceFinBERT(SentimentScorer):
    """Scorer baseado em um checkpoint FinBERT do HuggingFace.

    torch e transformers sao importados sob demanda: sao ~2 GB de dependencia e
    quem so coleta dados nao deveria precisar instala-los.

    batch_size e device default vem de settings (SENTIMENT_BATCH_SIZE, SENTIMENT_DEVICE)
    quando o construtor nao os recebe.
    """

    def __init__(
        self,
        model_name: str,
        lang: str,
        batch_size: int | None = None,
        device: str | None = None,
    ) -> None:
        settings = get_settings()
        self.model_name = model_name
        self.lang = lang
        self.batch_size = batch_size if batch_size is not None else settings.sentiment_batch_size
        self._device_preferido = device if device is not None else settings.sentiment_device
        self._device: str | None = None
        self._model = None
        self._tokenizer = None
        self._label_index: dict[str, int] = {}

    @property
    def device(self) -> str | None:
        """Device efetivo ("cpu", "cuda", "cuda:N"). None enquanto o modelo nao carregou."""
        return self._device

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

        # Resolve o device antes de baixar o modelo: pedido de cuda sem CUDA falha rapido.
        device = resolver_device(self._device_preferido, torch.cuda.is_available())

        cache = str(get_settings().hf_cache_dir)
        logger.info("carregando %s (cache: %s)", self.model_name, cache)
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name, cache_dir=cache)
        # _model so e atribuido depois do .to(): se mover para a GPU falhar, o proximo
        # _load() tenta de novo em vez de achar que o modelo ja esta pronto.
        model = AutoModelForSequenceClassification.from_pretrained(
            self.model_name, cache_dir=cache
        )
        model.eval()
        model.to(device)
        self._model = model
        self._device = device
        self._torch = torch
        if device.startswith("cuda"):
            logger.info(
                "%s usa GPU: %s", self.model_name, torch.cuda.get_device_name(device)
            )
        else:
            logger.info("%s usa device %s", self.model_name, device)

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
            encoded = {k: v.to(self._device) for k, v in encoded.items()}
            with self._torch.no_grad():
                logits = self._model(**encoded).logits
            probs = self._torch.nn.functional.softmax(logits, dim=-1).cpu()
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
