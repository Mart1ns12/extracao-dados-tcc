"""Gera o mapa ticker -> codigo CVM a partir dos endpoints da B3 (precisa de rede).

Uso:
    python scripts/gerar_mapas_b3_cvm.py > scratch/mapa_cvm.txt

Imprime um bloco `TICKER_CODE_CVM` pronto para colar em config.py, a lista de tickers
sem casamento e uma sugestao de NOMES_EMPRESA (so sugestao: nao altera nada).

O codigo CVM e o identificador estavel da companhia. A busca da B3 e por nome, entao o
casamento e conferido duas vezes: o `issuingCompany` da busca tem que ser o prefixo do
ticker, e o ticker tem que aparecer em `otherCodes` do detalhe da companhia.

Os GETs passam por `Collector.fetch` (throttle por dominio + retry), como todo coletor.
"""

from __future__ import annotations

import base64
import json
import logging
import sys

import httpx

from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.config import NOMES_EMPRESA, universo_coleta

logger = logging.getLogger("gerar_mapas_b3_cvm")

HOST = "sistemaswebb3-listados.b3.com.br"
BASE = f"https://{HOST}/listedCompaniesProxy/CompanyCall"
DELAY_S = 1.0
TAMANHO_PAGINA = 20
MAX_PAGINAS = 5
HEADERS = {"User-Agent": "Mozilla/5.0"}
CONECTIVOS = {"DO", "DA", "DE", "DOS", "DAS", "E"}


class ColetorB3(Collector):
    """So faz GETs com throttle; o casamento fica nas funcoes do script."""

    name = "b3_cvm"
    default_delay = DELAY_S

    def work_units(self):
        return iter(())

    def process(self, unit: WorkUnit) -> UnitResult:
        raise NotImplementedError


def _chamar(coletor: ColetorB3, metodo: str, params: dict) -> dict | None:
    """JSON (objeto) da B3, ou None para resposta nao-200, HTML ou vazia.

    Erro de rede levanta RuntimeError (vindo do `fetch`); quem chama decide o que fazer.
    """
    codificado = base64.b64encode(json.dumps(params, separators=(",", ":")).encode()).decode()
    resposta: httpx.Response = coletor.fetch(f"{BASE}/{metodo}/{codificado}", headers=HEADERS)
    if resposta.status_code != 200:
        return None
    try:
        dados = resposta.json()
    except ValueError:
        return None
    return dados if isinstance(dados, dict) else None


def _candidatos(coletor: ColetorB3, termo: str, prefixo: str) -> list[dict]:
    """Resultados da busca por `termo` cujo issuingCompany e o prefixo do ticker."""
    achados: list[dict] = []
    pagina = 1
    while True:
        dados = _chamar(
            coletor,
            "GetInitialCompanies",
            {
                "language": "pt-br",
                "pageNumber": pagina,
                "pageSize": TAMANHO_PAGINA,
                "company": termo,
            },
        )
        if dados is None:
            break
        for item in dados.get("results") or []:
            if isinstance(item, dict) and str(item.get("issuingCompany", "")).upper() == prefixo:
                achados.append(item)
        total = int((dados.get("page") or {}).get("totalPages") or 1)
        if pagina >= min(total, MAX_PAGINAS):
            break
        pagina += 1
    return achados


def resolver(coletor: ColetorB3, ticker: str) -> tuple[int, str] | None:
    """(codeCVM, tradingName) da companhia que lista `ticker`, ou None se nao casar."""
    prefixo = ticker[:4]
    visitados: set[str] = set()
    for termo in (prefixo, NOMES_EMPRESA.get(ticker, "")):
        if not termo:
            continue
        for candidato in _candidatos(coletor, termo, prefixo):
            cvm = str(candidato.get("codeCVM", "")).strip()
            if not cvm or cvm in visitados:
                continue
            visitados.add(cvm)
            detalhe = _chamar(coletor, "GetDetail", {"codeCVM": cvm, "language": "pt-br"})
            if detalhe is None:
                continue
            codigos = {detalhe.get("code")}
            for outro in detalhe.get("otherCodes") or []:
                if isinstance(outro, dict):
                    codigos.add(outro.get("code"))
            if ticker in codigos:
                return int(cvm), str(detalhe.get("tradingName") or "")
    return None


def sugerir_nome(trading: str) -> str:
    """Caixa normal a partir do tradingName da B3 (ex.: 'PETROBRAS' -> 'Petrobras')."""
    palavras = []
    for p in trading.split():
        if p in CONECTIVOS:
            palavras.append(p.lower())
        elif p.isupper() and (len(p) <= 2 or any(c.isdigit() for c in p)):
            palavras.append(p)
        else:
            palavras.append(p.capitalize())
    return " ".join(palavras)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    coletor = ColetorB3()
    coletor.throttle.configure(HOST, DELAY_S)

    mapa: dict[str, int] = {}
    sugestoes: dict[str, tuple[str, str]] = {}
    sem_casamento: list[str] = []
    falha_rede: list[str] = []
    try:
        for ticker in universo_coleta():
            try:
                achado = resolver(coletor, ticker)
            except RuntimeError as exc:
                logger.error("%s: %s", ticker, exc)
                falha_rede.append(ticker)
                continue
            if achado is None:
                logger.info("%s: sem casamento", ticker)
                sem_casamento.append(ticker)
                continue
            cvm, trading = achado
            mapa[ticker] = cvm
            sugerido = sugerir_nome(trading)
            if sugerido and sugerido.lower() != NOMES_EMPRESA.get(ticker, "").lower():
                sugestoes[ticker] = (NOMES_EMPRESA.get(ticker, ""), sugerido)
            logger.info("%s -> %d (%s)", ticker, cvm, trading)
    finally:
        coletor.close()

    print("# TICKER_CODE_CVM gerado (cole em config.py)")
    print("TICKER_CODE_CVM: dict[str, int] = {")
    for ticker in sorted(mapa):
        print(f'    "{ticker}": {mapa[ticker]},')
    print("}")
    print()
    print(f"# Sem casamento ({len(sem_casamento)}): {', '.join(sem_casamento) or '-'}")
    print(f"# Falha de rede ({len(falha_rede)}): {', '.join(falha_rede) or '-'}")
    print()
    print("# Sugestao de NOMES_EMPRESA (so sugestao, nada foi alterado):")
    for ticker in sorted(sugestoes):
        atual, sugerido = sugestoes[ticker]
        print(f'#   "{ticker}": "{atual}" -> "{sugerido}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
