"""Copia os dados de um banco para outro (PostgreSQL <-> MySQL).

Uso:
    python scripts/migrar_dados.py --origem "postgresql+psycopg://..." \\
                                   --destino "mysql+pymysql://...?charset=utf8mb4"

Pressupoe que o destino ja tem as tabelas (`alembic upgrade head`). Nao cria nem
apaga estrutura -- so move linhas.

Idempotente: usa o mesmo upsert do pipeline, entao rodar duas vezes nao duplica nada
e uma execucao interrompida pode ser repetida do zero sem limpar o destino.

A ordem das tabelas respeita as chaves estrangeiras: artigos antes de corpos,
sentimentos e vinculos. Inverter quebraria a integridade referencial no MySQL.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from tcc_scraper.db.models import (
    Article,
    ArticleBody,
    ArticleSentiment,
    ArticleTicker,
    CollectionRun,
    DatasetV1,
    Macro,
    Price,
)
from tcc_scraper.db.upsert import bulk_upsert

# (modelo, colunas que identificam a linha). Ordem = dependencia de FK.
TABELAS = [
    (Article, ["url_hash"]),
    (Price, ["ticker", "date"]),
    (Macro, ["series", "date"]),
    (CollectionRun, ["collector", "unit_key"]),
    (ArticleBody, ["article_id"]),
    (ArticleTicker, ["article_id", "ticker"]),
    (ArticleSentiment, ["article_id", "model"]),
    (DatasetV1, ["data", "ticker"]),
]

# Lido e escrito de uma vez. 2.000 linhas de artigo com corpo ficam bem abaixo do
# max_allowed_packet padrao do MySQL (64 MB) e evitam carregar tudo em memoria.
LOTE = 2_000


def migrar(origem_url: str, destino_url: str, *, dry_run: bool = False) -> int:
    origem = create_engine(origem_url)
    destino = create_engine(destino_url)

    print(f"origem : {origem.url.render_as_string(hide_password=True)}")
    print(f"destino: {destino.url.render_as_string(hide_password=True)}")
    if dry_run:
        print("\n[dry-run] nada sera escrito\n")
    print()

    total = 0
    with Session(origem) as src, Session(destino) as dst:
        for modelo, chaves in TABELAS:
            tabela = modelo.__table__
            n = src.execute(select(func.count()).select_from(tabela)).scalar_one()
            if n == 0:
                print(f"  {tabela.name:30s} vazia")
                continue

            colunas = [c.name for c in tabela.columns]
            # A PK autoincremento e copiada de proposito: `staging_article_tickers`
            # e `raw_article_bodies` apontam para `raw_articles.id`, e renumerar
            # quebraria os vinculos.
            copiadas = 0
            for offset in range(0, n, LOTE):
                linhas = [
                    dict(zip(colunas, row, strict=True))
                    for row in src.execute(
                        select(tabela).order_by(*[tabela.c[k] for k in chaves])
                        .offset(offset).limit(LOTE)
                    ).all()
                ]
                if not dry_run and linhas:
                    bulk_upsert(dst, tabela, linhas, index_elements=chaves,
                                update_columns=None)
                copiadas += len(linhas)
            if not dry_run:
                dst.commit()
            print(f"  {tabela.name:30s} {copiadas:7d} linhas")
            total += copiadas

    print(f"\ntotal: {total} linhas")
    return total


def conferir(origem_url: str, destino_url: str) -> bool:
    """Compara a contagem de linhas tabela a tabela."""
    origem, destino = create_engine(origem_url), create_engine(destino_url)
    ok = True
    print("\n=== conferencia ===")
    with Session(origem) as src, Session(destino) as dst:
        for modelo, _ in TABELAS:
            t = modelo.__table__
            a = src.execute(select(func.count()).select_from(t)).scalar_one()
            b = dst.execute(select(func.count()).select_from(t)).scalar_one()
            marca = "OK " if a == b else "DIF"
            if a != b:
                ok = False
            print(f"  {marca} {t.name:30s} origem={a:7d}  destino={b:7d}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Migra os dados entre dois bancos")
    ap.add_argument("--origem", required=True, help="URL SQLAlchemy de origem")
    ap.add_argument("--destino", required=True, help="URL SQLAlchemy de destino")
    ap.add_argument("--dry-run", action="store_true", help="So conta, nao escreve")
    args = ap.parse_args()

    migrar(args.origem, args.destino, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    return 0 if conferir(args.origem, args.destino) else 1


if __name__ == "__main__":
    sys.exit(main())
