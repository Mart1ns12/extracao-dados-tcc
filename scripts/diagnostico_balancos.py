"""Diagnostico de balancos: CVM bruto (v1-raw_cvm_balancos) x v1-balancos. SOMENTE LEITURA.

Uso:
    .venv\\Scripts\\python.exe scripts/diagnostico_balancos.py
    .venv\\Scripts\\python.exe scripts/diagnostico_balancos.py CSNA3 PETR4
    .venv\\Scripts\\python.exe scripts/diagnostico_balancos.py --todos-periodos

Para cada ticker: cd_cvm pelo TICKER_CODE_CVM (config.py) e cnpj pelo cadastro
(v1-cadastro). Pega o dt_refer mais recente da empresa e, nele, a maior VERSAO de cada
tipo_documento. Lista as linhas de v1-raw_cvm_balancos com cd_conta iniciando em
2.01.04, 2.02.01, 1.01.01 e 1.01.02, e a linha correspondente em v1-balancos
(emprestimos_*, caixa, aplicacoes_financeiras).

--todos-periodos repete o mesmo para todos os dt_refer de cada empresa.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import or_, select

from tcc_scraper.config import TICKER_CODE_CVM
from tcc_scraper.db.models_entrega import Balanco, Cadastro
from tcc_scraper.db.models_fontes import RawCvmBalanco
from tcc_scraper.db.session import get_engine

TICKERS_PADRAO = [
    "CSNA3", "SBSP3", "RDOR3", "PCAR3", "MGLU3", "LWSA3",
    "ALOS3", "CMIN3", "SAUD3", "AUAU3", "TIMS3", "JBSS32",
]
PREFIXOS_CONTA = ("2.01.04", "2.02.01", "1.01.01", "1.01.02")


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


def periodos_da_empresa(conn, cd_cvm: int) -> dict[tuple[str, object, str], int]:
    """(cnpj, dt_refer, tipo_documento) -> maior VERSAO daquele periodo."""
    R = RawCvmBalanco
    maiores: dict[tuple[str, object, str], int] = {}
    for cnpj, dt_refer, tipo, versao in conn.execute(
        select(R.cnpj, R.dt_refer, R.tipo_documento, R.versao)
        .where(R.cd_cvm == cd_cvm)
        .distinct()
    ).all():
        chave = (cnpj, dt_refer, tipo)
        if versao > maiores.get(chave, -1):
            maiores[chave] = versao
    return maiores


def contas_do_periodo(conn, cnpj: str, dt_refer, tipo: str, versao: int) -> list:
    R = RawCvmBalanco
    filtro_conta = or_(*[R.cd_conta.like(f"{prefixo}%") for prefixo in PREFIXOS_CONTA])
    return conn.execute(
        select(
            R.demonstracao, R.cd_conta, R.ds_conta, R.vl_conta,
            R.dt_ini_exerc, R.dt_fim_exerc, R.ordem_exerc, R.escala_moeda,
        )
        .where(
            R.cnpj == cnpj,
            R.dt_refer == dt_refer,
            R.tipo_documento == tipo,
            R.versao == versao,
            filtro_conta,
        )
        .order_by(R.demonstracao, R.cd_conta, R.dt_ini_exerc, R.ordem_exerc)
    ).all()


def linhas_v1_balancos(conn, cnpj: str, dt_refer, tipo: str) -> list:
    B = Balanco
    return conn.execute(
        select(
            B.data_inicio_periodo, B.data_fim_periodo, B.emprestimos_curto_prazo,
            B.emprestimos_longo_prazo, B.caixa, B.aplicacoes_financeiras,
        )
        .where(B.cnpj == cnpj, B.data_fim_periodo == dt_refer, B.tipo_documento == tipo)
        .order_by(B.data_inicio_periodo)
    ).all()


def diagnosticar_ticker(conn, ticker: str, todos_periodos: bool) -> None:
    cd_cvm = TICKER_CODE_CVM.get(ticker)
    print("=" * 100)
    if cd_cvm is None:
        print(f"{ticker}: sem cd_cvm no TICKER_CODE_CVM (config.py)")
        print()
        return

    cnpj_cadastro = conn.execute(
        select(Cadastro.cnpj).where(Cadastro.ticker == ticker)
    ).scalar()
    maiores = periodos_da_empresa(conn, cd_cvm)
    if not maiores:
        print(f"{ticker} (cd_cvm={cd_cvm}): sem linhas em v1-raw_cvm_balancos")
        print()
        return

    cnpjs = sorted({cnpj for cnpj, _, _ in maiores})
    print(f"{ticker} | cd_cvm={cd_cvm} | cnpj cadastro={cnpj_cadastro} | cnpj CVM={', '.join(cnpjs)}")
    if cnpj_cadastro and cnpj_cadastro not in cnpjs:
        print("  AVISO: cnpj do cadastro diferente do cnpj da CVM; usando o da CVM")

    if todos_periodos:
        selecionados = sorted(maiores, key=lambda chave: (chave[1], chave[2]))
    else:
        ultimo = max(dt for _, dt, _ in maiores)
        selecionados = sorted(
            (chave for chave in maiores if chave[1] == ultimo), key=lambda chave: chave[2]
        )

    for cnpj, dt_refer, tipo in selecionados:
        versao = maiores[(cnpj, dt_refer, tipo)]
        print()
        print(f"  dt_refer={dt_refer.isoformat()} tipo={tipo} versao={versao} (maior VERSAO)")
        contas = contas_do_periodo(conn, cnpj, dt_refer, tipo, versao)
        if not contas:
            print("    sem linhas com as contas 2.01.04 / 2.02.01 / 1.01.01 / 1.01.02")
        else:
            linhas = [
                [
                    demo, cd, ds, vl,
                    ini.isoformat(), fim.isoformat(), ordem, escala,
                ]
                for demo, cd, ds, vl, ini, fim, ordem, escala in contas
            ]
            tabela(
                ["demo", "cd_conta", "ds_conta", "vl_conta", "dt_ini", "dt_fim", "ordem", "escala"],
                linhas,
            )

        v1 = linhas_v1_balancos(conn, cnpj, dt_refer, tipo)
        print("  v1-balancos:")
        if not v1:
            print("    sem linha em v1-balancos para este cnpj/tipo/data_fim")
        else:
            tabela(
                ["data_inicio", "data_fim", "emprestimos_cp", "emprestimos_lp", "caixa",
                 "aplicacoes_financeiras"],
                [
                    [
                        ini.isoformat(), fim.isoformat(), cp, lp, caixa, apl,
                    ]
                    for ini, fim, cp, lp, caixa, apl in v1
                ],
            )
    print()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Diagnostico de balancos (somente leitura).")
    parser.add_argument("tickers", nargs="*", default=TICKERS_PADRAO, help="tickers (padrao: lista fixa)")
    parser.add_argument(
        "--todos-periodos", action="store_true", help="todos os dt_refer, nao so o mais recente"
    )
    args = parser.parse_args()

    with get_engine().connect() as conn:
        for ticker in args.tickers:
            diagnosticar_ticker(conn, ticker.upper(), args.todos_periodos)


if __name__ == "__main__":
    main()
