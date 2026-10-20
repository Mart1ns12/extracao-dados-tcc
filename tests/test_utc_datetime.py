"""O timestamp precisa voltar identico do MySQL, com fuso UTC e microssegundos.

Importa porque a agregacao diaria converte `published_at` para America/Sao_Paulo antes
de extrair a data. Se o banco devolvesse o horario sem fuso (ou com outro fuso), uma
materia das 22h cairia em dias diferentes -- e o dataset mudaria sem ninguem mexer
no codigo.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select
from sqlalchemy.orm import Session

from tcc_scraper.db.models import UTCDateTime


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
