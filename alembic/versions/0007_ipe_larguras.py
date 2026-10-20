"""alarga colunas de texto de raw_ipe_docs que estouravam com dados reais da CVM

A fixture do IPE 2026 (1.110 linhas) trouxe valores acima dos tamanhos da 0005:
tipo_apresentacao chega a 33 caracteres (cabia em 32), categoria a 82 (cabia em 64),
tipo a 91 (cabia em 128) e especie a 100 (cabia em 128). Novos tamanhos com folga:
tipo_apresentacao 64, categoria 128, tipo e especie 255. protocolo_entrega (PK) fica em
32: o maior valor medido tem 30 caracteres.

MySQL: o ALTER de coluna regrava a definicao inteira, por isso cada alter_column leva
existing_type e existing_nullable. Nenhuma mudanca de PK.

Downgrade volta aos tamanhos da 0005. Se houver linha com valor maior que o tamanho
antigo, o downgrade falha (truncamento recusado): nao ha reescrita de dados.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "raw_ipe_docs", "categoria",
        existing_type=sa.String(length=64), existing_nullable=False,
        type_=sa.String(length=128),
    )
    op.alter_column(
        "raw_ipe_docs", "tipo",
        existing_type=sa.String(length=128), existing_nullable=True,
        type_=sa.String(length=255),
    )
    op.alter_column(
        "raw_ipe_docs", "especie",
        existing_type=sa.String(length=128), existing_nullable=True,
        type_=sa.String(length=255),
    )
    op.alter_column(
        "raw_ipe_docs", "tipo_apresentacao",
        existing_type=sa.String(length=32), existing_nullable=True,
        type_=sa.String(length=64),
    )


def downgrade() -> None:
    op.alter_column(
        "raw_ipe_docs", "tipo_apresentacao",
        existing_type=sa.String(length=64), existing_nullable=True,
        type_=sa.String(length=32),
    )
    op.alter_column(
        "raw_ipe_docs", "especie",
        existing_type=sa.String(length=255), existing_nullable=True,
        type_=sa.String(length=128),
    )
    op.alter_column(
        "raw_ipe_docs", "tipo",
        existing_type=sa.String(length=255), existing_nullable=True,
        type_=sa.String(length=128),
    )
    op.alter_column(
        "raw_ipe_docs", "categoria",
        existing_type=sa.String(length=128), existing_nullable=False,
        type_=sa.String(length=64),
    )
