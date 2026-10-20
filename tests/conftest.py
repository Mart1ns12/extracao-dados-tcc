"""Fixtures compartilhadas da suite.

O projeto tem UM banco: o MySQL 8 do .env. Nao existe banco de teste separado, entao os
testes que gravam no banco sao OPT-IN: sem TCC_TESTES_COM_BANCO=1 sao pulados, e com a
variavel ligada escrevem no banco real. Por isso cada teste limpa so as linhas que criou
(prefixos TESTE-, REGR, t27., ZZZZ9 e similares), antes e depois.

Testes sem banco (a maioria) nao dependem disto e rodam sem rede nem MySQL.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import Engine

from tcc_scraper.db.session import get_engine

MOTIVO_DESLIGADO = (
    "teste com banco desligado (defina TCC_TESTES_COM_BANCO=1; usa o MySQL do .env)"
)


def banco_habilitado() -> bool:
    return os.environ.get("TCC_TESTES_COM_BANCO") == "1"


@pytest.fixture
def engine() -> Engine:
    """Engine do MySQL do .env. Pula o teste se TCC_TESTES_COM_BANCO nao estiver ligada."""
    if not banco_habilitado():
        pytest.skip(MOTIVO_DESLIGADO)
    return get_engine()
