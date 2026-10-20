"""Escrita dos CSVs da entrega: virgula, UTF-8 sem BOM, None vira campo vazio."""

from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any


def _formatar(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float):
        if valor != valor:  # NaN
            return ""
        # repr da o menor texto que volta ao mesmo float; Decimal evita a notacao cientifica.
        return format(Decimal(repr(valor)), "f")
    if isinstance(valor, dt.date):
        return valor.isoformat()
    return str(valor)


def escrever_csv(nome: str, linhas: list[dict], colunas: list[str], pasta: Path) -> Path:
    """Grava `<pasta>/<nome>.csv` com o cabecalho `colunas` e uma linha por item de `linhas`."""
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / f"{nome}.csv"
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo, delimiter=",", quoting=csv.QUOTE_MINIMAL,
                              lineterminator="\n")
        escritor.writerow(colunas)
        for linha in linhas:
            escritor.writerow([_formatar(linha.get(c)) for c in colunas])
    return caminho
