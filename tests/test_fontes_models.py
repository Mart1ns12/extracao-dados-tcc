"""Modelos das fontes novas (CVM, B3) e da juíza, sem banco.

Garante o contrato da migration 0005: nomes, chaves primárias, FKs com cascata e
nenhum Float (só Double) nas tabelas.
"""

from __future__ import annotations

from sqlalchemy import Double, Float

from tcc_scraper.db import models_fontes  # noqa: F401  (registra as tabelas no metadata)
from tcc_scraper.db.models import Base, tabela_do_projeto

TABELAS_FONTES = {
    "raw_ipe_docs",
    "raw_cvm_balancos",
    "raw_cvm_acoes",
    "raw_cvm_cadastro",
    "raw_b3_cadastro",
    "raw_b3_proventos",
    "raw_b3_suplemento",
    "raw_llm_juiza_runs",
    "staging_noticias_julgamento",
}

PKS_ESPERADAS = {
    "raw_ipe_docs": ["protocolo_entrega"],
    "raw_cvm_balancos": [
        "cnpj", "tipo_documento", "dt_refer", "versao", "demonstracao",
        "cd_conta", "dt_ini_exerc", "dt_fim_exerc",
    ],
    "raw_cvm_acoes": ["cnpj", "tipo_documento", "dt_refer", "versao"],
    "raw_cvm_cadastro": ["cnpj"],
    "raw_b3_cadastro": ["cd_cvm"],
    "raw_b3_proventos": ["registro_hash"],
    "raw_b3_suplemento": ["registro_hash"],
    "raw_llm_juiza_runs": ["id"],
    "staging_noticias_julgamento": ["article_id", "ticker", "prompt_versao"],
}


def test_todas_as_tabelas_de_fontes_estao_no_metadata() -> None:
    assert set(Base.metadata.tables) >= TABELAS_FONTES


def test_chaves_primarias_conferem() -> None:
    for nome, pk_esperada in PKS_ESPERADAS.items():
        tabela = Base.metadata.tables[nome]
        assert [c.name for c in tabela.primary_key.columns] == pk_esperada, nome


def test_nomes_de_tabela_passam_no_filtro_de_camada() -> None:
    # O autogenerate do Alembic so enxerga tabelas que passam por tabela_do_projeto.
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
    assert isinstance(Base.metadata.tables["raw_cvm_balancos"].c.vl_conta.type, Double)
    julgamento = Base.metadata.tables["staging_noticias_julgamento"]
    assert isinstance(julgamento.c.qualidade.type, Double)
    assert isinstance(julgamento.c.confianca.type, Double)


def test_staging_julgamento_cascateia_do_artigo() -> None:
    tabela = Base.metadata.tables["staging_noticias_julgamento"]
    fks = {fk.parent.name: fk for fk in tabela.foreign_keys}

    assert fks["article_id"].column.table.name == "raw_articles"
    assert fks["article_id"].ondelete == "CASCADE"
    assert fks["run_id"].column.table.name == "raw_llm_juiza_runs"
    assert fks["run_id"].ondelete is None


def test_juiza_runs_aponta_para_o_prompt() -> None:
    tabela = Base.metadata.tables["raw_llm_juiza_runs"]
    fks = list(tabela.foreign_keys)

    assert len(fks) == 1
    assert fks[0].column.table.name == "raw_llm_prompts"
    assert fks[0].column.name == "id"
