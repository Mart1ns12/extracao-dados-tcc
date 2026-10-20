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
    """Engine unica do processo, sempre no MySQL do .env.

    pool_recycle existe por causa do MySQL: ele fecha conexoes ociosas por conta
    propria (wait_timeout, 8 h por padrao) e uma coleta de ~10 h esbarra nisso --
    a conexao morreria no meio sem o reciclo preventivo.
    """
    return create_engine(
        get_settings().effective_url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )


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
