"""Features de preco e o target.

Toda janela aqui usa apenas o passado. Isso nao e estilo: a nota 6 de
docs/fontes_dataset.xlsx exige que cada linha contenha somente informacao disponivel
na data da observacao. Janelas centradas (center=True) ou shift negativo
introduziriam vazamento e inflariam artificialmente o resultado do modelo.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

RETURN_WINDOWS = (1, 5, 20)
VOL_WINDOW = 20
VOLUME_WINDOW = 20


def compute_price_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Calcula retornos, volatilidade e volume relativo por ticker.

    Espera as colunas ticker, date, adj_close, volume. Devolve o mesmo indice com
    as features anexadas.
    """
    out = prices.sort_values(["ticker", "date"]).reset_index(drop=True).copy()
    grupo = out.groupby("ticker", sort=False)["adj_close"]

    for janela in RETURN_WINDOWS:
        # pct_change com periods=n olha n pregoes para tras -- nunca para frente.
        out[f"retorno_{janela}d"] = grupo.pct_change(periods=janela)

    retorno_diario = grupo.pct_change(periods=1)
    out["_retorno_diario"] = retorno_diario

    # ddof=1 (desvio amostral): a janela e uma amostra dos retornos, nao a populacao.
    out["volatilidade_20d"] = (
        out.groupby("ticker", sort=False)["_retorno_diario"]
        .rolling(window=VOL_WINDOW, min_periods=VOL_WINDOW)
        .std(ddof=1)
        .reset_index(level=0, drop=True)
    )

    # shift(1) antes da media: a media de 20 pregoes ANTERIORES, sem incluir o dia.
    # Incluir o proprio dia diluiria justamente o pico que a feature quer detectar.
    media_volume = (
        out.groupby("ticker", sort=False)["volume"]
        .shift(1)
        .groupby(out["ticker"], sort=False)
        .rolling(window=VOLUME_WINDOW, min_periods=VOLUME_WINDOW)
        .mean()
        .reset_index(level=0, drop=True)
    )
    out["volume_relativo"] = np.where(
        (media_volume > 0) & media_volume.notna(), out["volume"] / media_volume, np.nan
    )

    return out.drop(columns=["_retorno_diario"])


def compute_target(stock: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    """Target binario: 1 se a acao superar o benchmark no pregao SEGUINTE.

    E o unico ponto do pipeline que olha o futuro, e de proposito: e a variavel a
    ser prevista. O shift(-1) fica isolado aqui para que qualquer auditoria do
    codigo encontre um unico lugar onde t+1 e acessado.
    """
    acao = stock.sort_values(["ticker", "date"]).reset_index(drop=True).copy()

    bench = (
        benchmark[["date", "retorno_1d"]]
        .rename(columns={"retorno_1d": "_bench_ret"})
        .drop_duplicates(subset="date")
    )
    acao = acao.merge(bench, on="date", how="left")

    # Retorno de t+1, tanto da acao quanto do benchmark.
    ret_acao_amanha = acao.groupby("ticker", sort=False)["retorno_1d"].shift(-1)
    ret_bench_amanha = acao.groupby("ticker", sort=False)["_bench_ret"].shift(-1)

    comparavel = ret_acao_amanha.notna() & ret_bench_amanha.notna()
    acao["target"] = np.where(
        comparavel, (ret_acao_amanha > ret_bench_amanha).astype("float"), np.nan
    )
    # Retorno do benchmark NA data da observacao (nao em t+1): e uma feature.
    acao["retorno_ibovespa"] = acao["_bench_ret"]

    return acao.drop(columns=["_bench_ret"])
