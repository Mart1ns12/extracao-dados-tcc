"""Consulta de teste: imprime o resultado cru do driver (PyMySQL), sem formatacao.

Uso:
    python scripts/teste_consulta.py
    python scripts/teste_consulta.py "SELECT * FROM raw_prices LIMIT 3"
"""

from __future__ import annotations

import sys

from tcc_scraper.db.session import get_engine

SQL_PADRAO = "SELECT * FROM features_dataset_v1 ORDER BY data DESC LIMIT 3"


def main() -> None:
    sql = sys.argv[1] if len(sys.argv) > 1 else SQL_PADRAO

    conexao = get_engine().raw_connection()  # conexao PyMySQL pura
    try:
        cursor = conexao.cursor()
        cursor.execute(sql)
        print(cursor.description)
        print(cursor.fetchall())
    finally:
        conexao.close()


if __name__ == "__main__":
    main()
