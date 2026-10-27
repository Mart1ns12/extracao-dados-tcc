"""Casador v3: um caso sintetico por motivo E1-E6, mais os casos de contexto e de revisavel.

Textos sinteticos. As regras vem do config real (IBOV_TICKERS, APELIDOS, TICKER_CODE_CVM...).
"""
from __future__ import annotations

from tcc_scraper.enrich.casador import (
    construir_regras,
    evidencias,
    tem_ocorrencia_bruta,
    trecho_mencoes,
)

REGRAS = construir_regras()
TITULO_NEUTRO = "Mercado fecha em alta"


def test_e1_ao_vivo_nao_casa_vivt3():
    ev = evidencias("Operadora transmite ao vivo o balanco", [], REGRAS)["VIVT3"]
    assert not ev.vincula
    assert ev.n_titulo == 0
    assert ev.invalidas == {"E1": 1}
    assert not ev.revisavel  # E1 e exclusao deterministica: nao vai para a juiza


def test_e1_sem_rumo_nao_casa_rail3():
    ev = evidencias("Ferrovia segue sem rumo", [], REGRAS)["RAIL3"]
    assert not ev.vincula
    assert ev.invalidas == {"E1": 1}
    assert not ev.revisavel


def test_e2_analista_so_invalida_e_revisavel():
    # "Na avaliacao do BTG Pactual" e fonte de analise, nao sujeito da noticia: E2.
    ev = evidencias("Na avaliacao do BTG Pactual, acoes sobem", [], REGRAS)["BPAC11"]
    assert not ev.vincula
    assert ev.invalidas == {"E2": 1}
    assert ev.n_titulo == 0
    assert ev.n_titulo_revisavel == 1
    assert ev.revisavel


def test_e3_palavra_comum_em_minuscula():
    # "rumo" com 4 letras e minuscula no titulo: palavra comum, nao nome proprio.
    ev = evidencias("Economia segue com rumo firme", [], REGRAS)["RAIL3"]
    assert not ev.vincula
    assert ev.invalidas == {"E3": 1}
    assert not ev.revisavel  # E3 nao vai para a juiza


def test_e4_boilerplate_so_no_corpo():
    blocos = ["Petrobras anuncia dividendos"]
    boiler = {0: [(0, len("petrobras"))]}  # o trecho "petrobras" e boilerplate do site
    ev = evidencias(TITULO_NEUTRO, blocos, REGRAS, boiler=boiler)["PETR4"]
    assert not ev.vincula
    assert ev.invalidas == {"E4": 1}
    assert not ev.revisavel  # E4 conta no revisavel, mas so 1 ocorrencia (MIN_CORPO = 2)


def test_e5_panorama_nao_entra_no_revisavel():
    # Bloco com 4 tickers distintos em ocorrencia bruta: panorama de mercado.
    blocos = ["PETR4 VALE3 ITUB4 BBDC4 sobem hoje"]
    ev = evidencias(TITULO_NEUTRO, blocos, REGRAS)["PETR4"]
    assert not ev.vincula
    assert ev.invalidas == {"E5": 1}
    assert not ev.revisavel


def test_e6_cotacao_so_no_corpo():
    blocos = ["PETR4 +2,5% no pregao"]
    ev = evidencias(TITULO_NEUTRO, blocos, REGRAS)["PETR4"]
    assert not ev.vincula
    assert ev.invalidas == {"E6": 1}


def test_codigo_vale_mesmo_com_nome_em_contexto_excluido():
    # "ao vivo" invalida o nome "vivo", mas o codigo VIVT3 continua contando.
    ev = evidencias("Operadora VIVT3 transmite ao vivo", [], REGRAS)["VIVT3"]
    assert ev.vincula
    assert ev.n_titulo == 1


def test_rede_dor_com_apostrofo_casa_rdor3():
    ev = evidencias("Rede D'Or anuncia expansao", [], REGRAS)["RDOR3"]
    assert ev.vincula
    assert ev.n_titulo == 1


def test_petr3_e_petr4_sao_a_mesma_empresa():
    ev = evidencias("PETR3 e PETR4 sobem", [], REGRAS)
    assert ev["PETR3"].outras_empresas_titulo == 0
    assert ev["PETR4"].outras_empresas_titulo == 0


def test_outra_empresa_no_titulo_conta_uma_vez():
    ev = evidencias("PETR3 e PETR4 sobem, VALE3 cai", [], REGRAS)
    assert ev["PETR3"].outras_empresas_titulo == 1  # so VALE3; PETR4 e a mesma empresa
    assert ev["VALE3"].outras_empresas_titulo == 1


def test_revisavel_so_com_e2_e_nao_vincula():
    ev = evidencias("Na avaliacao do BTG Pactual, acoes sobem", [], REGRAS)["BPAC11"]
    assert ev.revisavel
    assert not ev.vincula


def test_corpo_com_duas_mencoes_validas_vincula():
    blocos = ["PETR4 sobe forte hoje", "A Petrobras anuncia dividendos"]
    ev = evidencias(TITULO_NEUTRO, blocos, REGRAS)["PETR4"]
    assert ev.vincula
    assert ev.n_corpo == 2
    assert ev.primeiro_bloco == 0


def test_tem_ocorrencia_bruta_ignora_contexto():
    # Pre-filtro de armazenamento: ocorrencia bruta ("ao vivo" conta), sem E1-E6.
    assert tem_ocorrencia_bruta("Operadora transmite ao vivo", "", REGRAS)
    assert tem_ocorrencia_bruta("", "Rede D'Or anuncia expansao", REGRAS)
    assert not tem_ocorrencia_bruta(TITULO_NEUTRO, "Dia tranquilo no pregao hoje", REGRAS)


def test_trecho_mencoes_respeita_max_chars_e_vazio_sem_mencao():
    texto = "PETR4 sobe forte hoje. Analistas veem mais alta para a Petrobras na sessao de amanha."
    trecho = trecho_mencoes(texto, "PETR4", REGRAS, 60)
    assert "PETR4" in trecho
    assert len(trecho) <= 60
    assert trecho_mencoes(texto, "VALE3", REGRAS, 60) == ""
