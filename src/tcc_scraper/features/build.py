"""Montagem do dataset v1 (14 colunas) a partir do banco, sem Typer.

Chamado pelo build-dataset do cli e pelo orquestrador. Levanta SemPrecos em vez
de sair do processo, para quem chama decidir o que fazer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select

from tcc_scraper.config import BENCHMARK_TICKER, IBOV_TICKERS, get_settings
from tcc_scraper.db.models import (
    Article,
    ArticleSentiment,
    ArticleTicker,
    DatasetV1,
    Price,
)
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.export.writers import write_dataset
from tcc_scraper.features.dataset import DATASET_COLUMNS, assemble_dataset, drop_unlabeled


@dataclass
class ResultadoBuild:
    dataset: pd.DataFrame
    linhas_banco: int
    arquivos: dict[str, Path]


class SemPrecos(RuntimeError):
    """Nao ha precos no banco: rode collect-prices antes."""


def carregar_entradas_v1() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Precos (ticker, date, adj_close, volume) e artigos pontuados por ticker."""
    with session_scope() as session:
        precos = pd.DataFrame(
            session.execute(
                select(Price.ticker, Price.date, Price.adj_close, Price.volume)
            ).all(),
            columns=["ticker", "date", "adj_close", "volume"],
        )
        artigos = pd.DataFrame(
            session.execute(
                select(
                    Article.id, ArticleTicker.ticker, Article.published_at,
                    ArticleSentiment.prob_pos, ArticleSentiment.prob_neu,
                    ArticleSentiment.prob_neg,
                )
                .join(ArticleTicker, ArticleTicker.article_id == Article.id)
                .join(ArticleSentiment, ArticleSentiment.article_id == Article.id)
            ).all(),
            columns=["article_id", "ticker", "published_at", "prob_pos", "prob_neu", "prob_neg"],
        )
    return precos, artigos


def persistir_dataset(dataset: pd.DataFrame) -> int:
    """Materializa o dataset em features_dataset_v1.

    Upsert por (data, ticker): remontar o dataset com outra regra de feature
    sobrescreve as linhas em vez de duplicar, e a tabela reflete sempre a versao
    mais recente. Linhas de uma execucao anterior que nao aparecem mais (por exemplo,
    ao encurtar a janela) NAO sao removidas -- use `--no-persist` e limpe a tabela a
    mao se quiser um recorte exato.

    NaN do pandas vira NULL no banco: `positivo` nulo significa "nao medido", e
    gravar 0 afirmaria sentimento neutro observado.
    """
    if dataset.empty:
        return 0

    linhas = dataset.replace({np.nan: None}).to_dict("records")
    atualizaveis = [c for c in DATASET_COLUMNS if c not in ("data", "ticker")]
    with session_scope() as session:
        return bulk_upsert(
            session,
            DatasetV1.__table__,
            linhas,
            index_elements=["data", "ticker"],
            update_columns=atualizaveis,
        )


def build_dataset_v1(
    *, persist: bool = True, export: bool = True, keep_unlabeled: bool = False
) -> ResultadoBuild:
    """Monta o dataset v1; grava no banco e/ou exporta conforme as flags."""
    settings = get_settings()
    precos, artigos = carregar_entradas_v1()
    if precos.empty:
        raise SemPrecos("sem precos no banco -- rode collect-prices antes")

    # Universo explicito: raw_prices tambem guarda o benchmark e as series macro
    # (Brent, dolar), que nao sao observacoes do dataset.
    #
    # start=data_start recorta as linhas a janela do dataset; o preco anterior
    # (coletado a partir de price_start) fica so como aquecimento das janelas de
    # 20 pregoes, para que a primeira linha ja saia com retorno_20d preenchido.
    dataset = assemble_dataset(
        precos,
        artigos,
        benchmark=BENCHMARK_TICKER,
        universe=IBOV_TICKERS.keys(),
        start=settings.data_start,
        end=settings.data_end,
    )
    if not keep_unlabeled:
        dataset = drop_unlabeled(dataset)

    linhas_banco = persistir_dataset(dataset) if persist else 0
    arquivos = write_dataset(dataset, settings.export_dir) if export else {}
    return ResultadoBuild(dataset=dataset, linhas_banco=linhas_banco, arquivos=arquivos)
