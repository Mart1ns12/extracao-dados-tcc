"""aplicar_fator_cotacao: divide por lote o preco do Yahoo na janela do FATCOT da B3."""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from tcc_scraper.collectors.prices_yahoo import PriceRow, aplicar_fator_cotacao
from tcc_scraper.config import FATORES_COTACAO_YAHOO, FatorCotacaoYahoo

DIA = dt.date


def _linha(
    data: dt.date,
    close: float,
    volume: int = 1000,
    abertura: float | None = None,
    maxima: float | None = None,
    minima: float | None = None,
) -> PriceRow:
    """Linha do Yahoo. Sem open/high/low, todos repetem o close."""
    return PriceRow(
        ticker="RAIZ4",
        date=data,
        open=close if abertura is None else abertura,
        high=close if maxima is None else maxima,
        low=close if minima is None else minima,
        close=close,
        adj_close=close,
        volume=volume,
    )


def _serie_raizen_em_lote() -> list[PriceRow]:
    """Valores reais do Yahoo para RAIZ4: 30/10 por acao; 03/11 repete o lote (FATCOT 100)."""
    return [
        _linha(DIA(2026, 10, 30), 0.30),
        _linha(
            DIA(2026, 11, 3),
            27.10,
            volume=13_496_600,
            abertura=30.30,
            maxima=30.30,
            minima=21.00,
        ),
        _linha(DIA(2026, 11, 6), 28.00),
    ]


def test_divide_por_lote_a_partir_de_03_11_e_volume_fica() -> None:
    rows = _serie_raizen_em_lote()

    resultado = {r.date: r for r in aplicar_fator_cotacao(rows, FATORES_COTACAO_YAHOO, "RAIZ4")}

    dia = resultado[DIA(2026, 11, 3)]
    assert dia.close == pytest.approx(0.271)
    assert dia.adj_close == pytest.approx(0.271)
    assert dia.open == pytest.approx(0.303)
    assert dia.high == pytest.approx(0.303)
    assert dia.low == pytest.approx(0.21)
    assert dia.volume == 13_496_600
    assert resultado[DIA(2026, 11, 6)].close == pytest.approx(0.28)


def test_dia_antes_da_janela_fica_intacto() -> None:
    rows = _serie_raizen_em_lote()

    resultado = {r.date: r for r in aplicar_fator_cotacao(rows, FATORES_COTACAO_YAHOO, "RAIZ4")}

    assert resultado[DIA(2026, 10, 30)] == rows[0]


def test_com_fim_definido_o_dia_depois_fica_intacto() -> None:
    regra = {
        "RAIZ4": FatorCotacaoYahoo(
            codigo_b3="RAIZ55", fator=100, inicio=DIA(2026, 11, 3), fim=DIA(2026, 11, 4)
        )
    }
    rows = [
        _linha(DIA(2026, 10, 30), 0.30),
        _linha(DIA(2026, 11, 3), 27.10, volume=13_496_600),
        _linha(DIA(2026, 11, 4), 26.00, volume=9_000_000),
        _linha(DIA(2026, 11, 5), 28.00, volume=7_000_000),
    ]

    resultado = {r.date: r for r in aplicar_fator_cotacao(rows, regra, "RAIZ4")}

    assert resultado[DIA(2026, 11, 3)].close == pytest.approx(0.271)
    assert resultado[DIA(2026, 11, 4)].close == pytest.approx(0.26)
    assert resultado[DIA(2026, 11, 5)] == rows[3]


def test_serie_ja_por_acao_nao_muda_e_avisa(caplog: pytest.LogCaptureFixture) -> None:
    rows = [
        _linha(DIA(2026, 10, 30), 0.30),
        _linha(DIA(2026, 11, 3), 0.271, volume=13_496_600),
        _linha(DIA(2026, 11, 6), 0.28),
    ]

    with caplog.at_level(logging.WARNING, logger="tcc_scraper.collectors.prices_yahoo"):
        resultado = aplicar_fator_cotacao(rows, FATORES_COTACAO_YAHOO, "RAIZ4")

    assert resultado is rows
    assert "ja por acao" in caplog.text


def test_sem_pregao_antes_da_janela_divide_mesmo_assim() -> None:
    rows = [_linha(DIA(2026, 11, 3), 27.10), _linha(DIA(2026, 11, 6), 28.00)]

    resultado = aplicar_fator_cotacao(rows, FATORES_COTACAO_YAHOO, "RAIZ4")

    assert [r.close for r in resultado] == [pytest.approx(0.271), pytest.approx(0.28)]


def test_none_continua_none() -> None:
    rows = [
        _linha(DIA(2026, 10, 30), 0.30),
        PriceRow(
            ticker="RAIZ4", date=DIA(2026, 11, 3), open=None, high=None, low=None,
            close=27.10, adj_close=None, volume=None,
        ),
    ]

    dia = aplicar_fator_cotacao(rows, FATORES_COTACAO_YAHOO, "RAIZ4")[1]

    assert dia.open is None
    assert dia.adj_close is None
    assert dia.volume is None
    assert dia.close == pytest.approx(0.271)


def test_ticker_sem_regra_nao_mexe_na_serie() -> None:
    rows = [
        PriceRow(
            ticker="PETR4", date=DIA(2026, 11, 3), open=27.10, high=27.10, low=27.10,
            close=27.10, adj_close=27.10, volume=1000,
        )
    ]

    assert aplicar_fator_cotacao(rows, FATORES_COTACAO_YAHOO) is rows
    assert aplicar_fator_cotacao(rows, FATORES_COTACAO_YAHOO, ticker="PETR4") is rows


def test_lista_vazia_devolve_vazia() -> None:
    assert aplicar_fator_cotacao([], FATORES_COTACAO_YAHOO, ticker="RAIZ4") == []
    assert aplicar_fator_cotacao([], FATORES_COTACAO_YAHOO) == []
