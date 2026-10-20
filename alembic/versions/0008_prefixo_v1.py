"""prefixo `v1-` nas tabelas criadas pela pipeline nova

Pedido do dono: as tabelas CRIADAS pela pipeline nova (migrations 0004 a 0007) levam
`v1-` na frente do nome (`v1-raw_ipe_docs`, `v1-painel_comandos`, `v1-noticias`). Sao 25:
8 de painel_, 9 de fontes (CVM, B3, IPE, juiza) e as 8 de entrega.

Criterio: so o que nasceu a partir da 0004. As que ja existiam (0001-0003) mantem o nome
original, inclusive `raw_articles`, `raw_prices` e `raw_llm_runs`, que a pipeline nova le
mas nao criou. Ficam sem prefixo tambem `features_dataset_v1` (dataset v1 congelado) e
`alembic_version`.

So o NOME DA TABELA muda: `op.rename_table` no MySQL. Chaves primarias, unique, foreign
keys, indices e sequencias mantem o nome antigo (ex.: `ix_articles_origem`,
`uq_raw_articles_url_hash`). Isso e aceitavel: o SQLAlchemy nao depende do nome da
constraint para gravar nem para ler, e as FKs seguem a tabela renomeada.

Ordem de renomeacao: indiferente (as FKs apontam para o objeto, nao para o nome).

Downgrade desfaz os 25 renames.

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

# As 25 tabelas criadas a partir da 0004, sem prefixo (o nome que tinham antes desta migration).
TABELAS = (
    # painel_ (0004)
    "painel_comandos", "painel_heartbeat", "painel_execucoes", "painel_etapas",
    "painel_historico", "painel_historico_progresso", "painel_logs", "painel_comandos_aplicados",
    # fontes e juiza (0005, 0007)
    "raw_ipe_docs", "raw_cvm_balancos", "raw_cvm_acoes", "raw_cvm_cadastro", "raw_b3_cadastro",
    "raw_b3_proventos", "raw_b3_suplemento", "raw_llm_juiza_runs", "staging_noticias_julgamento",
    # entrega (0006)
    "noticias", "precos", "cadastro", "mudancas_ticker", "balancos", "acoes_emitidas",
    "proventos", "indices",
)


def upgrade() -> None:
    for nome in TABELAS:
        op.rename_table(nome, f"{PREFIXO}{nome}")


def downgrade() -> None:
    for nome in reversed(TABELAS):
        op.rename_table(f"{PREFIXO}{nome}", nome)
