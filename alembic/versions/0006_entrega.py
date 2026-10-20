"""entrega para a etapa seguinte: 8 tabelas sem prefixo de camada

noticias, precos, cadastro, mudancas_ticker, balancos, acoes_emitidas, proventos e indices
tem o nome exato da spec (sem prefixo raw_/staging_, por decisao do dono). Colunas e chaves
primarias seguem a parte 2 da spec; a noticias leva ainda as 7 colunas extras do desenho
(secao 5.1). data_hora_* sao texto ISO com offset, porque o MySQL nao guarda fuso.

Double em vez de Float (mesmo motivo da 0001). Datas em Date.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "noticias",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("data_hora_publicacao", sa.String(length=32), nullable=True),
        sa.Column("data_hora_coleta", sa.String(length=32), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("fonte", sa.String(length=255), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("prob_positivo", sa.Double(), nullable=True),
        sa.Column("prob_neutro", sa.Double(), nullable=True),
        sa.Column("prob_negativo", sa.Double(), nullable=True),
        sa.Column("data_publicacao", sa.Date(), nullable=False),
        sa.Column("momento_pregao", sa.String(length=16), nullable=False),
        sa.Column("origem", sa.String(length=16), nullable=False),
        sa.Column("papel", sa.String(length=24), nullable=True),
        sa.Column("tipo_conteudo", sa.String(length=24), nullable=True),
        sa.Column("evento", sa.String(length=24), nullable=True),
        sa.Column("qualidade", sa.Double(), nullable=True),
        sa.PrimaryKeyConstraint("id", "ticker", name="pk_noticias"),
    )
    op.create_index(
        "ix_noticias_ticker_data_publicacao", "noticias", ["ticker", "data_publicacao"],
    )

    op.create_table(
        "precos",
        sa.Column("data", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("abertura", sa.Double(), nullable=True),
        sa.Column("maxima", sa.Double(), nullable=True),
        sa.Column("minima", sa.Double(), nullable=True),
        sa.Column("fechamento", sa.Double(), nullable=True),
        sa.Column("fechamento_ajustado", sa.Double(), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("data", "ticker", name="pk_precos"),
    )
    op.create_index("ix_precos_ticker", "precos", ["ticker"])

    op.create_table(
        "cadastro",
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("cnpj", sa.String(length=14), nullable=True),
        sa.Column("empresa", sa.String(length=128), nullable=False),
        sa.Column("setor", sa.String(length=128), nullable=True),
        sa.Column("subsetor", sa.String(length=128), nullable=True),
        sa.Column("tipo_acao", sa.String(length=8), nullable=False),
        sa.Column("unit_qtd_on", sa.SmallInteger(), nullable=True),
        sa.Column("unit_qtd_pn", sa.SmallInteger(), nullable=True),
        sa.PrimaryKeyConstraint("ticker", name="pk_cadastro"),
    )

    op.create_table(
        "mudancas_ticker",
        sa.Column("ticker_antigo", sa.String(length=12), nullable=False),
        sa.Column("ticker_novo", sa.String(length=12), nullable=False),
        sa.Column("data_mudanca", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("ticker_antigo", name="pk_mudancas_ticker"),
    )

    op.create_table(
        "balancos",
        sa.Column("cnpj", sa.String(length=14), nullable=False),
        sa.Column("tipo_documento", sa.String(length=3), nullable=False),
        sa.Column("data_inicio_periodo", sa.Date(), nullable=False),
        sa.Column("data_fim_periodo", sa.Date(), nullable=False),
        sa.Column("data_entrega", sa.Date(), nullable=True),
        sa.Column("receita_liquida", sa.Double(), nullable=True),
        sa.Column("lucro_bruto", sa.Double(), nullable=True),
        sa.Column("lucro_liquido", sa.Double(), nullable=True),
        sa.Column("patrimonio_liquido", sa.Double(), nullable=True),
        sa.PrimaryKeyConstraint(
            "cnpj", "tipo_documento", "data_inicio_periodo", "data_fim_periodo",
            name="pk_balancos",
        ),
    )

    op.create_table(
        "acoes_emitidas",
        sa.Column("cnpj", sa.String(length=14), nullable=False),
        sa.Column("data_referencia", sa.Date(), nullable=False),
        sa.Column("qtd_on", sa.BigInteger(), nullable=True),
        sa.Column("qtd_pn", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("cnpj", "data_referencia", name="pk_acoes_emitidas"),
    )

    op.create_table(
        "proventos",
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("tipo", sa.String(length=10), nullable=False),
        sa.Column("data_com", sa.Date(), nullable=False),
        sa.Column("valor_por_acao", sa.Double(), nullable=False),
        sa.Column("data_pagamento", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("ticker", "tipo", "data_com", name="pk_proventos"),
    )
    op.create_index("ix_proventos_ticker", "proventos", ["ticker"])

    op.create_table(
        "indices",
        sa.Column("data", sa.Date(), nullable=False),
        sa.Column("indice", sa.String(length=8), nullable=False),
        sa.Column("valor", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("data", "indice", name="pk_indices"),
    )
    op.create_index("ix_indices_indice", "indices", ["indice"])


def downgrade() -> None:
    op.drop_index("ix_indices_indice", "indices")
    op.drop_table("indices")

    op.drop_index("ix_proventos_ticker", "proventos")
    op.drop_table("proventos")

    op.drop_table("acoes_emitidas")
    op.drop_table("balancos")
    op.drop_table("mudancas_ticker")
    op.drop_table("cadastro")

    op.drop_index("ix_precos_ticker", "precos")
    op.drop_table("precos")

    op.drop_index("ix_noticias_ticker_data_publicacao", "noticias")
    op.drop_table("noticias")
