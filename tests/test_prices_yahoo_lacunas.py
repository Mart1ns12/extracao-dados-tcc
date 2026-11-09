"""completar_lacunas: pregoes que o Yahoo nao tem entram com o preco da B3 na escala do Yahoo."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import replace

import pytest

from tcc_scraper.collectors.b3_cotahist import _gravar_lacunas, carregar_lacunas
from tcc_scraper.collectors.prices_yahoo import (
    LacunaPreco,
    PriceRow,
    completar_lacunas,
    razao_mediana,
)

DIA = dt.date


def _yahoo(
    data: dt.date,
    ticker: str,
    o: float,
    h: float,
    lo: float,
    c: float,
    adj: float | None = None,
    volume: int = 1000,
) -> PriceRow:
    return PriceRow(
        ticker=ticker, date=data, open=o, high=h, low=lo, close=c,
        adj_close=c if adj is None else adj, volume=volume,
    )


# BPAC11 (units): o close do Yahoo nao e o oficial, mas O/H/L batem com a B3 x 0,25.
YAHOO_19_11 = _yahoo(DIA(2020, 11, 19), "BPAC11", 20.575, 20.815, 20.325, 20.458)
# 23/11 construido com a mesma razao 0,25 sobre os precos da B3 do mesmo dia.
YAHOO_23_11 = _yahoo(DIA(2020, 11, 23), "BPAC11", 20.00, 20.25, 19.75, 20.10, adj=19.60)


def _lacuna_bpac() -> LacunaPreco:
    """Pregao 20/11 sem Yahoo; vizinhos 19/11 (antes) e 23/11 (depois), precos da B3."""
    return LacunaPreco(
        ticker="BPAC11",
        date=DIA(2020, 11, 20),
        codneg="BPAC11",
        open=81.00,
        high=82.00,
        low=80.00,
        close=81.50,
        volume=4_000_000,
        fonte="b3_cotahist",
        ref_anterior=DIA(2020, 11, 19),
        ant=(82.30, 83.26, 81.30, 82.36),
        ref_seguinte=DIA(2020, 11, 23),
        seg=(80.00, 81.00, 79.00, 80.40),
    )


def test_razao_mediana_das_units_e_025_nos_dois_vizinhos() -> None:
    antes = razao_mediana(
        (YAHOO_19_11.open, YAHOO_19_11.high, YAHOO_19_11.low, YAHOO_19_11.close),
        (82.30, 83.26, 81.30, 82.36),
    )
    depois = razao_mediana(
        (YAHOO_23_11.open, YAHOO_23_11.high, YAHOO_23_11.low, YAHOO_23_11.close),
        (80.00, 81.00, 79.00, 80.40),
    )

    assert antes == pytest.approx(0.25, rel=1e-4)
    assert depois == pytest.approx(0.25, rel=1e-4)


def test_razao_mediana_sem_pares_validos_e_none() -> None:
    assert razao_mediana((None, None), (1.0, 2.0)) is None
    assert razao_mediana((1.0, 2.0), (0.0, None)) is None


def test_units_completam_o_pregao_na_escala_025_do_yahoo() -> None:
    resultado = completar_lacunas(
        [YAHOO_19_11, YAHOO_23_11], {"BPAC11": [_lacuna_bpac()]}, ticker="BPAC11"
    )

    dia = {r.date: r for r in resultado}[DIA(2020, 11, 20)]
    assert dia.open == pytest.approx(20.25)
    assert dia.high == pytest.approx(20.50)
    assert dia.low == pytest.approx(20.00)
    assert dia.close == pytest.approx(20.375)
    assert dia.volume == 16_000_000
    assert dia.source == "b3_cotahist"


def test_adj_close_usa_o_fator_do_pregao_seguinte() -> None:
    resultado = completar_lacunas(
        [YAHOO_19_11, YAHOO_23_11], {"BPAC11": [_lacuna_bpac()]}, ticker="BPAC11"
    )

    dia = {r.date: r for r in resultado}[DIA(2020, 11, 20)]
    assert dia.adj_close == pytest.approx(20.375 * 19.60 / 20.10)


def test_desdobramento_b3sa3_razao_um_terco_e_volume_triplica() -> None:
    yahoo = [
        _yahoo(DIA(2020, 11, 19), "B3SA3", 4.00, 4.00, 4.00, 4.00),
        _yahoo(DIA(2020, 11, 23), "B3SA3", 4.10, 4.10, 4.10, 4.10),
    ]
    lacuna = replace(
        _lacuna_bpac(),
        ticker="B3SA3",
        codneg="B3SA3",
        open=12.00,
        high=12.00,
        low=12.00,
        close=12.60,
        volume=1_000_000,
        ant=(12.00, 12.00, 12.00, 12.00),
        seg=(12.30, 12.30, 12.30, 12.30),
    )

    resultado = completar_lacunas(yahoo, {"B3SA3": [lacuna]}, ticker="B3SA3")

    dia = {r.date: r for r in resultado}[DIA(2020, 11, 20)]
    assert dia.close == pytest.approx(4.20)
    assert dia.open == pytest.approx(4.00)
    assert dia.volume == 3_000_000


def test_data_que_o_yahoo_ja_tem_nao_muda() -> None:
    rows = [
        YAHOO_19_11,
        _yahoo(DIA(2020, 11, 20), "BPAC11", 20.30, 20.40, 20.20, 20.35),
        YAHOO_23_11,
    ]

    assert completar_lacunas(rows, {"BPAC11": [_lacuna_bpac()]}, ticker="BPAC11") is rows


def test_falta_vizinho_no_yahoo_pula_com_aviso(caplog: pytest.LogCaptureFixture) -> None:
    # 20/11 dentro do periodo coletado, mas sem o 23/11 (vizinho seguinte da lacuna).
    rows = [YAHOO_19_11, _yahoo(DIA(2020, 11, 24), "BPAC11", 20.0, 20.2, 19.8, 20.1)]

    with caplog.at_level(logging.WARNING, logger="tcc_scraper.collectors.prices_yahoo"):
        resultado = completar_lacunas(rows, {"BPAC11": [_lacuna_bpac()]}, ticker="BPAC11")

    assert resultado is rows
    assert "lacuna pulada" in caplog.text


def test_lacuna_fora_do_periodo_coletado_e_ignorada_sem_aviso(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Coleta incremental do daily: so pregoes recentes; a lacuna de 2020 nao e desta busca.
    rows = [_yahoo(DIA(2026, 11, 5), "BPAC11", 40.0, 40.5, 39.5, 40.2)]

    with caplog.at_level(logging.WARNING, logger="tcc_scraper.collectors.prices_yahoo"):
        resultado = completar_lacunas(rows, {"BPAC11": [_lacuna_bpac()]}, ticker="BPAC11")

    assert resultado is rows
    assert "lacuna" not in caplog.text


def test_razoes_instaveis_pulam_com_aviso(caplog: pytest.LogCaptureFixture) -> None:
    # 23/11 com razao 0,2525 contra 0,25 do dia anterior: 1% de diferenca, acima de 0,3%.
    seguinte_instavel = _yahoo(DIA(2020, 11, 23), "BPAC11", 20.20, 20.4525, 19.9475, 20.301)
    rows = [YAHOO_19_11, seguinte_instavel]

    with caplog.at_level(logging.WARNING, logger="tcc_scraper.collectors.prices_yahoo"):
        resultado = completar_lacunas(rows, {"BPAC11": [_lacuna_bpac()]}, ticker="BPAC11")

    assert resultado is rows
    assert "instavel" in caplog.text


def test_indice_sem_referencia_entra_com_o_preco_da_b3() -> None:
    lacuna = LacunaPreco(
        ticker="^BVSP",
        date=DIA(2020, 11, 20),
        codneg="IBOV",
        open=99_000.0,
        high=100_000.0,
        low=98_500.0,
        close=99_500.0,
        volume=5_000_000,
        fonte="b3",
    )

    resultado = completar_lacunas([], {"^BVSP": [lacuna]}, ticker="^BVSP")

    assert resultado == [
        PriceRow(
            ticker="^BVSP",
            date=DIA(2020, 11, 20),
            open=99_000.0,
            high=100_000.0,
            low=98_500.0,
            close=99_500.0,
            adj_close=99_500.0,
            volume=5_000_000,
            source="b3",
        )
    ]


def test_ticker_sem_lacuna_nao_mexe_na_serie() -> None:
    rows = [YAHOO_19_11]

    assert completar_lacunas(rows, {"BPAC11": [_lacuna_bpac()]}, ticker="PETR4") is rows


def test_ida_e_volta_pelo_csv(tmp_path) -> None:
    indice = LacunaPreco(
        ticker="^BVSP",
        date=DIA(2020, 11, 20),
        codneg="IBOV",
        open=99_000.0,
        high=100_000.0,
        low=98_500.0,
        close=99_500.0,
        volume=5_000_000,
        fonte="b3",
    )
    _gravar_lacunas(tmp_path / "lacunas.csv", [_lacuna_bpac(), indice])

    lidas = carregar_lacunas(tmp_path)

    assert lidas == {"BPAC11": [_lacuna_bpac()], "^BVSP": [indice]}


def test_pasta_ausente_devolve_vazio(tmp_path) -> None:
    assert carregar_lacunas(tmp_path / "nao_existe") == {}
