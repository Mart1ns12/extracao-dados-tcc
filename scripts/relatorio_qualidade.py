"""Relatorio de qualidade do banco, em texto simples. SOMENTE LEITURA (apenas SELECT).

Uso:
    .venv\\Scripts\\python.exe scripts/relatorio_qualidade.py

Secoes:
    a. Noticias por ano (ano em America/Sao_Paulo), por origem: total, precisao da data
       e quantas 'datetime' estao em 00:00:00 de America/Los_Angeles (hora suspeita).
    b. Pares (noticia, ticker) de staging_article_tickers sem julgamento da juiza na
       prompt_versao atual (llm/juiza_prompt.py VERSAO), por ano.
    c. Cobertura das series: raw_prices (^BVSP, IDIV/SMLL/IFIX) e raw_macro (CDI, IPCA).
    d. IBOV: dias em que open == close do pregao anterior (arredondado a 2 casas).
    e. v1-balancos: emprestimos zerados/NULL e aplicacoes_financeiras zeradas/NULL, por ticker.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from tcc_scraper.config import BENCHMARK_TICKER, INDICES_BCB, INDICES_YAHOO
from tcc_scraper.db.models import Article, ArticleTicker, Macro, Price
from tcc_scraper.db.models_entrega import Balanco, Cadastro
from tcc_scraper.db.models_fontes import StagingNoticiaJulgamento
from tcc_scraper.db.session import get_engine
from tcc_scraper.llm.juiza_prompt import VERSAO

FUSO_SP = ZoneInfo("America/Sao_Paulo")
FUSO_LA = ZoneInfo("America/Los_Angeles")


def tabela(cabecalho: list[str], linhas: list[list]) -> None:
    """Imprime uma tabela de texto alinhada (primeira coluna a esquerda, demais a direita)."""
    textos = [[str(c) for c in cabecalho]]
    textos += [["" if v is None else str(v) for v in linha] for linha in linhas]
    larguras = [max(len(linha[i]) for linha in textos) for i in range(len(cabecalho))]
    for indice, linha in enumerate(textos):
        partes = [
            c.ljust(larguras[j]) if j == 0 else c.rjust(larguras[j])
            for j, c in enumerate(linha)
        ]
        print("  " + "  ".join(partes))
        if indice == 0:
            print("  " + "  ".join("-" * w for w in larguras))


def percentual(parte: int, total: int) -> str:
    return f"{100 * parte / total:.1f}%" if total else "-"


def e_meia_noite_la(momento) -> bool:
    local = momento.astimezone(FUSO_LA)
    return local.hour == 0 and local.minute == 0 and local.second == 0


def secao_a(conn) -> None:
    print("a. NOTICIAS POR ANO (ano em America/Sao_Paulo)")
    print("   suspeita = precisao 'datetime' com horario 00:00:00 em America/Los_Angeles")
    print()
    linhas = conn.execute(
        select(Article.published_at, Article.origem, Article.published_precision)
    ).all()

    agregado: dict[tuple[str, int], Counter] = defaultdict(Counter)
    for momento, origem, precisao in linhas:
        ano = momento.astimezone(FUSO_SP).year
        suspeita = precisao == "datetime" and e_meia_noite_la(momento)
        for chave in ((origem, ano), ("TOTAL", ano)):
            c = agregado[chave]
            c["total"] += 1
            c[precisao if precisao in ("datetime", "date") else "outra"] += 1
            if suspeita:
                c["suspeita"] += 1

    origens = sorted({o for o, _ in agregado if o != "TOTAL"}) + ["TOTAL"]
    cabecalho = ["ano", "total", "datetime", "date", "outra", "suspeita", "% susp/datetime"]
    for origem in origens:
        print(f"  origem: {origem}")
        saida = []
        anos = sorted(a for o, a in agregado if o == origem)
        for ano in anos:
            c = agregado[(origem, ano)]
            saida.append([
                ano, c["total"], c["datetime"], c["date"], c["outra"], c["suspeita"],
                percentual(c["suspeita"], c["datetime"]),
            ])
        tabela(cabecalho, saida)
        print()


def secao_b(conn) -> None:
    print(f"b. PARES (noticia, ticker) SEM JULGAMENTO (prompt_versao={VERSAO})")
    print()
    pares = conn.execute(
        select(ArticleTicker.article_id, ArticleTicker.ticker, Article.published_at)
        .select_from(ArticleTicker)
        .join(Article, Article.id == ArticleTicker.article_id)
    ).all()
    julgados = {
        (artigo, ticker)
        for artigo, ticker in conn.execute(
            select(StagingNoticiaJulgamento.article_id, StagingNoticiaJulgamento.ticker).where(
                StagingNoticiaJulgamento.prompt_versao == VERSAO
            )
        ).all()
    }

    por_ano: dict[int, Counter] = defaultdict(Counter)
    for artigo, ticker, momento in pares:
        c = por_ano[momento.astimezone(FUSO_SP).year]
        c["total"] += 1
        if (artigo, ticker) in julgados:
            c["julgados"] += 1
        else:
            c["sem"] += 1

    saida = []
    total_geral = Counter()
    for ano in sorted(por_ano):
        c = por_ano[ano]
        total_geral.update(c)
        saida.append([
            ano, c["total"], c["julgados"], c["sem"],
            percentual(c["julgados"], c["total"]), percentual(c["sem"], c["total"]),
        ])
    saida.append([
        "TOTAL", total_geral["total"], total_geral["julgados"], total_geral["sem"],
        percentual(total_geral["julgados"], total_geral["total"]),
        percentual(total_geral["sem"], total_geral["total"]),
    ])
    tabela(["ano", "pares", "julgados", "sem julg.", "% julgados", "% sem julg."], saida)
    print()


def secao_c(conn) -> None:
    print("c. COBERTURA DAS SERIES")
    print()
    print("  raw_prices (ticker armazenado; indices do Yahoo sao gravados sem o .SA):")
    rotulos = [BENCHMARK_TICKER, *INDICES_YAHOO.values()]
    variantes: dict[str, list[str]] = {}
    for rotulo in rotulos:
        opcoes = [rotulo]
        if rotulo.endswith(".SA"):
            opcoes.append(rotulo.removesuffix(".SA"))
        variantes[rotulo] = opcoes
    todos = [v for opcoes in variantes.values() for v in opcoes]
    encontrados = {
        ticker: (minimo, maximo, n)
        for ticker, minimo, maximo, n in conn.execute(
            select(
                Price.ticker,
                func.min(Price.date),
                func.max(Price.date),
                func.count(),
            )
            .where(Price.ticker.in_(todos))
            .group_by(Price.ticker)
        ).all()
    }
    saida = []
    for rotulo in rotulos:
        achou = [v for v in variantes[rotulo] if v in encontrados]
        if not achou:
            saida.append([rotulo, "(nenhuma)", "-", "-", 0])
        for v in achou:
            minimo, maximo, n = encontrados[v]
            saida.append([rotulo, v, minimo, maximo, n])
    tabela(["simbolo", "armazenado como", "min(date)", "max(date)", "linhas"], saida)
    print()

    print("  raw_macro (BCB SGS, collectors/indices.py):")
    saida = []
    for serie, codigo in INDICES_BCB.items():
        minimo, maximo, n = conn.execute(
            select(func.min(Macro.date), func.max(Macro.date), func.count())
            .where(Macro.series == serie)
        ).one()
        saida.append([f"{serie.upper()} (SGS {codigo})", serie, minimo, maximo, n])
    tabela(["indice", "series", "min(date)", "max(date)", "linhas"], saida)
    print()


def secao_d(conn) -> None:
    print(f"d. {BENCHMARK_TICKER} (IBOV): open == close do pregao anterior")
    print()
    precos = conn.execute(
        select(Price.date, Price.open, Price.close)
        .where(Price.ticker == BENCHMARK_TICKER)
        .order_by(Price.date)
    ).all()

    total = iguais = descartados = 0
    exemplos: list[str] = []
    anterior = None
    for atual in precos:
        if anterior is not None:
            if atual.open is None or anterior.close is None:
                descartados += 1
            else:
                total += 1
                if round(atual.open, 2) == round(anterior.close, 2):
                    iguais += 1
                    if len(exemplos) < 10:
                        exemplos.append(atual.date.isoformat())
        anterior = atual

    tabela(
        ["pares (dia, anterior)", "open == close anterior", "%", "descartados (NULL)"],
        [[total, iguais, percentual(iguais, total), descartados]],
    )
    print(f"  exemplos (ate 10): {', '.join(exemplos) if exemplos else '-'}")
    print()


def secao_e(conn) -> None:
    print("e. v1-balancos: emprestimos e aplicacoes zerados ou NULL, por ticker")
    print("   linhas = (cnpj, tipo, periodo). Cnpj com mais de um ticker conta em cada um.")
    print()
    tickers_por_cnpj: dict[str, list[str]] = defaultdict(list)
    for ticker, cnpj in conn.execute(
        select(Cadastro.ticker, Cadastro.cnpj).where(Cadastro.cnpj.is_not(None))
    ).all():
        tickers_por_cnpj[cnpj].append(ticker)

    linhas = conn.execute(
        select(
            Balanco.cnpj,
            Balanco.emprestimos_curto_prazo,
            Balanco.emprestimos_longo_prazo,
            Balanco.aplicacoes_financeiras,
        )
    ).all()

    por_ticker: dict[str, Counter] = defaultdict(Counter)
    for cnpj, cp, lp, apl in linhas:
        for ticker in tickers_por_cnpj.get(cnpj) or ["(sem cadastro)"]:
            c = por_ticker[ticker]
            c["linhas"] += 1
            if cp == 0 or lp == 0:
                c["emp_zero"] += 1
            if apl == 0:
                c["apl_zero"] += 1
            if cp is None or lp is None:
                c["emp_null"] += 1
            if apl is None:
                c["apl_null"] += 1

    saida = []
    total = Counter()
    for ticker in sorted(por_ticker):
        c = por_ticker[ticker]
        total.update(c)
        saida.append([
            ticker, c["linhas"], c["emp_zero"], c["apl_zero"], c["emp_null"], c["apl_null"],
        ])
    saida.append([
        "TOTAL", total["linhas"], total["emp_zero"], total["apl_zero"],
        total["emp_null"], total["apl_null"],
    ])
    tabela(
        ["ticker", "linhas", "emp CP ou LP = 0", "aplic = 0", "emp CP ou LP NULL", "aplic NULL"],
        saida,
    )
    print()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    with get_engine().connect() as conn:
        secao_a(conn)
        secao_b(conn)
        secao_c(conn)
        secao_d(conn)
        secao_e(conn)


if __name__ == "__main__":
    main()
