"""prefixo `v1-` em todas as tabelas da pipeline nova

Pedido do dono: toda tabela usada pela pipeline nova leva `v1-` na frente do nome
(`raw_articles` vira `v1-raw_articles`). Sao 38 tabelas: as de camada (raw_, staging_,
painel_) e as 8 de entrega (noticias ... indices). Fica de fora `features_dataset_v1`,
o dataset v1 congelado, e `alembic_version`.

So o NOME DA TABELA muda: `op.rename_table` no PostgreSQL e no MySQL. Chaves
primarias, unique, foreign keys, indices e sequencias mantem o nome antigo (ex.:
`ix_articles_origem`, `uq_..._url_hash`, `raw_articles_id_seq`). Isso e aceitavel:
o SQLAlchemy nao depende do nome da constraint para gravar nem para ler, e as FKs
seguem a tabela renomeada no PostgreSQL e no MySQL. O custo e que o nome de um indice
ou constraint criado a partir de agora usa o `v1-` (naming_convention), enquanto os
antigos nao; o alembic check so aponta divergencia se o nome for comparado.

Ordem de renomeacao: indiferente (as FKs apontam para o objeto, nao para o nome).

Downgrade desfaz os 38 renames.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

PREFIXO = "v1-"

# Os 38 nomes antigos (sem prefixo). Conferir contra Base.metadata: so
# `features_dataset_v1` fica de fora.
TABELAS = (
    "raw_articles", "raw_article_bodies", "raw_prices", "raw_macro", "raw_collection_runs",
    "staging_article_tickers", "staging_article_sentiment", "raw_llm_prompts", "raw_llm_runs",
    "staging_llm_noticias_empresas", "staging_llm_noticias_macro", "staging_llm_acoes_sinais",
    "staging_llm_mercados", "painel_comandos", "painel_heartbeat", "painel_execucoes",
    "painel_etapas", "painel_historico", "painel_historico_progresso", "painel_logs",
    "painel_comandos_aplicados", "raw_ipe_docs", "raw_cvm_balancos", "raw_cvm_acoes",
    "raw_cvm_cadastro", "raw_b3_cadastro", "raw_b3_proventos", "raw_b3_suplemento",
    "raw_llm_juiza_runs", "staging_noticias_julgamento", "noticias", "precos", "cadastro",
    "mudancas_ticker", "balancos", "acoes_emitidas", "proventos", "indices",
)


def upgrade() -> None:
    for nome in TABELAS:
        op.rename_table(nome, f"{PREFIXO}{nome}")


def downgrade() -> None:
    for nome in reversed(TABELAS):
        op.rename_table(f"{PREFIXO}{nome}", nome)
