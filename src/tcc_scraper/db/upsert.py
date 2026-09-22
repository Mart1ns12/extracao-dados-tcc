"""Upsert em lote, portavel entre PostgreSQL e MySQL.

Dois problemas resolvidos aqui:

**Portabilidade.** `ON CONFLICT` e sintaxe exclusiva do PostgreSQL; o MySQL usa
`ON DUPLICATE KEY UPDATE`. Sem esta camada, migrar de banco exigiria reescrever os
seis metodos `_persist` do projeto.

**Lote.** A versao anterior gravava uma linha por `session.execute()` dentro de um
laco. Numa carga de ~150 mil artigos isso vira ~450 mil idas ao banco. Passando a
lista inteira de uma vez, o SQLAlchemy emite um `executemany` e o numero de idas cai
para a quantidade de lotes.

Uma diferenca de comportamento que NAO da para abstrair: o PostgreSQL permite dizer
em qual restricao o conflito e esperado (`index_elements`); o MySQL dispara em
qualquer chave unica violada. Na pratica do projeto isso nao muda o resultado, porque
cada tabela tem uma unica restricao relevante -- mas esta documentado para quem
adicionar uma segunda chave unica no futuro.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Table
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Acima disso, um unico INSERT gera uma query grande demais para o servidor
# (max_allowed_packet no MySQL). Dividir em blocos mantem o ganho do lote sem
# esbarrar no limite.
DEFAULT_CHUNK_SIZE = 500


def _chunks(rows: Sequence[dict], size: int):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def deduplicate(rows: Sequence[dict], index_elements: Sequence[str]) -> list[dict]:
    """Remove linhas com a mesma chave dentro do lote, mantendo a ULTIMA.

    Isto nao e zelo: o PostgreSQL ABORTA um `ON CONFLICT DO UPDATE` cujo lote traga
    a mesma chave duas vezes -- "cannot affect row a second time" -- enquanto o MySQL
    aceita em silencio. Como a resposta do Google News repete materia dentro de uma
    mesma janela, sem esta deduplicacao a coleta quebraria no PostgreSQL e produziria
    resultado dependente do banco.

    Manter a ultima ocorrencia reproduz o comportamento efetivo do MySQL e privilegia
    o dado mais recente da resposta.
    """
    vistos: dict[tuple, dict] = {}
    for row in rows:
        vistos[tuple(row.get(col) for col in index_elements)] = row
    return list(vistos.values())


def bulk_upsert(
    session: Session,
    table: Table,
    rows: Sequence[dict[str, Any]],
    *,
    index_elements: Sequence[str],
    update_columns: Sequence[str] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Insere `rows` resolvendo conflitos de chave, em lote.

    index_elements: colunas da restricao unica onde o conflito e esperado.
    update_columns: colunas a sobrescrever quando a linha ja existe. `None` ou lista
        vazia significa "nao faca nada" -- a linha existente e preservada.

    Devolve a quantidade de linhas enviadas (nao a de linhas efetivamente escritas:
    nenhum dos dois bancos informa isso de forma confiavel num executemany).
    """
    if not rows:
        return 0

    # Obrigatorio, nao opcional: sem isto o PostgreSQL aborta o lote com chave
    # repetida. Ver deduplicate().
    rows = deduplicate(rows, index_elements)

    dialect = session.get_bind().dialect.name
    total = 0
    for chunk in _chunks(rows, chunk_size):
        if dialect == "postgresql":
            _upsert_postgresql(session, table, chunk, index_elements, update_columns)
        elif dialect in ("mysql", "mariadb"):
            _upsert_mysql(session, table, chunk, index_elements, update_columns)
        else:
            raise NotImplementedError(
                f"upsert nao implementado para o dialeto {dialect!r}; "
                "suportados: postgresql, mysql, mariadb"
            )
        total += len(chunk)
    return total


def _upsert_postgresql(
    session: Session,
    table: Table,
    rows: Sequence[dict],
    index_elements: Sequence[str],
    update_columns: Sequence[str] | None,
) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(table).values(list(rows))
    if update_columns:
        stmt = stmt.on_conflict_do_update(
            index_elements=list(index_elements),
            set_={col: stmt.excluded[col] for col in update_columns},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=list(index_elements))
    session.execute(stmt)


def _upsert_mysql(
    session: Session,
    table: Table,
    rows: Sequence[dict],
    index_elements: Sequence[str],
    update_columns: Sequence[str] | None,
) -> None:
    from sqlalchemy.dialects.mysql import insert as mysql_insert

    stmt = mysql_insert(table).values(list(rows))
    if update_columns:
        stmt = stmt.on_duplicate_key_update(
            **{col: stmt.inserted[col] for col in update_columns}
        )
    else:
        # O MySQL nao tem "do nothing". `INSERT IGNORE` existe, mas engole TODOS os
        # erros -- inclusive truncagem de dado e tipo invalido -- e transformaria um
        # bug em silencio. Atribuir uma coluna da chave a si mesma e um no-op que
        # so neutraliza o conflito de chave duplicada.
        key = index_elements[0]
        stmt = stmt.on_duplicate_key_update(**{key: stmt.inserted[key]})
    session.execute(stmt)
