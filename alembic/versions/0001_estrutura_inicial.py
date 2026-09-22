"""estrutura inicial: as 8 tabelas com prefixo de camada

As camadas (raw / staging / features) sao PREFIXO no nome da tabela, nao schema
separado. Motivo: o MySQL nao tem schemas como o PostgreSQL -- la "schema" e
"database" sao sinonimos -- e o projeto precisa rodar nos dois. Um unico esquema de
nomes evita codigo condicional por dialeto.

Tipos escolhidos com os dois bancos em mente:
- String com tamanho explicito onde entra em indice (o MySQL nao indexa TEXT sem
  prefixo de tamanho).
- Timestamps via UTCDateTime, que normaliza o fuso nos dois (ver db/models.py).
- Double em vez de Float: `sa.Float()` vira 4 bytes no MySQL e truncaria as
  probabilidades do FinBERT em ~7 digitos, mudando o dataset conforme o banco.

Revision ID: 0001
Create Date: 2026-09-07 (reescrita em 2026-09-21 para portabilidade MySQL)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from tcc_scraper.db.models import UTCDateTime

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- raw
    op.create_table(
        "raw_articles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("google_id", sa.Text(), nullable=True),
        sa.Column("resolved_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.String(length=255), nullable=True),
        sa.Column("published_at", UTCDateTime(), nullable=False),
        sa.Column("lang", sa.String(length=5), nullable=False),
        sa.Column("collected_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_raw_articles"),
        sa.UniqueConstraint("url_hash", name="uq_raw_articles_url_hash"),
    )
    op.create_index("ix_articles_published_at", "raw_articles", ["published_at"])
    op.create_index("ix_articles_source_domain", "raw_articles", ["source_domain"])

    op.create_table(
        "raw_article_bodies",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("extraction_method", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"], ["raw_articles.id"],
            name="fk_raw_article_bodies_article_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("article_id", name="pk_raw_article_bodies"),
    )

    op.create_table(
        "raw_prices",
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Double(), nullable=True),
        sa.Column("high", sa.Double(), nullable=True),
        sa.Column("low", sa.Double(), nullable=True),
        sa.Column("close", sa.Double(), nullable=True),
        sa.Column("adj_close", sa.Double(), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("ticker", "date", name="pk_raw_prices"),
    )

    op.create_table(
        "raw_macro",
        sa.Column("series", sa.String(length=24), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("series", "date", name="pk_raw_macro"),
    )

    op.create_table(
        "raw_collection_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("collector", sa.String(length=32), nullable=False),
        sa.Column("unit_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("items_found", sa.Integer(), nullable=True),
        sa.Column("hit_cap", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_raw_collection_runs"),
        sa.UniqueConstraint(
            "collector", "unit_key", name="uq_collection_runs_collector_unit"
        ),
    )

    # ------------------------------------------------------------ staging
    op.create_table(
        "staging_article_tickers",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"], ["raw_articles.id"],
            name="fk_staging_article_tickers_article_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("article_id", "ticker", name="pk_staging_article_tickers"),
    )
    op.create_index("ix_article_tickers_ticker", "staging_article_tickers", ["ticker"])

    op.create_table(
        "staging_article_sentiment",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prob_pos", sa.Double(), nullable=False),
        sa.Column("prob_neu", sa.Double(), nullable=False),
        sa.Column("prob_neg", sa.Double(), nullable=False),
        sa.Column("text_source", sa.String(length=8), nullable=False),
        sa.Column("scored_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "prob_pos >= 0 AND prob_neu >= 0 AND prob_neg >= 0",
            name="ck_article_sentiment_probs_nonneg",
        ),
        sa.ForeignKeyConstraint(
            ["article_id"], ["raw_articles.id"],
            name="fk_staging_article_sentiment_article_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("article_id", "model", name="pk_staging_article_sentiment"),
    )

    # ----------------------------------------------------------- features
    op.create_table(
        "features_dataset_v1",
        sa.Column("data", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("quant_noticias", sa.Integer(), nullable=False),
        sa.Column("positivo", sa.Double(), nullable=True),
        sa.Column("neutro", sa.Double(), nullable=True),
        sa.Column("negativo", sa.Double(), nullable=True),
        sa.Column("score_finbert", sa.Double(), nullable=True),
        sa.Column("retorno_1d", sa.Double(), nullable=True),
        sa.Column("retorno_5d", sa.Double(), nullable=True),
        sa.Column("retorno_20d", sa.Double(), nullable=True),
        sa.Column("volatilidade_20d", sa.Double(), nullable=True),
        sa.Column("volume_relativo", sa.Double(), nullable=True),
        sa.Column("retorno_ibovespa", sa.Double(), nullable=True),
        sa.Column("target", sa.SmallInteger(), nullable=True),
        sa.PrimaryKeyConstraint("data", "ticker", name="pk_features_dataset_v1"),
    )


def downgrade() -> None:
    op.drop_table("features_dataset_v1")
    op.drop_table("staging_article_sentiment")
    op.drop_index("ix_article_tickers_ticker", "staging_article_tickers")
    op.drop_table("staging_article_tickers")
    op.drop_table("raw_collection_runs")
    op.drop_table("raw_macro")
    op.drop_table("raw_prices")
    op.drop_table("raw_article_bodies")
    op.drop_index("ix_articles_source_domain", "raw_articles")
    op.drop_index("ix_articles_published_at", "raw_articles")
    op.drop_table("raw_articles")
