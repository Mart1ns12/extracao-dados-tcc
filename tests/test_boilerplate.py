"""IndiceShingles: detecta trecho repetido em 5 posts, na janela causal de 30 dias.

Textos sinteticos.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tcc_scraper.enrich.boilerplate import IndiceShingles
from tcc_scraper.enrich.casador import normaliza_mapa

BOIL = "Assine a newsletter diaria do portal e receba as principais noticias do mercado financeiro"
BASE = datetime(2026, 10, 1, 9, 0)


def _blocos(i: int) -> list[str]:
    """Bloco proprio do post (nao se repete) + o rodape de boilerplate (se repete)."""
    return [f"Noticia exclusiva numero {i} sobre a empresa numero {i} hoje", BOIL]


def test_detecta_boilerplate_so_a_partir_do_quinto_post():
    idx = IndiceShingles()
    for i in range(1, 5):
        idx.adicionar(("infomoney", i), BASE + timedelta(hours=i), _blocos(i))
        assert idx.trechos(("infomoney", i), _blocos(i)) == {}  # ate 4 posts: abaixo de N_BOILER

    idx.adicionar(("infomoney", 5), BASE + timedelta(hours=5), _blocos(5))
    trechos = idx.trechos(("infomoney", 5), _blocos(5))

    assert set(trechos) == {1}  # so o rodape (bloco 1); o bloco proprio nao e boilerplate
    bn, _ = normaliza_mapa(BOIL)
    assert trechos[1] == [(0, len(bn))]  # o rodape inteiro, num trecho so


def test_janela_causal_nao_ve_posts_do_futuro():
    # Com 4 posts ate aqui, o post 4 nao chega a N_BOILER: nenhum post posterior entra na conta.
    idx = IndiceShingles()
    for i in range(1, 5):
        idx.adicionar(("exame", i), BASE + timedelta(hours=i), _blocos(i))
    assert idx.trechos(("exame", 4), _blocos(4)) == {}


def test_posts_de_mais_de_30_dias_saem_da_janela():
    idx = IndiceShingles()
    for i in range(1, 5):
        idx.adicionar(("exame", i), BASE, _blocos(i))
    idx.adicionar(("exame", 5), BASE + timedelta(days=30), _blocos(5))
    # Janela de BASE+30d e (BASE, BASE+30d]: os posts de BASE ficam de fora.
    assert idx.trechos(("exame", 5), _blocos(5)) == {}


def test_posts_dentro_de_29_dias_ainda_contam():
    idx = IndiceShingles()
    for i in range(1, 5):
        idx.adicionar(("exame", i), BASE, _blocos(i))
    idx.adicionar(("exame", 5), BASE + timedelta(days=29), _blocos(5))
    assert set(idx.trechos(("exame", 5), _blocos(5))) == {1}


def test_adicionar_fora_de_ordem_levanta():
    idx = IndiceShingles()
    idx.adicionar(("moneytimes", 1), BASE + timedelta(days=2), _blocos(1))
    with pytest.raises(ValueError):
        idx.adicionar(("moneytimes", 2), BASE, _blocos(2))


def test_trechos_so_para_o_ultimo_post_adicionado():
    idx = IndiceShingles()
    idx.adicionar(("infomoney", 1), BASE, _blocos(1))
    idx.adicionar(("infomoney", 2), BASE + timedelta(hours=1), _blocos(2))
    with pytest.raises(ValueError):
        idx.trechos(("infomoney", 1), _blocos(1))
