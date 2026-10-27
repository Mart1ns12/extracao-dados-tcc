"""aplicar_correcoes: a serie da B3 substitui so a janela do Yahoo que esta corrompida."""

from __future__ import annotations

import datetime as dt

from tcc_scraper.collectors.prices_yahoo import PriceRow, aplicar_correcoes


def _yahoo(data: dt.date, close: float = 10.0, ticker: str = "AZUL3") -> PriceRow:
    return PriceRow(
        ticker=ticker, date=data, open=close, high=close, low=close, close=close,
        adj_close=close, volume=100,
    )


def _b3(data: dt.date, close: float = 1.0) -> PriceRow:
    return PriceRow(
        ticker="AZUL3", date=data, open=close, high=close, low=close, close=close,
        adj_close=close, volume=5, source="b3_cotahist",
    )


DIA = dt.date


def test_sem_correcao_devolve_as_linhas_intactas() -> None:
    rows = [_yahoo(DIA(2026, 1, 12)), _yahoo(DIA(2026, 1, 13))]

    assert aplicar_correcoes(rows, {}) is rows
    assert aplicar_correcoes(rows, {"PETR4": [_b3(DIA(2026, 1, 13))]}) is rows


def test_substitui_so_a_janela_da_correcao() -> None:
    rows = [
        _yahoo(DIA(2026, 1, 12), close=41.0),  # antes da janela: fica
        _yahoo(DIA(2026, 1, 13), close=41_000_000.0),  # na janela: sai
        _yahoo(DIA(2026, 1, 14), close=7.42),  # na janela, sem correcao: sai
        _yahoo(DIA(2026, 4, 17), close=28_000_000.0),  # na janela: sai
        _yahoo(DIA(2026, 4, 20), close=30.54),  # depois da janela: fica
    ]
    correcao = [_b3(DIA(2026, 1, 13), 464.06), _b3(DIA(2026, 4, 17), 32.25)]

    resultado = aplicar_correcoes(rows, {"AZUL3": correcao})

    assert [(r.date, r.close) for r in resultado] == [
        (DIA(2026, 1, 12), 41.0),
        (DIA(2026, 1, 13), 464.06),
        (DIA(2026, 4, 17), 32.25),
        (DIA(2026, 4, 20), 30.54),
    ]


def test_marca_a_origem_de_cada_linha() -> None:
    rows = [_yahoo(DIA(2026, 1, 12)), _yahoo(DIA(2026, 1, 13))]
    resultado = aplicar_correcoes(rows, {"AZUL3": [_b3(DIA(2026, 1, 13))]})

    assert [r.source for r in resultado] == ["yahoo", "b3_cotahist"]


def test_ordena_por_data_mesmo_com_correcao_no_meio() -> None:
    rows = [_yahoo(DIA(2026, 4, 20)), _yahoo(DIA(2026, 1, 12))]
    correcao = [_b3(DIA(2026, 2, 18)), _b3(DIA(2026, 1, 13))]

    datas = [r.date for r in aplicar_correcoes(rows, {"AZUL3": correcao})]

    assert datas == sorted(datas)


def test_sem_linhas_do_yahoo_usa_o_ticker_informado() -> None:
    """Se o Yahoo nao trouxe nada, a correcao ainda entra: o ticker vem do argumento."""
    correcao = [_b3(DIA(2026, 1, 13))]

    resultado = aplicar_correcoes([], {"AZUL3": correcao}, ticker="AZUL3")

    assert resultado == correcao


def test_ticker_sem_correcao_nao_mexe_na_serie() -> None:
    rows = [_yahoo(DIA(2026, 1, 13), ticker="PETR4")]
    correcoes = {"AZUL3": [_b3(DIA(2026, 1, 13))]}

    assert aplicar_correcoes(rows, correcoes) is rows
