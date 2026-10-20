from __future__ import annotations

import pytest

from tcc_scraper.llm.juiza_regras import (
    contar_empresas_no_titulo,
    nomes_por_ticker,
    normalizar,
    regra_barata,
)

PANORAMA = {
    "papel": "mencao",
    "tipo_conteudo": "resumo_mercado",
    "evento": "macro",
    "qualidade": 0.1,
    "confianca": 0.9,
    "tickers_adicionais": [],
}

NOMES = nomes_por_ticker(
    {
        "VALE3": "Vale",
        "PETR4": "Petrobras",
        "ITUB4": "Itaú",
        "BBDC4": "Bradesco",
        "RENT3": "Localiza",
        "ABEV3": "Ambev",
    },
    {"ITUB4": ["Itau Unibanco"], "ABEV3": ["Skol"]},
)


def _artigo(titulo: str, dominio: str = "infomoney.com.br", origem: str = "google_news") -> dict:
    return {
        "id": 1,
        "title": titulo,
        "source_domain": dominio,
        "origem": origem,
        "lang": "pt",
    }


def test_normalizar_remove_acento_e_colapsa_espacos() -> None:
    assert normalizar("Ação Ã") == "acao a"
    assert normalizar("  Olá   mundo ") == "ola mundo"


def test_nomes_por_ticker_une_nome_e_apelidos_normalizados() -> None:
    assert nomes_por_ticker({"ITUB4": "Itaú"}, {"ITUB4": ["Itau Unibanco"]}) == {
        "ITUB4": ["itau", "itau unibanco"]
    }
    assert NOMES["ABEV3"] == ["ambev", "skol"]


def test_contar_ignora_o_proprio_ticker() -> None:
    assert contar_empresas_no_titulo("Petrobras e Itaú sobem", NOMES, ignorar="PETR4") == 1
    assert contar_empresas_no_titulo("Petrobras e Itaú sobem", NOMES) == 2


def test_contar_exige_palavra_inteira() -> None:
    assert contar_empresas_no_titulo("Localizar mais frota agora", NOMES) == 0


def test_contar_nome_curto_so_com_inicial_maiuscula() -> None:
    assert contar_empresas_no_titulo("Ação da vale cai hoje", NOMES) == 0
    assert contar_empresas_no_titulo("Vale cai hoje", NOMES) == 1


def test_contar_casa_apelido() -> None:
    assert contar_empresas_no_titulo("Skol lidera vendas no trimestre", NOMES) == 1


@pytest.mark.parametrize(
    ("titulo", "ticker"),
    [
        ("Ibovespa fecha em alta com Vale e Petrobras", "PETR4"),
        ("Carteira recomendada de outubro: 5 ações para comprar", "ITUB4"),
        ("Agenda do dia: PIB, Copom e balanços", "VALE3"),
        ("Maiores altas do dia: Vale sobe 3%", "VALE3"),
        ("Vale, Petrobras, Itaú e Bradesco puxam o índice", "VALE3"),
    ],
)
def test_panorama_de_mercado_vira_resumo(titulo: str, ticker: str) -> None:
    assert regra_barata(_artigo(titulo), ticker, NOMES) == PANORAMA


def test_vale_verbo_nao_conta_e_nao_e_panorama() -> None:
    assert regra_barata(_artigo("Vale a pena investir em FIIs agora?"), "VALE3", NOMES) is None


def test_noticia_de_uma_empresa_vai_para_a_llm() -> None:
    artigo = _artigo("Petrobras anuncia novo campo no pré-sal")
    assert regra_barata(artigo, "PETR4", NOMES) is None


def test_duas_empresas_nao_bastam_para_panorama() -> None:
    assert regra_barata(_artigo("Petrobras e Itaú sobem"), "BBDC4", NOMES) is None


@pytest.mark.parametrize(
    "dominio",
    ["youtube.com", "www.youtube.com", "m.youtube.com", "open.spotify.com", "tiktok.com"],
)
def test_midia_sem_texto_vira_podcast_video(dominio: str) -> None:
    assert regra_barata(_artigo("Entrevista com o CEO", dominio), "PETR4", NOMES) == {
        "papel": "mencao",
        "tipo_conteudo": "podcast_video",
        "evento": "outro",
        "qualidade": 0.0,
        "confianca": 1.0,
        "tickers_adicionais": [],
    }


def test_cvm_ipe_vira_fato_relevante() -> None:
    artigo = _artigo("Fato relevante: aquisição", "rad.cvm.gov.br", origem="cvm_ipe")
    assert regra_barata(artigo, "PETR4", NOMES) == {
        "papel": "protagonista",
        "tipo_conteudo": "reportagem",
        "evento": "fato_relevante",
        "qualidade": 1.0,
        "confianca": 1.0,
        "tickers_adicionais": [],
    }


def test_dominio_comum_sem_regra_vai_para_a_llm() -> None:
    artigo = _artigo("Petrobras anuncia dividendos", "www.infomoney.com.br")
    assert regra_barata(artigo, "PETR4", NOMES) is None
