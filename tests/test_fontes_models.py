"""Modelos das fontes novas (CVM, B3) e da juíza, sem banco.

Garante o contrato do schema em sql/schema.mysql.sql: nomes, chaves primárias, FKs com cascata e
nenhum Float (só Double) nas tabelas.
"""

from __future__ import annotations

from sqlalchemy import Double, Float, ForeignKeyConstraint
from sqlalchemy.dialects import mysql

from tcc_scraper.db import models_fontes  # noqa: F401  (registra as tabelas no metadata)
from tcc_scraper.db.models import Base, tabela_do_projeto

TABELAS_FONTES = {
    "v1-raw_ipe_docs",
    "v1-raw_cvm_balancos",
    "v1-raw_cvm_acoes",
    "v1-raw_cvm_cadastro",
    "v1-raw_b3_cadastro",
    "v1-raw_b3_proventos",
    "v1-raw_b3_suplemento",
    "v1-raw_llm_juiza_runs",
    "v1-staging_noticias_julgamento",
    "v1-staging_juiza_tentativas",
    "v1-raw_wp_posts",
    "v1-staging_historias",
    "v1-staging_wp_processamento",
}

PKS_ESPERADAS = {
    "v1-raw_ipe_docs": ["protocolo_entrega"],
    "v1-raw_cvm_balancos": [
        "cnpj", "tipo_documento", "dt_refer", "versao", "demonstracao",
        "cd_conta", "dt_ini_exerc", "dt_fim_exerc",
    ],
    "v1-raw_cvm_acoes": ["cnpj", "tipo_documento", "dt_refer", "versao"],
    "v1-raw_cvm_cadastro": ["cnpj"],
    "v1-raw_b3_cadastro": ["cd_cvm"],
    "v1-raw_b3_proventos": ["registro_hash"],
    "v1-raw_b3_suplemento": ["registro_hash"],
    "v1-raw_llm_juiza_runs": ["id"],
    "v1-staging_noticias_julgamento": ["article_id", "ticker", "prompt_versao"],
    "v1-staging_juiza_tentativas": ["article_id", "ticker", "prompt_versao"],
    "v1-raw_wp_posts": ["site", "wp_id"],
    "v1-staging_historias": ["article_id"],
    "v1-staging_wp_processamento": ["site", "wp_id"],
}


def test_todas_as_tabelas_de_fontes_estao_no_metadata() -> None:
    assert set(Base.metadata.tables) >= TABELAS_FONTES


def test_chaves_primarias_conferem() -> None:
    for nome, pk_esperada in PKS_ESPERADAS.items():
        tabela = Base.metadata.tables[nome]
        assert [c.name for c in tabela.primary_key.columns] == pk_esperada, nome


def test_nomes_de_tabela_passam_no_filtro_de_camada() -> None:
    # Tabelas fora do filtro tabela_do_projeto ficariam de fora das checagens de schema.
    for nome in TABELAS_FONTES:
        assert tabela_do_projeto(nome), nome


def test_nenhuma_coluna_float_nas_tabelas_de_fontes() -> None:
    # Double herda de Float no SQLAlchemy: compara para nao confundir os dois.
    for nome in TABELAS_FONTES:
        for coluna in Base.metadata.tables[nome].columns:
            assert not (isinstance(coluna.type, Float) and not isinstance(coluna.type, Double)), (
                f"{nome}.{coluna.name} usa Float; use Double"
            )


def test_valores_de_balanco_e_probabilidade_sao_double() -> None:
    assert isinstance(Base.metadata.tables["v1-raw_cvm_balancos"].c.vl_conta.type, Double)
    julgamento = Base.metadata.tables["v1-staging_noticias_julgamento"]
    assert isinstance(julgamento.c.qualidade.type, Double)
    assert isinstance(julgamento.c.confianca.type, Double)


def test_custo_da_juiza_e_double() -> None:
    tabela = Base.metadata.tables["v1-raw_llm_juiza_runs"]
    assert isinstance(tabela.c.custo_usd.type, Double)
    assert tabela.c.custo_usd.nullable


def test_finalidade_da_juiza_tem_default_producao_no_banco() -> None:
    tabela = Base.metadata.tables["v1-raw_llm_juiza_runs"]
    assert tabela.c.finalidade.server_default.arg == "producao"
    assert not tabela.c.finalidade.nullable


def test_staging_julgamento_cascateia_do_artigo() -> None:
    tabela = Base.metadata.tables["v1-staging_noticias_julgamento"]
    fks = {fk.parent.name: fk for fk in tabela.foreign_keys}

    assert fks["article_id"].column.table.name == "raw_articles"
    assert fks["article_id"].ondelete == "CASCADE"
    assert fks["run_id"].column.table.name == "v1-raw_llm_juiza_runs"
    assert fks["run_id"].ondelete is None


def test_juiza_runs_aponta_para_o_prompt() -> None:
    tabela = Base.metadata.tables["v1-raw_llm_juiza_runs"]
    fks = list(tabela.foreign_keys)

    assert len(fks) == 1
    assert fks[0].column.table.name == "raw_llm_prompts"
    assert fks[0].column.name == "id"


def test_raw_wp_posts_tem_indice_de_data_e_html_em_longtext() -> None:
    tabela = Base.metadata.tables["v1-raw_wp_posts"]

    assert "ix_raw_wp_posts_date_gmt" in {idx.name for idx in tabela.indexes}
    assert isinstance(tabela.c.content_html.type, mysql.LONGTEXT)


def test_staging_historias_cascateia_do_artigo_e_indexa_a_historia() -> None:
    tabela = Base.metadata.tables["v1-staging_historias"]
    fks = {fk.parent.name: fk for fk in tabela.foreign_keys}

    assert fks["article_id"].column.table.name == "raw_articles"
    assert fks["article_id"].ondelete == "CASCADE"
    assert fks["historia_article_id"].column.table.name == "raw_articles"
    assert fks["historia_article_id"].ondelete is None
    assert "ix_staging_historias_historia_article_id" in {idx.name for idx in tabela.indexes}


def test_processamento_aponta_para_o_post_com_fk_composta() -> None:
    tabela = Base.metadata.tables["v1-staging_wp_processamento"]
    fks_do_post = {
        fk.parent.name: fk.column.name
        for fk in tabela.foreign_keys
        if fk.column.table.name == "v1-raw_wp_posts"
    }
    composta = [
        c for c in tabela.constraints
        if isinstance(c, ForeignKeyConstraint) and len(c.elements) == 2
    ]

    # Uma constraint so para (site, wp_id), nao uma FK por coluna.
    assert fks_do_post == {"site": "site", "wp_id": "wp_id"}
    assert len(composta) == 1
    assert tabela.c.article_id.nullable
    assert {fk.column.table.name for fk in tabela.c.article_id.foreign_keys} == {"raw_articles"}
