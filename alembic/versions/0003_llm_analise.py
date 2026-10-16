"""analise diaria por LLM: 6 tabelas (raw_llm_* e staging_llm_*)

raw_llm_prompts / raw_llm_runs guardam o que veio da rede: o template versionado e cada
tentativa de chamada ao Gemini (inclusive as que falharam). staging_llm_* guardam o JSON
normalizado em linhas, com FK para o run que as gerou.

Dois tipos escolhidos com o MySQL em mente:
- LONGTEXT em texto_template, prompt_renderizado e resposta_bruta: `Text` no MySQL e
  TEXT de 64 KB, e uma resposta de ~15 mil tokens passa disso e seria truncada.
- JSON nativo nas colunas de lista/objeto (PostgreSQL json, MySQL JSON).

Double em vez de Float (mesmo motivo da 0001). Timestamps via UTCDateTime.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from tcc_scraper.db.models import UTCDateTime

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

LongText = sa.Text().with_variant(mysql.LONGTEXT, "mysql")


def upgrade() -> None:
    # ------------------------------------------------------------------ raw
    op.create_table(
        "raw_llm_prompts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nome", sa.String(length=64), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("texto_template", LongText, nullable=False),
        sa.Column("schema_json", sa.JSON(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("criado_em", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_raw_llm_prompts"),
        sa.UniqueConstraint("nome", "versao", name="uq_llm_prompts_nome_versao"),
    )

    op.create_table(
        "raw_llm_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("prompt_id", sa.BigInteger(), nullable=False),
        sa.Column("data_referencia", sa.Date(), nullable=False),
        sa.Column("pregao_referencia", sa.Date(), nullable=True),
        sa.Column("modelo", sa.String(length=64), nullable=False),
        sa.Column("prompt_renderizado", LongText, nullable=False),
        sa.Column("resposta_bruta", LongText, nullable=True),
        sa.Column("resposta_json", sa.JSON(), nullable=True),
        sa.Column("resumo_geral", sa.Text(), nullable=True),
        sa.Column("buscas_json", sa.JSON(), nullable=True),
        sa.Column("citacoes_json", sa.JSON(), nullable=True),
        sa.Column("tokens_entrada", sa.Integer(), nullable=True),
        sa.Column("tokens_saida", sa.Integer(), nullable=True),
        sa.Column("tokens_pensamento", sa.Integer(), nullable=True),
        sa.Column("latencia_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("iniciado_em", UTCDateTime(), nullable=False),
        sa.Column("finalizado_em", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["prompt_id"], ["raw_llm_prompts.id"],
            name="fk_raw_llm_runs_prompt_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_llm_runs"),
    )
    op.create_index("ix_llm_runs_data_referencia", "raw_llm_runs", ["data_referencia"])

    # -------------------------------------------------------------- staging
    op.create_table(
        "staging_llm_noticias_empresas",
        sa.Column("data_referencia", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("item_hash", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("pregao_referencia", sa.Date(), nullable=True),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("resumo", sa.Text(), nullable=False),
        sa.Column("fonte", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("publicado_em", sa.Date(), nullable=True),
        sa.Column("setor", sa.String(length=32), nullable=False),
        sa.Column("sentimento", sa.String(length=16), nullable=False),
        sa.Column("relevancia", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["raw_llm_runs.id"],
            name="fk_staging_llm_noticias_empresas_run_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "data_referencia", "ticker", "item_hash",
            name="pk_staging_llm_noticias_empresas",
        ),
    )
    op.create_index(
        "ix_llm_noticias_empresas_ticker", "staging_llm_noticias_empresas", ["ticker"]
    )

    op.create_table(
        "staging_llm_noticias_macro",
        sa.Column("data_referencia", sa.Date(), nullable=False),
        sa.Column("item_hash", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("pregao_referencia", sa.Date(), nullable=True),
        sa.Column("escopo", sa.String(length=8), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("resumo", sa.Text(), nullable=False),
        sa.Column("fonte", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("publicado_em", sa.Date(), nullable=True),
        sa.Column("commodities_afetadas", sa.JSON(), nullable=False),
        sa.Column("direcao_commodity", sa.String(length=16), nullable=False),
        sa.Column("setores_afetados", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["raw_llm_runs.id"],
            name="fk_staging_llm_noticias_macro_run_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "data_referencia", "item_hash", name="pk_staging_llm_noticias_macro",
        ),
    )

    op.create_table(
        "staging_llm_acoes_sinais",
        sa.Column("data_referencia", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("pregao_referencia", sa.Date(), nullable=True),
        sa.Column("direcao_esperada", sa.String(length=8), nullable=False),
        sa.Column("confianca", sa.Double(), nullable=False),
        sa.Column("motivo", sa.Text(), nullable=False),
        sa.Column("setor", sa.String(length=32), nullable=False),
        sa.Column("noticias_relacionadas", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["raw_llm_runs.id"],
            name="fk_staging_llm_acoes_sinais_run_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "data_referencia", "ticker", name="pk_staging_llm_acoes_sinais",
        ),
    )
    op.create_index("ix_llm_acoes_sinais_ticker", "staging_llm_acoes_sinais", ["ticker"])

    op.create_table(
        "staging_llm_mercados",
        sa.Column("data_referencia", sa.Date(), nullable=False),
        sa.Column("nome", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("pregao_referencia", sa.Date(), nullable=True),
        sa.Column("tipo", sa.String(length=16), nullable=False),
        sa.Column("variacao_pct", sa.Double(), nullable=True),
        sa.Column("motivo", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["raw_llm_runs.id"],
            name="fk_staging_llm_mercados_run_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "data_referencia", "nome", name="pk_staging_llm_mercados",
        ),
    )


def downgrade() -> None:
    op.drop_table("staging_llm_mercados")
    op.drop_index("ix_llm_acoes_sinais_ticker", "staging_llm_acoes_sinais")
    op.drop_table("staging_llm_acoes_sinais")
    op.drop_table("staging_llm_noticias_macro")
    op.drop_index("ix_llm_noticias_empresas_ticker", "staging_llm_noticias_empresas")
    op.drop_table("staging_llm_noticias_empresas")
    op.drop_index("ix_llm_runs_data_referencia", "raw_llm_runs")
    op.drop_table("raw_llm_runs")
    op.drop_table("raw_llm_prompts")
