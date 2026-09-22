"""O timestamp precisa voltar identico do PostgreSQL e do MySQL.

Importa porque a agregacao diaria converte `published_at` para America/Sao_Paulo antes
de extrair a data. Se o MySQL devolvesse o horario sem fuso e o Postgres com, uma
materia das 22h cairia em dias diferentes conforme o banco -- e o dataset mudaria de
conteudo sem ninguem mexer no codigo.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import cache

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from sqlalchemy.orm import Session

from tcc_scraper.db.models import UTCDateTime

PG_URL = os.environ.get(
    "TEST_PG_URL", "postgresql+psycopg://tcc:tcc_local_dev@localhost:5433/tcc_scraper"
)
MYSQL_URL = os.environ.get(
    "TEST_MYSQL_URL", "mysql+pymysql://root@127.0.0.1:3307/tcc_scraper"
)


# Timeout curto e resultado em cache, de proposito. Com o timeout padrao do driver
# (30 s+) e uma tentativa por teste, a suite travava por dez minutos so para concluir
# que um banco opcional nao estava no ar -- foi o que aconteceu apos um reinicio da
# maquina. Com cache, a conclusao e tirada UMA vez por URL.
_CONNECT_TIMEOUT = 2


@cache
def _tentar_engine(url: str):
    """Devolve (engine, None) se conectou, ou (None, motivo). Avaliado uma vez por URL."""
    args = (
        {"connect_timeout": _CONNECT_TIMEOUT}
        if url.startswith(("mysql", "postgresql"))
        else {}
    )
    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args=args)
        with engine.connect():
            pass
        return engine, None
    except Exception as exc:
        return None, str(exc)[:80]


def _engine_or_skip(url: str, label: str):
    engine, motivo = _tentar_engine(url)
    if engine is None:
        pytest.skip(f"{label} indisponivel: {motivo}")
    return engine


@pytest.fixture(params=["postgresql", "mysql"])
def engine(request):
    if request.param == "postgresql":
        return _engine_or_skip(PG_URL, "PostgreSQL")
    return _engine_or_skip(MYSQL_URL, "MySQL")


@pytest.fixture
def tabela(engine):
    meta = MetaData()
    t = Table(
        "tz_probe", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("quando", UTCDateTime, nullable=False),
    )
    meta.drop_all(engine)
    meta.create_all(engine)
    yield t
    meta.drop_all(engine)


def _round_trip(engine, tabela, valor: dt.datetime) -> dt.datetime:
    with Session(engine) as s:
        s.execute(tabela.insert().values(quando=valor))
        s.commit()
        return s.execute(select(tabela.c.quando)).scalar_one()


def test_volta_sempre_com_fuso(engine, tabela) -> None:
    lido = _round_trip(engine, tabela, dt.datetime(2024, 6, 3, 14, 30, tzinfo=dt.UTC))
    assert lido.tzinfo is not None, "sem fuso, a conversao para horario de Brasilia erra"


def test_preserva_o_instante(engine, tabela) -> None:
    original = dt.datetime(2024, 6, 3, 14, 30, 45, tzinfo=dt.UTC)
    assert _round_trip(engine, tabela, original) == original


def test_converte_fuso_nao_utc_para_utc(engine, tabela) -> None:
    """22h em Brasilia (UTC-3) e 01h do dia seguinte em UTC."""
    brasilia = dt.timezone(dt.timedelta(hours=-3))
    original = dt.datetime(2024, 6, 3, 22, 0, tzinfo=brasilia)

    lido = _round_trip(engine, tabela, original)

    assert lido == original, "o instante deve ser o mesmo"
    assert lido.utcoffset() == dt.timedelta(0), "deve voltar normalizado em UTC"
    assert (lido.year, lido.month, lido.day, lido.hour) == (2024, 6, 4, 1)


def test_datetime_naive_e_assumido_utc(engine, tabela) -> None:
    """Defesa: um naive que escape para o banco nao pode virar horario local."""
    lido = _round_trip(engine, tabela, dt.datetime(2024, 6, 3, 14, 30))
    assert lido == dt.datetime(2024, 6, 3, 14, 30, tzinfo=dt.UTC)


def test_preserva_microssegundos(engine, tabela) -> None:
    """Sem fsp=6 o MySQL truncaria para o segundo, criando empates falsos."""
    original = dt.datetime(2024, 6, 3, 14, 30, 45, 123456, tzinfo=dt.UTC)
    assert _round_trip(engine, tabela, original).microsecond == 123456
