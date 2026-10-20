"""O scoring precisa gravar em blocos, nao so no fim.

Com 88 mil artigos a inferencia leva ~30 min. Pontuar tudo e so entao gravar
significa que uma queda aos 25 min perde o trabalho inteiro -- e, como a query de
pendentes filtra por quem ainda nao tem sentimento, gravar em blocos torna a etapa
retomavel de graca.
"""

from __future__ import annotations

import datetime as dt
from contextlib import nullcontext

from tcc_scraper.sentiment import scoring
from tcc_scraper.sentiment.base import SentimentScore
from tcc_scraper.sentiment.scoring import blocos, linhas_sentimento, score_pending


def test_blocos_divide_preservando_tudo() -> None:
    itens = list(range(250))
    partes = list(blocos(itens, 100))

    assert [len(b) for b in partes] == [100, 100, 50]
    assert [x for b in partes for x in b] == itens


def test_blocos_de_lista_vazia() -> None:
    assert list(blocos([], 10)) == []


def test_bloco_maior_que_a_lista() -> None:
    assert list(blocos([1, 2], 500)) == [[1, 2]]


def test_linhas_de_sentimento_tem_as_colunas_da_tabela() -> None:
    agora = dt.datetime.now(dt.UTC)
    rows = [(7, "Manchete", "pt", None), (8, "Outra", "en", "corpo completo")]
    resultados = [
        (SentimentScore(prob_pos=0.7, prob_neu=0.2, prob_neg=0.1), "modelo-pt"),
        (SentimentScore(prob_pos=0.1, prob_neu=0.2, prob_neg=0.7), "modelo-en"),
    ]

    linhas = linhas_sentimento(rows, resultados, agora)

    assert len(linhas) == 2
    assert linhas[0]["article_id"] == 7
    assert linhas[0]["model"] == "modelo-pt"
    assert linhas[0]["prob_pos"] == 0.7
    # text_source registra se o score saiu do corpo ou so da manchete -- e o que
    # permite avaliar, depois, se o enriquecimento melhorou o sinal.
    assert linhas[0]["text_source"] == "title"
    assert linhas[1]["text_source"] == "body"
    assert set(linhas[0]) == {
        "article_id", "model", "prob_pos", "prob_neu", "prob_neg",
        "text_source", "scored_at",
    }


class _RouterFalso:
    """Router sem modelo: devolve o mesmo score para todo texto."""

    def __init__(self) -> None:
        self.scorers: dict = {}

    def score_texts(self, texts: list[str], langs: list[str] | None = None) -> list:
        score = SentimentScore(prob_pos=0.6, prob_neu=0.3, prob_neg=0.1)
        return [(score, "modelo-falso") for _ in texts]


class _ObservadorEspiao:
    def __init__(self) -> None:
        self.planos: list[int] = []
        self.unidades: list[tuple] = []

    def plano(self, n_blocos: int) -> None:
        self.planos.append(n_blocos)

    def deve_parar(self) -> bool:
        return False

    def unidade(self, nome: str, *, ok: bool, itens: int, saturada: bool, erro) -> None:
        self.unidades.append((nome, ok, itens))


def test_score_pending_grava_por_bloco_e_reporta(monkeypatch) -> None:
    rows = [
        (1, "Manchete um", "pt", None),
        (2, "Manchete dois", "pt", "corpo completo"),
        (3, "Three", "en", None),
    ]
    gravacoes: list[list[dict]] = []
    monkeypatch.setattr(scoring, "pendentes_de_score", lambda limit=None: rows)
    monkeypatch.setattr(scoring, "session_scope", lambda: nullcontext(None))
    monkeypatch.setattr(
        scoring,
        "bulk_upsert",
        lambda session, tabela, linhas, **kw: gravacoes.append(linhas) or len(linhas),
    )
    observador = _ObservadorEspiao()

    out = score_pending(router=_RouterFalso(), observador=observador)

    assert observador.planos == [1], "3 artigos cabem em um bloco"
    assert len(observador.unidades) == 1
    assert len(gravacoes) == 1
    assert out == {
        "artigos": 3, "gravados": 3, "com_corpo": 1, "so_manchete": 2, "interrompido": 0,
    }


def test_score_pending_sem_pendentes_devolve_zeros(monkeypatch) -> None:
    monkeypatch.setattr(scoring, "pendentes_de_score", lambda limit=None: [])
    observador = _ObservadorEspiao()

    out = score_pending(router=_RouterFalso(), observador=observador)

    assert out == {
        "artigos": 0, "gravados": 0, "com_corpo": 0, "so_manchete": 0, "interrompido": 0,
    }
    assert observador.planos == [], "sem trabalho, o observador nem e avisado"
