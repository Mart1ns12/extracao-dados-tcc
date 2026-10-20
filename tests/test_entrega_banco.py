"""build_entrega(tabelas=["indices"]) contra PostgreSQL e MySQL reais (skip se nao estiver no ar).

Padrao de tests/test_orquestrador_banco.py: o session_scope aponta para o engine de teste
(TEST_PG_URL / TEST_MYSQL_URL). Os dados de teste usam datas em 2099 e sao removidos.
Os dois dialetos devem gravar exatamente o mesmo conteudo para as mesmas linhas de origem.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import cache

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from tcc_scraper.db import session as session_mod
from tcc_scraper.db.models import Macro, Price
from tcc_scraper.db.models_entrega import Indice
from tcc_scraper.entrega.builder import build_entrega

PG_URL = os.environ.get(
    "TEST_PG_URL", "postgresql+psycopg://tcc:tcc_local_dev@localhost:5433/tcc_scraper"
)
MYSQL_URL = os.environ.get(
    "TEST_MYSQL_URL", "mysql+pymysql://root@127.0.0.1:3307/tcc_scraper"
)
_CONNECT_TIMEOUT = 2
INICIO = dt.date(2099, 1, 1)
ESPERADO = [
    (dt.date(2099, 1, 1), "IPCA", 0.26),
    (dt.date(2099, 1, 2), "CDI", 0.0551),
    (dt.date(2099, 1, 2), "IDIV", 5123.5),
]


@cache
def _tentar_engine(url: str):
    args = {"connect_timeout": _CONNECT_TIMEOUT} if url.startswith(("mysql", "postgresql")) else {}
    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args=args)
        with engine.connect():
            pass
        return engine, None
    except Exception as exc:
        return None, str(exc)[:80]


@pytest.fixture(params=["postgresql", "mysql"])
def engine(request):
    url, label = (PG_URL, "PostgreSQL") if request.param == "postgresql" else (MYSQL_URL, "MySQL")
    eng, motivo = _tentar_engine(url)
    if eng is None:
        pytest.skip(f"{label} indisponivel: {motivo}")
    return eng


def _limpar(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(Macro).where(Macro.series.in_(["cdi", "ipca"]), Macro.date >= INICIO))
        s.execute(delete(Price).where(Price.ticker == "IDIV", Price.date >= INICIO))
        s.execute(delete(Indice).where(Indice.data >= INICIO))
        s.commit()


@pytest.fixture
def dados(engine, monkeypatch):
    sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda: sm)
    _limpar(engine)
    with Session(engine) as s:
        s.add_all([
            Macro(series="cdi", date=dt.date(2099, 1, 2), value=0.0551),
            Macro(series="ipca", date=dt.date(2099, 1, 1), value=0.26),
            Price(ticker="IDIV", date=dt.date(2099, 1, 2), open=None, high=None, low=None,
                  close=5123.5, adj_close=None, volume=None, source="yahoo"),
        ])
        s.commit()
    yield engine
    _limpar(engine)


def _indices_de_teste(engine) -> list[tuple]:
    with Session(engine) as s:
        linhas = s.execute(
            select(Indice.data, Indice.indice, Indice.valor)
            .where(Indice.data >= INICIO)
            .order_by(Indice.data, Indice.indice)
        ).all()
    return [(d, i, float(v)) for d, i, v in linhas]


def test_indices_nao_duplica_e_grava_o_mesmo_conteudo(dados) -> None:
    engine = dados
    primeira = build_entrega(tabelas=["indices"])
    assert primeira["indices"] >= len(ESPERADO)
    assert _indices_de_teste(engine) == sorted(ESPERADO)

    segunda = build_entrega(tabelas=["indices"])
    assert segunda["indices"] == primeira["indices"]
    assert _indices_de_teste(engine) == sorted(ESPERADO)  # nada duplicado
