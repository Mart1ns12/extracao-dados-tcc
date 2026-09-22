"""alinha raw_collection_runs ao modelo: items_found e hit_cap NOT NULL

A migration 0001 criou as duas colunas como nullable, mas o modelo as declara como
`Mapped[int]` / `Mapped[bool]` (sem `| None`), o que o SQLAlchemy interpreta como
NOT NULL. A divergencia fazia `alembic check` falhar -- ruido que mascararia uma
mudanca de schema de verdade no futuro.

Semanticamente o NOT NULL esta certo: `Collector._record` sempre preenche os dois
campos (0 e False quando a unidade falha). Confirmado antes de aplicar: zero linhas
nulas nos tres bancos.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Defensivo: o ALTER falharia se houvesse NULL remanescente de uma execucao
    # anterior ao NOT NULL.
    op.execute("UPDATE raw_collection_runs SET items_found = 0 WHERE items_found IS NULL")
    op.execute("UPDATE raw_collection_runs SET hit_cap = FALSE WHERE hit_cap IS NULL")

    op.alter_column(
        "raw_collection_runs", "items_found",
        existing_type=sa.Integer(), nullable=False,
    )
    op.alter_column(
        "raw_collection_runs", "hit_cap",
        existing_type=sa.Boolean(), nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "raw_collection_runs", "hit_cap",
        existing_type=sa.Boolean(), nullable=True,
    )
    op.alter_column(
        "raw_collection_runs", "items_found",
        existing_type=sa.Integer(), nullable=True,
    )
