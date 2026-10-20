"""fontes novas (CVM, B3) e juiza de noticias: 9 tabelas raw_* e staging_*

raw_ipe_docs, raw_cvm_balancos, raw_cvm_acoes e raw_cvm_cadastro vem da CVM (dados
abertos). raw_b3_cadastro, raw_b3_proventos e raw_b3_suplemento vem do
listedCompaniesProxy da B3. raw_llm_juiza_runs guarda cada chamada da juiza ao Gemini
(inclusive as que falharam) e staging_noticias_julgamento guarda o julgamento por
(artigo, ticker), substituido ao reprocessar uma versao do prompt.

Double em vez de Float (mesmo motivo da 0001). Timestamps via UTCDateTime.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from tcc_scraper.db.models import UTCDateTime

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

LongText = sa.Text().with_variant(mysql.LONGTEXT, "mysql")


def upgrade() -> None:
    # ------------------------------------------------------------ CVM
    op.create_table(
        "raw_ipe_docs",
        sa.Column("protocolo_entrega", sa.String(length=32), nullable=False),
        sa.Column("cnpj", sa.String(length=14), nullable=False),
        sa.Column("nome_companhia", sa.Text(), nullable=True),
        sa.Column("cd_cvm", sa.Integer(), nullable=False),
        sa.Column("data_referencia", sa.Date(), nullable=True),
        sa.Column("categoria", sa.String(length=64), nullable=False),
        sa.Column("tipo", sa.String(length=128), nullable=True),
        sa.Column("especie", sa.String(length=128), nullable=True),
        sa.Column("assunto", sa.Text(), nullable=True),
        sa.Column("data_entrega", sa.Date(), nullable=False),
        sa.Column("tipo_apresentacao", sa.String(length=32), nullable=True),
        sa.Column("versao", sa.Integer(), nullable=True),
        sa.Column("link_download", sa.Text(), nullable=False),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("protocolo_entrega", name="pk_raw_ipe_docs"),
    )
    op.create_index("ix_raw_ipe_docs_cd_cvm", "raw_ipe_docs", ["cd_cvm"])

    op.create_table(
        "raw_cvm_balancos",
        sa.Column("cnpj", sa.String(length=14), nullable=False),
        sa.Column("tipo_documento", sa.String(length=3), nullable=False),
        sa.Column("dt_refer", sa.Date(), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("demonstracao", sa.String(length=3), nullable=False),
        sa.Column("cd_conta", sa.String(length=16), nullable=False),
        sa.Column("dt_ini_exerc", sa.Date(), nullable=False),
        sa.Column("dt_fim_exerc", sa.Date(), nullable=False),
        sa.Column("cd_cvm", sa.Integer(), nullable=False),
        sa.Column("ds_conta", sa.String(length=255), nullable=True),
        sa.Column("ordem_exerc", sa.String(length=16), nullable=False),
        sa.Column("escala_moeda", sa.String(length=8), nullable=False),
        sa.Column("vl_conta", sa.Double(), nullable=False),
        sa.Column("dt_receb", sa.Date(), nullable=True),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "cnpj", "tipo_documento", "dt_refer", "versao", "demonstracao",
            "cd_conta", "dt_ini_exerc", "dt_fim_exerc",
            name="pk_raw_cvm_balancos",
        ),
    )
    op.create_index("ix_raw_cvm_balancos_cd_cvm", "raw_cvm_balancos", ["cd_cvm"])

    op.create_table(
        "raw_cvm_acoes",
        sa.Column("cnpj", sa.String(length=14), nullable=False),
        sa.Column("tipo_documento", sa.String(length=3), nullable=False),
        sa.Column("dt_refer", sa.Date(), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("cd_cvm", sa.Integer(), nullable=False),
        sa.Column("qt_on", sa.BigInteger(), nullable=True),
        sa.Column("qt_pn", sa.BigInteger(), nullable=True),
        sa.Column("qt_total", sa.BigInteger(), nullable=True),
        sa.Column("dt_receb", sa.Date(), nullable=True),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "cnpj", "tipo_documento", "dt_refer", "versao", name="pk_raw_cvm_acoes",
        ),
    )

    op.create_table(
        "raw_cvm_cadastro",
        sa.Column("cnpj", sa.String(length=14), nullable=False),
        sa.Column("cd_cvm", sa.Integer(), nullable=False),
        sa.Column("denom_social", sa.Text(), nullable=False),
        sa.Column("sit", sa.String(length=32), nullable=True),
        sa.Column("setor_ativ", sa.Text(), nullable=True),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("cnpj", name="pk_raw_cvm_cadastro"),
    )
    op.create_index("ix_raw_cvm_cadastro_cd_cvm", "raw_cvm_cadastro", ["cd_cvm"])

    # ------------------------------------------------------------ B3
    op.create_table(
        "raw_b3_cadastro",
        sa.Column("cd_cvm", sa.Integer(), nullable=False),
        sa.Column("cnpj", sa.String(length=14), nullable=True),
        sa.Column("trading_name", sa.String(length=128), nullable=True),
        sa.Column("issuing_company", sa.String(length=8), nullable=True),
        sa.Column("industry_classification", sa.Text(), nullable=True),
        sa.Column("other_codes", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("cd_cvm", name="pk_raw_b3_cadastro"),
    )

    op.create_table(
        "raw_b3_proventos",
        sa.Column("registro_hash", sa.String(length=64), nullable=False),
        sa.Column("issuing_company", sa.String(length=8), nullable=False),
        sa.Column("trading_name", sa.String(length=128), nullable=True),
        sa.Column("type_stock", sa.String(length=16), nullable=True),
        sa.Column("corporate_action", sa.String(length=64), nullable=True),
        sa.Column("value_cash", sa.Double(), nullable=True),
        sa.Column("last_date_prior_ex", sa.Date(), nullable=True),
        sa.Column("date_approval", sa.Date(), nullable=True),
        sa.Column("ratio", sa.String(length=32), nullable=True),
        sa.Column("quoted_per_shares", sa.String(length=32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("registro_hash", name="pk_raw_b3_proventos"),
    )
    op.create_index(
        "ix_raw_b3_proventos_issuing_company", "raw_b3_proventos", ["issuing_company"],
    )

    op.create_table(
        "raw_b3_suplemento",
        sa.Column("registro_hash", sa.String(length=64), nullable=False),
        sa.Column("issuing_company", sa.String(length=8), nullable=False),
        sa.Column("tipo", sa.String(length=8), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("registro_hash", name="pk_raw_b3_suplemento"),
    )
    op.create_index(
        "ix_raw_b3_suplemento_issuing_company", "raw_b3_suplemento", ["issuing_company"],
    )

    # ------------------------------------------------------------ juiza
    op.create_table(
        "raw_llm_juiza_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("prompt_id", sa.BigInteger(), nullable=False),
        sa.Column("modelo", sa.String(length=64), nullable=False),
        sa.Column("n_itens", sa.Integer(), nullable=False),
        sa.Column("tokens_entrada", sa.Integer(), nullable=True),
        sa.Column("tokens_saida", sa.Integer(), nullable=True),
        sa.Column("latencia_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("resposta_bruta", LongText, nullable=True),
        sa.Column("iniciado_em", UTCDateTime(), nullable=False),
        sa.Column("finalizado_em", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["prompt_id"], ["raw_llm_prompts.id"],
            name="fk_raw_llm_juiza_runs_prompt_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_llm_juiza_runs"),
    )

    op.create_table(
        "staging_noticias_julgamento",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("prompt_versao", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("modelo", sa.String(length=64), nullable=False),
        sa.Column("papel", sa.String(length=24), nullable=False),
        sa.Column("tipo_conteudo", sa.String(length=24), nullable=False),
        sa.Column("evento", sa.String(length=24), nullable=False),
        sa.Column("tickers_adicionais", sa.JSON(), nullable=False),
        sa.Column("qualidade", sa.Double(), nullable=False),
        sa.Column("confianca", sa.Double(), nullable=False),
        sa.Column("julgado_em", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"], ["raw_articles.id"],
            name="fk_staging_noticias_julgamento_article_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["raw_llm_juiza_runs.id"],
            name="fk_staging_noticias_julgamento_run_id",
        ),
        sa.PrimaryKeyConstraint(
            "article_id", "ticker", "prompt_versao",
            name="pk_staging_noticias_julgamento",
        ),
    )
    op.create_index(
        "ix_staging_noticias_julgamento_ticker", "staging_noticias_julgamento", ["ticker"],
    )


def downgrade() -> None:
    # Ordem inversa das FKs: staging antes de juiza_runs, juiza_runs antes de raw_llm_prompts
    # (que e da 0003 e nao e tocada aqui).
    op.drop_index(
        "ix_staging_noticias_julgamento_ticker", "staging_noticias_julgamento",
    )
    op.drop_table("staging_noticias_julgamento")
    op.drop_table("raw_llm_juiza_runs")

    op.drop_index("ix_raw_b3_suplemento_issuing_company", "raw_b3_suplemento")
    op.drop_table("raw_b3_suplemento")
    op.drop_index("ix_raw_b3_proventos_issuing_company", "raw_b3_proventos")
    op.drop_table("raw_b3_proventos")
    op.drop_table("raw_b3_cadastro")

    op.drop_index("ix_raw_cvm_cadastro_cd_cvm", "raw_cvm_cadastro")
    op.drop_table("raw_cvm_cadastro")
    op.drop_table("raw_cvm_acoes")
    op.drop_index("ix_raw_cvm_balancos_cd_cvm", "raw_cvm_balancos")
    op.drop_table("raw_cvm_balancos")
    op.drop_index("ix_raw_ipe_docs_cd_cvm", "raw_ipe_docs")
    op.drop_table("raw_ipe_docs")
