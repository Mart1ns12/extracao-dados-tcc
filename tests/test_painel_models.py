"""Modelos painel_* e colunas novas de raw_articles, sem banco.

Garante o contrato que o painel le direto no MySQL: nomes, chaves primarias, FK com
cascata e nenhum Float (so Double) nas tabelas da vitrine.
"""

from __future__ import annotations

from sqlalchemy import Double, Float

from tcc_scraper.db import models_painel  # noqa: F401  (registra as tabelas no metadata)
from tcc_scraper.db.models import Article, Base

TABELAS_PAINEL = {
    "v1-painel_comandos",
    "v1-painel_heartbeat",
    "v1-painel_execucoes",
    "v1-painel_etapas",
    "v1-painel_historico",
    "v1-painel_historico_progresso",
    "v1-painel_logs",
    "v1-painel_comandos_aplicados",
}

PKS_ESPERADAS = {
    "v1-painel_comandos": ["id"],
    "v1-painel_heartbeat": ["host"],
    "v1-painel_execucoes": ["id"],
    "v1-painel_etapas": ["execucao_id", "etapa_id"],
    "v1-painel_historico": ["id"],
    "v1-painel_historico_progresso": ["ano", "coletor"],
    "v1-painel_logs": ["id"],
    "v1-painel_comandos_aplicados": ["id"],
}


def test_todas_as_tabelas_painel_estao_no_metadata() -> None:
    encontradas = {nome for nome in Base.metadata.tables if nome.startswith("v1-painel_")}
    assert encontradas == TABELAS_PAINEL


def test_chaves_primarias_conferem() -> None:
    for nome, pk_esperada in PKS_ESPERADAS.items():
        tabela = Base.metadata.tables[nome]
        assert [c.name for c in tabela.primary_key.columns] == pk_esperada, nome


def test_nenhuma_coluna_float_nas_tabelas_painel() -> None:
    # Double herda de Float no SQLAlchemy: compara o tipo exato para nao confundir.
    for nome in TABELAS_PAINEL:
        for coluna in Base.metadata.tables[nome].columns:
            assert not (isinstance(coluna.type, Float) and not isinstance(coluna.type, Double)), (
                f"{nome}.{coluna.name} usa Float; use Double"
            )


def test_itens_por_min_e_double() -> None:
    coluna = Base.metadata.tables["v1-painel_historico"].c.itens_por_min
    assert isinstance(coluna.type, Double)


def test_etapas_tem_fk_com_cascata_para_execucoes() -> None:
    tabela = Base.metadata.tables["v1-painel_etapas"]
    fks = list(tabela.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "v1-painel_execucoes"
    assert fk.column.name == "id"
    assert fk.ondelete == "CASCADE"


def test_logs_nao_tem_fk() -> None:
    assert not list(Base.metadata.tables["v1-painel_logs"].foreign_keys)
    assert not list(Base.metadata.tables["v1-painel_comandos_aplicados"].foreign_keys)


def test_raw_articles_tem_origem_e_precisao_com_default_de_google_news() -> None:
    colunas = Article.__table__.c

    assert colunas.origem.server_default is not None
    assert colunas.origem.server_default.arg == "google_news"
    assert colunas.published_precision.server_default is not None
    assert colunas.published_precision.server_default.arg == "datetime"


def test_raw_articles_tem_indice_em_origem() -> None:
    nomes = {ix.name for ix in Article.__table__.indexes}
    assert "ix_articles_origem" in nomes
