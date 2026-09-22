"""O scoring precisa gravar em blocos, nao so no fim.

Com 88 mil artigos a inferencia leva ~30 min. Pontuar tudo e so entao gravar
significa que uma queda aos 25 min perde o trabalho inteiro -- e, como a query de
pendentes filtra por quem ainda nao tem sentimento, gravar em blocos torna a etapa
retomavel de graca.
"""

from __future__ import annotations

import datetime as dt

from tcc_scraper.cli import _blocos, _linhas_sentimento
from tcc_scraper.sentiment.base import SentimentScore


def test_blocos_divide_preservando_tudo() -> None:
    itens = list(range(250))
    blocos = list(_blocos(itens, 100))

    assert [len(b) for b in blocos] == [100, 100, 50]
    assert [x for b in blocos for x in b] == itens


def test_blocos_de_lista_vazia() -> None:
    assert list(_blocos([], 10)) == []


def test_bloco_maior_que_a_lista() -> None:
    assert list(_blocos([1, 2], 500)) == [[1, 2]]


def test_linhas_de_sentimento_tem_as_colunas_da_tabela() -> None:
    agora = dt.datetime.now(dt.UTC)
    rows = [(7, "Manchete", "pt", None), (8, "Outra", "en", "corpo completo")]
    resultados = [
        (SentimentScore(prob_pos=0.7, prob_neu=0.2, prob_neg=0.1), "modelo-pt"),
        (SentimentScore(prob_pos=0.1, prob_neu=0.2, prob_neg=0.7), "modelo-en"),
    ]

    linhas = _linhas_sentimento(rows, resultados, agora)

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
