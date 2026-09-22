"""Engine e sessao do SQLAlchemy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from tcc_scraper.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Engine unica do processo.

    pool_recycle existe por causa do MySQL: ele fecha conexoes ociosas por conta
    propria (wait_timeout, 8 h por padrao) e uma coleta de ~10 h esbarra nisso --
    a conexao morreria no meio sem o reciclo preventivo.
    """
    settings = get_settings()
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if settings.database_url.startswith(("mysql", "mariadb")):
        kwargs["pool_recycle"] = 3600
    return create_engine(settings.database_url, **kwargs)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sessao transacional: commit no sucesso, rollback em qualquer excecao."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dialect_name() -> str:
    """Dialeto em uso ('postgresql' ou 'mysql'), derivado da DATABASE_URL."""
    return get_engine().dialect.name
