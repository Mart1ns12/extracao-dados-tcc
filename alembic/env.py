"""Ambiente do Alembic.

A URL do banco vem do .env via Settings, nao do alembic.ini: assim existe uma unica
fonte de verdade e nenhuma credencial e commitada.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from tcc_scraper.config import get_settings
from tcc_scraper.db.models import Base, tabela_do_projeto

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().effective_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Restringe o autogenerate as tabelas do projeto.

    O filtro e por prefixo de camada: sem ele, o autogenerate tentaria derrubar
    qualquer tabela alheia que exista no mesmo database -- cenario real quando o
    MySQL do servidor e compartilhado com outra aplicacao.
    """
    if type_ == "table":
        return tabela_do_projeto(name)
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
