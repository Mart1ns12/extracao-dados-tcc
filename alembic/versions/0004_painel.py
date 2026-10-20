"""painel de acompanhamento: 8 tabelas painel_* e origem em raw_articles

As tabelas painel_* sao a vitrine que o painel (outro repositorio) le direto no MySQL.
painel_comandos e a unica escrita da API do painel; o executor escreve nas demais.
painel_logs e painel_comandos_aplicados nao tem FK: o log sobrevive a limpeza.

raw_articles ganha origem (google_news | IPE | RSS nativo) e published_precision.
Os defaults cobrem os artigos ja existentes, todos vindos do Google News.

Double em vez de Float (mesmo motivo da 0001). Timestamps via UTCDateTime.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from tcc_scraper.db.models import UTCDateTime

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------- raw_articles
    op.add_column(
        "raw_articles",
        sa.Column(
            "origem", sa.String(length=16), nullable=False, server_default="google_news"
        ),
    )
    op.add_column(
        "raw_articles",
        sa.Column(
            "published_precision", sa.String(length=8), nullable=False,
            server_default="datetime",
        ),
    )
    op.create_index("ix_articles_origem", "raw_articles", ["origem"])

    # ------------------------------------------------------------- painel
    op.create_table(
        "painel_comandos",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tipo", sa.String(length=16), nullable=False),
        sa.Column("parametros", sa.JSON(), nullable=False),
        sa.Column("criado_em", UTCDateTime(), nullable=False),
        sa.Column("ack_em", UTCDateTime(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_painel_comandos"),
    )
    op.create_index("ix_painel_comandos_ack_em", "painel_comandos", ["ack_em"])

    op.create_table(
        "painel_heartbeat",
        sa.Column("host", sa.String(length=64), nullable=False),
        sa.Column("versao", sa.String(length=32), nullable=False),
        sa.Column("ocupado_com", sa.String(length=16), nullable=True),
        sa.Column("visto_em", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("host", name="pk_painel_heartbeat"),
    )

    op.create_table(
        "painel_execucoes",
        sa.Column("id", sa.String(length=48), nullable=False),
        sa.Column("tipo", sa.String(length=12), nullable=False),
        sa.Column("etapa_alvo", sa.String(length=24), nullable=True),
        sa.Column("origem", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("aguardando", sa.String(length=16), nullable=True),
        sa.Column("iniciada_em", UTCDateTime(), nullable=False),
        sa.Column("finalizada_em", UTCDateTime(), nullable=True),
        sa.Column("prazo_em", UTCDateTime(), nullable=False),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("atualizada_em", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_painel_execucoes"),
    )
    op.create_index("ix_painel_execucoes_iniciada_em", "painel_execucoes", ["iniciada_em"])

    op.create_table(
        "painel_etapas",
        sa.Column("execucao_id", sa.String(length=48), nullable=False),
        sa.Column("etapa_id", sa.String(length=24), nullable=False),
        sa.Column("ordem", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("unidades_total", sa.Integer(), nullable=True),
        sa.Column("unidades_feitas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unidades_erro", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unidades_saturadas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("itens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("iniciada_em", UTCDateTime(), nullable=True),
        sa.Column("finalizada_em", UTCDateTime(), nullable=True),
        sa.Column("ultimo_erro_unidade", sa.String(length=128), nullable=True),
        sa.Column("ultimo_erro_mensagem", sa.Text(), nullable=True),
        sa.Column("atualizada_em", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["execucao_id"], ["painel_execucoes.id"],
            name="fk_painel_etapas_execucao_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "execucao_id", "etapa_id", name="pk_painel_etapas",
        ),
    )

    op.create_table(
        "painel_historico",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("modo", sa.String(length=24), nullable=False),
        sa.Column("anos", sa.JSON(), nullable=False),
        sa.Column("ano_atual", sa.Integer(), nullable=True),
        sa.Column("coletor_atual", sa.String(length=32), nullable=True),
        sa.Column("unidade_atual", sa.String(length=128), nullable=True),
        sa.Column("iniciada_em", UTCDateTime(), nullable=False),
        sa.Column("finalizada_em", UTCDateTime(), nullable=True),
        sa.Column("itens_por_min", sa.Double(), nullable=False, server_default="0"),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("atualizada_em", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_painel_historico"),
    )

    op.create_table(
        "painel_historico_progresso",
        sa.Column("ano", sa.Integer(), nullable=False),
        sa.Column("coletor", sa.String(length=32), nullable=False),
        sa.Column("unidades_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unidades_feitas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unidades_erro", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("itens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_segmento", sa.String(length=16), nullable=False),
        sa.Column("atualizado_em", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "ano", "coletor", name="pk_painel_historico_progresso",
        ),
    )

    op.create_table(
        "painel_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", UTCDateTime(), nullable=False),
        sa.Column("nivel", sa.String(length=8), nullable=False),
        sa.Column("origem", sa.String(length=16), nullable=False),
        sa.Column("etapa", sa.String(length=24), nullable=True),
        sa.Column("mensagem", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_painel_logs"),
    )
    op.create_index("ix_painel_logs_ts", "painel_logs", ["ts"])

    op.create_table(
        "painel_comandos_aplicados",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("aplicado_em", UTCDateTime(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_painel_comandos_aplicados"),
    )


def downgrade() -> None:
    op.drop_table("painel_comandos_aplicados")
    op.drop_index("ix_painel_logs_ts", "painel_logs")
    op.drop_table("painel_logs")
    op.drop_table("painel_historico_progresso")
    op.drop_table("painel_historico")
    # painel_etapas antes de painel_execucoes: a FK aponta para ela.
    op.drop_table("painel_etapas")
    op.drop_index("ix_painel_execucoes_iniciada_em", "painel_execucoes")
    op.drop_table("painel_execucoes")
    op.drop_table("painel_heartbeat")
    op.drop_index("ix_painel_comandos_ack_em", "painel_comandos")
    op.drop_table("painel_comandos")

    op.drop_index("ix_articles_origem", "raw_articles")
    op.drop_column("raw_articles", "published_precision")
    op.drop_column("raw_articles", "origem")
