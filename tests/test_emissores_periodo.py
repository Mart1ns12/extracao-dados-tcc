"""Emissor por periodo: um ticker ligado a companhias diferentes ao longo do tempo (JBSS32, NATU3).

Sem rede e sem banco: so le as tabelas de tcc_scraper.config.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tcc_scraper.config import (
    CORRECOES_PRECO_B3,
    EMISSORES_POR_PERIODO,
    MUDANCAS_TICKER,
    TICKER_CODE_CVM,
    cnpj_do_ticker_em,
    codigos_cvm_universo,
    emissor_vale,
    emissores_do_ticker,
    fator_emissor,
    ticker_atual_do_negociado,
    tickers_do_emissor,
)


@pytest.mark.parametrize(
    ("cd", "data", "esperado"),
    [
        (20575, date(2025, 6, 8), True),
        (20575, date(2025, 6, 9), False),
        (80233, date(2025, 5, 27), False),
        (80233, date(2025, 8, 13), True),
        (19550, date(2025, 5, 12), False),
        (19550, date(2025, 8, 11), True),
        (24783, date(2025, 5, 12), True),
        (9512, date(2025, 5, 12), True),
        (9512, date(2010, 1, 1), True),
        (None, date(2010, 1, 1), True),
    ],
)
def test_emissor_vale_respeita_as_datas_do_periodo(cd, data, esperado):
    assert emissor_vale(cd, data) is esperado


def test_tickers_do_emissor_separa_natura_da_holding():
    assert tickers_do_emissor(19550, date(2021, 3, 19)) == []
    assert tickers_do_emissor(24783, date(2021, 3, 19)) == ["NATU3"]
    assert tickers_do_emissor(19550, date(2025, 8, 11)) == ["NATU3"]
    assert tickers_do_emissor(20575, date(2025, 6, 8)) == ["JBSS32"]
    assert tickers_do_emissor(80233, date(2025, 6, 9)) == ["JBSS32"]


def test_ticker_atual_do_negociado_devolve_o_bdr_com_fator():
    assert ticker_atual_do_negociado("JBSS3", date(2024, 5, 1)) == ("JBSS32", 0.5)
    assert ticker_atual_do_negociado("NTCO3", date(2024, 1, 1)) == ("NATU3", 1.0)


def test_ticker_atual_do_negociado_sem_periodo_que_contem_a_data_e_none():
    assert ticker_atual_do_negociado("NATU3", date(2021, 6, 23)) is None
    assert ticker_atual_do_negociado("JBSS3", date(2025, 7, 1)) is None


def test_ticker_atual_do_negociado_sem_entrada_devolve_o_proprio_ticker():
    assert ticker_atual_do_negociado("PETR4", date(2021, 1, 1)) == ("PETR4", 1.0)


def test_fator_emissor_so_difere_de_um_no_periodo_do_bdr():
    assert fator_emissor(20575, date(2024, 5, 1)) == 0.5
    assert fator_emissor(80233, date(2025, 8, 13)) == 1.0
    assert fator_emissor(9512, date(2024, 5, 1)) == 1.0
    assert fator_emissor(None, date(2024, 5, 1)) == 1.0


def test_cnpj_do_ticker_em_muda_com_o_emissor():
    assert cnpj_do_ticker_em("JBSS32", date(2025, 6, 8)) == "02916265000160"
    assert cnpj_do_ticker_em("JBSS32", date(2025, 6, 9)) == "49115815000105"
    assert cnpj_do_ticker_em("PETR4", date(2025, 6, 9)) is None


def test_emissores_do_ticker_sem_entrada_explicita_e_periodo_aberto():
    (periodo,) = emissores_do_ticker("PETR4")
    assert periodo.cd_cvm == TICKER_CODE_CVM["PETR4"]
    assert periodo.inicio is None and periodo.fim is None and periodo.fator == 1.0
    assert emissores_do_ticker("TICKER_INEXISTENTE") == ()


def test_codigos_cvm_universo_inclui_os_codigos_dos_periodos():
    codigos = codigos_cvm_universo()
    assert set(TICKER_CODE_CVM.values()) <= codigos
    assert {20575, 80233, 19550, 24783} <= codigos


def test_periodos_sao_contiguos():
    for ticker, periodos in EMISSORES_POR_PERIODO.items():
        for anterior, proximo in zip(periodos, periodos[1:], strict=False):
            assert anterior.fim is not None, ticker
            assert proximo.inicio == anterior.fim + timedelta(days=1), ticker


def test_ultimo_periodo_e_aberto_e_do_codigo_atual():
    for ticker, periodos in EMISSORES_POR_PERIODO.items():
        ultimo = periodos[-1]
        assert ultimo.fim is None, ticker
        assert ultimo.cd_cvm == TICKER_CODE_CVM[ticker], ticker


def test_troca_de_ticker_fecha_o_periodo_do_codigo_antigo():
    for antigo, novo, data in MUDANCAS_TICKER:
        if novo not in EMISSORES_POR_PERIODO:
            continue
        assert data is not None, antigo
        do_antigo = [p for p in EMISSORES_POR_PERIODO[novo] if p.ticker_negociado == antigo]
        assert do_antigo, novo
        assert do_antigo[-1].fim == data - timedelta(days=1), novo


def test_fator_troca_das_correcoes_bate_com_o_emissor_no_fim_da_janela():
    for ticker, correcao in CORRECOES_PRECO_B3.items():
        if ticker not in EMISSORES_POR_PERIODO:
            continue
        periodo = next(
            p
            for p in EMISSORES_POR_PERIODO[ticker]
            if p.ticker_negociado in correcao.codigos
            and (p.inicio is None or p.inicio <= correcao.fim)
            and (p.fim is None or correcao.fim <= p.fim)
        )
        assert fator_emissor(periodo.cd_cvm, correcao.fim) == correcao.fator_troca, ticker
