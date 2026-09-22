"""Testes do roteamento de sentimento.

Usam scorers falsos: baixar os dois modelos BERT (~1 GB) para testar roteamento
seria desperdicio. O que se testa aqui e a logica de roteamento e ordenacao, nao
a qualidade do FinBERT.
"""

from __future__ import annotations

from tcc_scraper.sentiment.base import SentimentScore, SentimentScorer
from tcc_scraper.sentiment.router import SentimentRouter, detect_lang


class FakeScorer(SentimentScorer):
    """Devolve uma probabilidade que identifica o modelo, para rastrear o roteamento."""

    def __init__(self, model_name: str, lang: str, marker: float) -> None:
        self.model_name = model_name
        self.lang = lang
        self.marker = marker
        self.received: list[str] = []

    def score_batch(self, texts: list[str]) -> list[SentimentScore]:
        self.received.extend(texts)
        return [SentimentScore(prob_pos=self.marker, prob_neu=0.0, prob_neg=0.0) for _ in texts]


def _router() -> tuple[SentimentRouter, FakeScorer, FakeScorer]:
    pt = FakeScorer("fake-pt", "pt", marker=0.11)
    en = FakeScorer("fake-en", "en", marker=0.99)
    return SentimentRouter({"pt": pt, "en": en}), pt, en


# --------------------------------------------------------------------------------------
# Deteccao de idioma
# --------------------------------------------------------------------------------------
def test_detecta_portugues() -> None:
    texto = "Petrobras anuncia aumento no lucro liquido e distribuicao de dividendos aos acionistas"
    assert detect_lang(texto) == "pt"


def test_detecta_ingles() -> None:
    texto = (
        "Petrobras announces higher net income and approves dividend distribution"
        " to shareholders"
    )
    assert detect_lang(texto) == "en"


def test_idioma_nao_suportado_cai_no_fallback() -> None:
    """Materia em espanhol nao deve ir para um modelo aleatorio."""
    assert detect_lang("El precio del petroleo subio tras la decision de la OPEP") == "pt"


def test_texto_vazio_usa_fallback() -> None:
    assert detect_lang("") == "pt"
    assert detect_lang("   ") == "pt"


# --------------------------------------------------------------------------------------
# Roteamento
# --------------------------------------------------------------------------------------
def test_roteia_cada_texto_para_o_modelo_do_seu_idioma() -> None:
    router, pt, en = _router()

    resultados = router.score_texts(["texto A", "text B"], langs=["pt", "en"])

    assert pt.received == ["texto A"]
    assert en.received == ["text B"]
    assert resultados[0][1] == "fake-pt"
    assert resultados[1][1] == "fake-en"


def test_preserva_a_ordem_da_entrada_mesmo_agrupando_por_idioma() -> None:
    """O agrupamento por idioma nao pode embaralhar o resultado."""
    router, _, _ = _router()

    resultados = router.score_texts(
        ["pt1", "en1", "pt2", "en2", "pt3"], langs=["pt", "en", "pt", "en", "pt"]
    )

    modelos = [r[1] for r in resultados]
    assert modelos == ["fake-pt", "fake-en", "fake-pt", "fake-en", "fake-pt"]
    assert [r[0].prob_pos for r in resultados] == [0.11, 0.99, 0.11, 0.99, 0.11]


def test_agrupa_para_aproveitar_o_batch() -> None:
    """Cinco textos em dois idiomas devem virar duas chamadas, nao cinco."""
    router, pt, en = _router()
    chamadas = {"pt": 0, "en": 0}
    orig_pt, orig_en = pt.score_batch, en.score_batch

    def conta(nome, orig):
        def wrapper(texts):
            chamadas[nome] += 1
            return orig(texts)
        return wrapper

    pt.score_batch = conta("pt", orig_pt)
    en.score_batch = conta("en", orig_en)

    router.score_texts(["a", "b", "c", "d", "e"], langs=["pt", "pt", "en", "pt", "en"])

    assert chamadas == {"pt": 1, "en": 1}


def test_lista_vazia_nao_chama_modelo() -> None:
    router, pt, en = _router()

    assert router.score_texts([]) == []
    assert pt.received == [] and en.received == []


def test_score_e_positivo_menos_negativo() -> None:
    s = SentimentScore(prob_pos=0.7, prob_neu=0.2, prob_neg=0.1)
    assert s.score == 0.7 - 0.1
