"""Montagem do dataset v1 sem banco: erro sem precos e as 14 colunas.

O universo vem de IBOV_TICKERS e as datas de get_settings(); por isso o teste usa
um ticker real da carteira e ajusta a janela do settings, em vez de depender do .env.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tcc_scraper.config import get_settings
from tcc_scraper.features import build
from tcc_scraper.features.build import ResultadoBuild, SemPrecos, build_dataset_v1
from tcc_scraper.features.dataset import DATASET_COLUMNS

TICKER = "PETR4"
ARTIGOS_VAZIOS = pd.DataFrame(
    columns=["article_id", "ticker", "published_at", "prob_pos", "prob_neu", "prob_neg"]
)


def _serie(precos: list[float], ticker: str) -> pd.DataFrame:
    n = len(precos)
    datas = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame(
        {"ticker": ticker, "date": datas.date, "adj_close": precos, "volume": [1000] * n}
    )


def test_sem_precos_levanta_sem_tocar_no_banco(monkeypatch) -> None:
    precos_vazios = pd.DataFrame(columns=["ticker", "date", "adj_close", "volume"])
    monkeypatch.setattr(
        build, "carregar_entradas_v1", lambda: (precos_vazios, ARTIGOS_VAZIOS.copy())
    )

    with pytest.raises(SemPrecos, match="collect-prices"):
        build_dataset_v1(persist=False, export=False)


def test_monta_as_14_colunas_sem_gravar_nem_exportar(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "data_start", dt.date(2024, 1, 1))
    monkeypatch.setattr(settings, "data_end", dt.date(2030, 1, 1))
    precos = pd.concat(
        [
            _serie([100.0 + i for i in range(10)], TICKER),
            _serie([50.0 + i * 0.1 for i in range(10)], "^BVSP"),
        ],
        ignore_index=True,
    )
    monkeypatch.setattr(build, "carregar_entradas_v1", lambda: (precos, ARTIGOS_VAZIOS.copy()))

    resultado = build_dataset_v1(persist=False, export=False)

    assert isinstance(resultado, ResultadoBuild)
    assert resultado.linhas_banco == 0
    assert resultado.arquivos == {}
    assert len(DATASET_COLUMNS) == 14
    assert list(resultado.dataset.columns) == DATASET_COLUMNS
    assert set(resultado.dataset["ticker"]) == {TICKER}
    assert len(resultado.dataset) == 9, "o ultimo pregao sai sem target"
