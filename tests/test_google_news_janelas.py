"""Coletores da T5 (sem rede): janelas do Google News, universo do Yahoo e nomes do painel.

Nada aqui chama a internet nem grava no banco: so a montagem de unidades de trabalho,
o parse de um payload do Yahoo e a linha de comando.
"""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from tcc_scraper.cli import app
from tcc_scraper.collectors.google_news import GoogleNewsCollector, iter_weeks
from tcc_scraper.collectors.macro_bcb import BcbCollector
from tcc_scraper.collectors.prices_yahoo import YahooPriceCollector, parse_chart
from tcc_scraper.config import GOOGLE_NEWS_LOCALES, IVVB11
from tcc_scraper.enrich.article_fetcher import ArticleBodyCollector


def _chaves(coletor) -> list[str]:
    return [unit.key for unit in coletor.work_units()]


def test_chave_com_recorte_tem_quatro_partes() -> None:
    hoje = dt.date.today()
    with GoogleNewsCollector(
        tickers={"PETR4": "Petrobras"},
        desde=hoje - dt.timedelta(days=20),
        ate=hoje,
    ) as coletor:
        chaves = _chaves(coletor)

    assert chaves
    assert all(len(chave.split("|")) == 4 for chave in chaves)


def test_chave_sem_recorte_mantem_tres_partes() -> None:
    with GoogleNewsCollector(tickers={"PETR4": "Petrobras"}) as coletor:
        chaves = _chaves(coletor)

    assert chaves
    assert all(len(chave.split("|")) == 3 for chave in chaves)


def test_recorte_curto_gera_uma_janela_por_idioma() -> None:
    hoje = dt.date.today()
    desde, ate = hoje - dt.timedelta(days=2), hoje + dt.timedelta(days=1)

    assert list(iter_weeks(desde, ate)) == [(desde, ate)]

    with GoogleNewsCollector(tickers={"PETR4": "Petrobras"}, desde=desde, ate=ate) as coletor:
        unidades = list(coletor.work_units())

    assert len(unidades) == len(GOOGLE_NEWS_LOCALES)
    assert {(u.payload["start"], u.payload["end"]) for u in unidades} == {(desde, ate)}


def test_desde_sem_ate_e_recusado() -> None:
    with pytest.raises(ValueError):
        GoogleNewsCollector(tickers={"PETR4": "Petrobras"}, desde=dt.date(2026, 1, 1))


def test_collect_news_exige_desde_e_ate_juntos() -> None:
    resultado = CliRunner().invoke(app, ["collect-news", "--desde", "2026-01-01"])

    assert resultado.exit_code != 0


def test_yahoo_cobre_ivvb11_indices_e_universo_novo() -> None:
    tickers = set(YahooPriceCollector().tickers)

    assert {IVVB11, "IDIV", "CRFB3"} <= tickers


def test_indice_sem_adjclose_usa_o_close() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1_700_000_000],
                    "indicators": {
                        "quote": [
                            {
                                "open": [123.0],
                                "high": [124.0],
                                "low": [122.0],
                                "close": [123.5],
                                "volume": [10],
                            }
                        ]
                    },
                }
            ]
        }
    }

    linhas = parse_chart(payload, ticker="IDIV")

    assert len(linhas) == 1
    assert linhas[0].adj_close == linhas[0].close == 123.5


def test_nomes_do_painel() -> None:
    assert YahooPriceCollector.name == "yahoo"
    assert BcbCollector.name == "bcb"
    assert ArticleBodyCollector.name == "enriquecimento"
