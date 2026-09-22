"""Agregacao diaria de noticias e sentimento por (data, ticker).

A janela e "as 24 h anteriores a observacao", conforme docs/fontes_dataset.xlsx.
Implementada como o dia de pregao em horario de Brasilia: uma noticia publicada as
23h do dia 02 pertence ao dia 02, nao ao 03.
"""

from __future__ import annotations

import pandas as pd

SAO_PAULO = "America/Sao_Paulo"

AGG_COLUMNS = ["quant_noticias", "positivo", "neutro", "negativo", "score_finbert"]


def aggregate_news(articles: pd.DataFrame, trading_days: pd.DataFrame) -> pd.DataFrame:
    """Agrega sentimento por (ticker, data).

    articles: article_id, ticker, published_at (tz-aware), prob_pos, prob_neu, prob_neg.
    trading_days: ticker, date -- as linhas que o dataset precisa ter.

    Pregoes sem noticia recebem quant_noticias = 0 e sentimento nulo. Preencher com
    zero seria afirmar "sentimento neutro medido", que e diferente de "nao medido";
    a decisao de imputar cabe a etapa seguinte do projeto.
    """
    base = trading_days[["ticker", "date"]].drop_duplicates().reset_index(drop=True)

    if articles.empty:
        vazio = base.copy()
        vazio["quant_noticias"] = 0
        for col in ("positivo", "neutro", "negativo", "score_finbert"):
            vazio[col] = pd.NA
        return vazio

    df = articles.copy()
    published = pd.to_datetime(df["published_at"], utc=True)
    # Converte para horario local antes de extrair a data: o pregao e brasileiro.
    df["date"] = published.dt.tz_convert(SAO_PAULO).dt.date

    # Uma mesma materia pode ter sido pontuada por mais de um modelo; conta uma vez.
    df = df.drop_duplicates(subset=["article_id", "ticker", "date"])

    agrupado = (
        df.groupby(["ticker", "date"], sort=False)
        .agg(
            quant_noticias=("article_id", "nunique"),
            positivo=("prob_pos", "mean"),
            neutro=("prob_neu", "mean"),
            negativo=("prob_neg", "mean"),
        )
        .reset_index()
    )
    agrupado["score_finbert"] = agrupado["positivo"] - agrupado["negativo"]

    out = base.merge(agrupado, on=["ticker", "date"], how="left")
    out["quant_noticias"] = out["quant_noticias"].fillna(0).astype(int)
    return out
