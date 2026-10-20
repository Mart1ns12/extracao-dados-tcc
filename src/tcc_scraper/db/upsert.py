"""Upsert em lote no MySQL 8, ponto unico de escrita do projeto.

Todo `_persist` passa por `bulk_upsert`, que traduz para `ON DUPLICATE KEY UPDATE`.
Centralizar aqui evita que cada coletor reimplemente o upsert e a deduplicacao.

**Lote.** Gravar uma linha por `session.execute()` dentro de um laco vira centenas de
milhares de idas ao banco numa carga completa. Passando a lista inteira, o SQLAlchemy
emite um `executemany` e o numero de idas cai para a quantidade de lotes.

Diferenca do MySQL que o codigo precisa respeitar: ele dispara o conflito em QUALQUER
chave unica violada, nao so na indicada em `index_elements`. Por isso cada tabela que
passa por aqui deve ter uma unica restricao relevante -- se uma segunda chave unica
for adicionada, reveja este modulo.
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

    Isto nao e zelo: a resposta do Google News repete materia dentro de uma mesma
    janela. Sem esta deduplicacao, o MySQL aplicaria as duas ocorrencias em sequencia,
    e o resultado dependeria de detalhes do servidor. Deduplicar antes deixa o
    resultado deterministico.

    Manter a ultima ocorrencia privilegia o dado mais recente da resposta.
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
    o MySQL nao informa isso de forma confiavel num executemany).
    """
    if not rows:
        return 0

    # Obrigatorio: ver deduplicate().
    rows = deduplicate(rows, index_elements)

    # Guarda, nao ramo: sem ela, um banco que nao seja MySQL (ex.: SQLite em teste)
    # ignoraria a semantica de upsert em silencio.
    dialect = session.get_bind().dialect.name
    if dialect not in ("mysql", "mariadb"):
        raise NotImplementedError(
            f"upsert so e implementado para MySQL; dialeto atual: {dialect!r}"
        )
    total = 0
    for chunk in _chunks(rows, chunk_size):
        _upsert_mysql(session, table, chunk, index_elements, update_columns)
        total += len(chunk)
    return total


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
