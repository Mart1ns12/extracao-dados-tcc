"""Exportacao do dataset final.

Parquet e a entrega principal: preserva tipos (datas continuam datas, nulos
continuam nulos), comprime bem e e lido nativamente por pandas, polars e sklearn.
O CSV vai junto por conveniencia -- para abrir no Excel e conferir a olho.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from tcc_scraper.features.dataset import DATASET_COLUMNS

logger = logging.getLogger(__name__)


def write_dataset(
    dataset: pd.DataFrame,
    output_dir: Path,
    stem: str = "dataset_v1",
    write_csv: bool = True,
) -> dict[str, Path]:
    """Grava o dataset em Parquet (e CSV) e devolve os caminhos escritos."""
    if list(dataset.columns) != DATASET_COLUMNS:
        raise ValueError(
            f"colunas fora do esperado.\nrecebido: {list(dataset.columns)}\n"
            f"esperado: {DATASET_COLUMNS}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.date.today().isoformat()

    paths: dict[str, Path] = {}
    parquet_path = output_dir / f"{stem}_{stamp}.parquet"
    dataset.to_parquet(parquet_path, index=False, compression="snappy")
    paths["parquet"] = parquet_path

    if write_csv:
        csv_path = output_dir / f"{stem}_{stamp}.csv"
        dataset.to_csv(csv_path, index=False, encoding="utf-8")
        paths["csv"] = csv_path

    logger.info("dataset exportado: %d linhas -> %s", len(dataset), output_dir)
    return paths


def summarize(dataset: pd.DataFrame) -> pd.DataFrame:
    """Relatorio de qualidade do dataset.

    Existe para que a etapa seguinte saiba o que recebeu: cobertura de noticias e
    proporcao de nulos por coluna determinam quais features sao utilizaveis.
    """
    total = len(dataset)
    if total == 0:
        return pd.DataFrame(columns=["coluna", "nulos", "pct_nulos"])

    linhas = [
        {
            "coluna": col,
            "nulos": int(dataset[col].isna().sum()),
            "pct_nulos": round(dataset[col].isna().mean() * 100, 2),
        }
        for col in dataset.columns
    ]
    return pd.DataFrame(linhas)
