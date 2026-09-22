"""Montagem do Dataset 1.0: junta precos, sentimento e target nas 14 colunas."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable

import pandas as pd

from tcc_scraper.features.news_agg import aggregate_news
from tcc_scraper.features.returns import compute_price_features, compute_target

logger = logging.getLogger(__name__)

# Ordem exata de docs/Dataset1.0.xlsx. O consumidor a jusante depende dela.
DATASET_COLUMNS = [
    "data",
    "ticker",
    "quant_noticias",
    "positivo",
    "neutro",
    "negativo",
    "score_finbert",
    "retorno_1d",
    "retorno_5d",
    "retorno_20d",
    "volatilidade_20d",
    "volume_relativo",
    "retorno_ibovespa",
    "target",
]


def is_equity_symbol(ticker: str) -> bool:
    """True se o simbolo e uma acao, nao um indice ou serie macro.

    Simbolos do Yahoo que nao sao acoes carregam marcadores no proprio codigo:
    `^` para indices (^BVSP) e `=` para futuros e cambio (BZ=F, USDBRL=X).
    """
    return not (ticker.startswith("^") or "=" in ticker)


def assemble_dataset(
    prices: pd.DataFrame,
    articles: pd.DataFrame,
    benchmark: str = "^BVSP",
    universe: Iterable[str] | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> pd.DataFrame:
    """Produz o dataset final.

    prices: ticker, date, adj_close, volume -- acoes, benchmark E series macro juntos.
    articles: article_id, ticker, published_at, prob_pos, prob_neu, prob_neg.
    universe: quais tickers viram linha. Se omitido, aceita todo simbolo de acao.
    start/end: recorte das LINHAS do dataset. As features continuam sendo calculadas
        sobre todo o historico recebido.

    Sobre o universo: a versao anterior inferia os ativos por exclusao ("tudo menos o
    benchmark"), e no piloto isso fez Brent (BZ=F) e dolar (USDBRL=X) virarem linhas do
    dataset -- inclusive inflando os nulos de volume_relativo, ja que cambio nao tem
    volume. Inferir por exclusao e fragil: qualquer serie nova em raw_prices viraria
    uma "acao". Agora a inclusao e explicita.

    Sobre a janela: o recorte e aplicado NO FIM, depois das features. O preco anterior
    a `start` vira aquecimento -- sem ele, os 20 primeiros pregoes do dataset sairiam
    sem retorno_20d, volatilidade_20d e volume_relativo, porque a janela de 20 pregoes
    nao teria historico. Como ha 2 anos de preco coletados e o dataset cobre 1 ano, o
    excedente cobre o aquecimento sem custo nenhum.
    """
    features = compute_price_features(prices)

    bench = features[features["ticker"] == benchmark]
    if bench.empty:
        raise ValueError(f"benchmark {benchmark} ausente em prices: sem ele nao ha target")

    if universe is not None:
        permitidos = set(universe)
        acoes = features[features["ticker"].isin(permitidos)]
    else:
        acoes = features[features["ticker"].map(is_equity_symbol)]

    acoes = acoes[acoes["ticker"] != benchmark]
    if acoes.empty:
        return pd.DataFrame(columns=DATASET_COLUMNS)

    com_target = compute_target(acoes, bench)

    pregoes = com_target[["ticker", "date"]]
    noticias = aggregate_news(articles, pregoes)

    out = com_target.merge(noticias, on=["ticker", "date"], how="left")
    out["quant_noticias"] = out["quant_noticias"].fillna(0).astype(int)
    out = out.rename(columns={"date": "data"})

    faltando = [c for c in DATASET_COLUMNS if c not in out.columns]
    if faltando:
        raise ValueError(f"colunas ausentes na montagem: {faltando}")

    # Recorte por ultimo: tudo antes de `start` ja cumpriu seu papel de aquecimento
    # nas janelas de 20 pregoes.
    if start is not None:
        out = out[out["data"] >= start]
    if end is not None:
        out = out[out["data"] <= end]

    return out[DATASET_COLUMNS].sort_values(["data", "ticker"]).reset_index(drop=True)


def drop_unlabeled(dataset: pd.DataFrame) -> pd.DataFrame:
    """Remove linhas sem target (o ultimo pregao de cada ticker).

    Separado de assemble_dataset de proposito: para inspecionar o dataset completo
    e util ver as linhas sem rotulo; para treinar, nao.
    """
    antes = len(dataset)
    out = dataset[dataset["target"].notna()].copy()
    out["target"] = out["target"].astype(int)
    logger.info("descartadas %d linhas sem target (ultimo pregao)", antes - len(out))
    return out
