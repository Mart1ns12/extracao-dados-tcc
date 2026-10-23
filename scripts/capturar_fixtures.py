"""Captura fixtures reais das fontes novas (CVM, B3, BCB, Yahoo) para os testes sem rede.

Re-executavel e idempotente: sobrescreve apenas os arquivos que ele mesmo gera. Precisa de
rede. Cada fonte e independente: se uma falhar, as demais continuam, a falha e listada no
fim e o script sai com codigo 1. Nada e inventado: o que nao foi baixado nao vira fixture.

Uso: ./.venv/Scripts/python.exe scripts/capturar_fixtures.py
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
import sys
import zipfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from tcc_scraper.collectors.base import Collector

RAIZ = Path(__file__).resolve().parents[1]
FIXTURES = RAIZ / "tests" / "fixtures"

CVM = "https://dados.cvm.gov.br/dados/CIA_ABERTA"
BCB = "https://api.bcb.gov.br/dados/serie/bcdata.sgs"
B3 = "https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall"
B3_INDICES = "https://sistemaswebb3-listados.b3.com.br/indexStatisticsProxy/IndexCall"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"

# Yahoo e B3 recusam o User-Agent padrao do projeto.
NAVEGADOR = {"User-Agent": "Mozilla/5.0"}
TIMEOUT_ZIP = httpx.Timeout(300.0)

# CD_CVM: Petrobras, Itau Unibanco, Itausa, Klabin, Vale. Comparado sem zeros a esquerda.
ALVO = {"9512", "19348", "7617", "12653", "4170"}
LIMITE_BYTES = 300 * 1024
IPE_PRIMEIRAS = 300
CADASTRO_PRIMEIRAS = 30


class _Capturador(Collector):
    """Subclasse minima: usa apenas fetch() (throttle + retry) da base."""

    name = "capturar_fixtures"
    default_delay = 1.0

    def work_units(self):
        return iter(())

    def process(self, unit):
        raise NotImplementedError


# -- rede ---------------------------------------------------------------------------
def baixar(col: Collector, url: str, **kwargs: Any) -> bytes:
    resposta = col.fetch(url, **kwargs)
    if resposta.status_code != 200:
        raise RuntimeError(f"HTTP {resposta.status_code} em {url}")
    return resposta.content


def b3_url(metodo: str, parametros: dict[str, Any]) -> str:
    codigo = json.dumps(parametros, separators=(",", ":"))
    return f"{B3}/{metodo}/{base64.b64encode(codigo.encode()).decode()}"


def _grava_b3(col: Collector, saida: str, metodo: str, parametros: dict[str, Any]) -> bytes:
    corpo = baixar(col, b3_url(metodo, parametros), headers=NAVEGADOR)
    json.loads(corpo)  # confirma que e JSON antes de gravar
    gravar(saida, corpo)
    return corpo


def _grava_b3_indice(col: Collector, saida: str, indice: str, ano: int) -> bytes:
    """Ano inteiro de um indice (IDIV, SMLL, IFIX) pelo indexStatisticsProxy da B3."""
    codigo = json.dumps(
        {"index": indice, "language": "pt-br", "year": str(ano)}, separators=(",", ":")
    )
    url = f"{B3_INDICES}/GetPortfolioDay/{base64.b64encode(codigo.encode()).decode()}"
    corpo = baixar(col, url, headers=NAVEGADOR)
    json.loads(corpo)  # confirma que e JSON antes de gravar
    gravar(saida, corpo)
    return corpo


def _proventos_b3(col: Collector, pregao: str) -> tuple[bytes, list]:
    corpo = baixar(
        col,
        b3_url(
            "GetListedCashDividends",
            {"language": "pt-br", "pageNumber": 1, "pageSize": 120, "tradingName": pregao},
        ),
        headers=NAVEGADOR,
    )
    return corpo, json.loads(corpo)["results"]


def capturar_proventos(col: Collector, saida: str, pregao: str, busca: str) -> bytes:
    """Proventos da primeira pagina. Se o pregao exato vier vazio, descobre o nome
    pelo GetInitialCompanies (tradingName e companyName) e usa o primeiro que traga dados."""
    corpo, resultados = _proventos_b3(col, pregao)
    if not resultados:
        busca_corpo = baixar(
            col,
            b3_url(
                "GetInitialCompanies",
                {"language": "pt-br", "pageNumber": 1, "pageSize": 20, "company": busca},
            ),
            headers=NAVEGADOR,
        )
        empresas = json.loads(busca_corpo)["results"]
        nomes = [n for e in empresas for n in (e["tradingName"], e["companyName"]) if n != pregao]
        for nome in dict.fromkeys(nomes):
            corpo, resultados = _proventos_b3(col, nome)
            if resultados:
                print(f"    pregao '{pregao}' sem dados; usado '{nome}'")
                break
        else:
            raise RuntimeError(f"nenhum nome de pregao com proventos para {busca!r}: {nomes}")
    gravar(saida, corpo)
    return corpo


# -- arquivos -----------------------------------------------------------------------
def gravar(nome: str, conteudo: bytes, linhas: int | None = None) -> None:
    caminho = FIXTURES / nome
    caminho.write_bytes(conteudo)
    detalhe = f", {linhas} linhas" if linhas is not None else ""
    aviso = "  ** acima de 300 KB" if len(conteudo) > LIMITE_BYTES else ""
    print(f"  {caminho.relative_to(RAIZ)}  {len(conteudo)} bytes{detalhe}{aviso}")


def _zip_csv(zip_bytes: bytes, membro: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        if membro not in zf.namelist():
            raise RuntimeError(f"{membro} nao esta no zip; membros: {zf.namelist()}")
        return zf.read(membro)


def _registros(bruto: bytes) -> list[tuple[bytes, list[str]]]:
    """Cada registro do CSV com seus bytes originais e campos parseados.

    Latin-1 mapeia 1 byte para 1 caractere, entao a copia sai byte a byte igual ao
    arquivo da CVM, qualquer que seja a codificacao real.
    """
    fisicas = list(io.StringIO(bruto.decode("latin-1"), newline=""))
    leitor = csv.reader(fisicas, delimiter=";")
    registros: list[tuple[bytes, list[str]]] = []
    inicio = 0
    for campos in leitor:
        fim = leitor.line_num
        if campos:
            registros.append(("".join(fisicas[inicio:fim]).encode("latin-1"), campos))
        inicio = fim
    return registros


def _ler(bruto: bytes) -> tuple[bytes, list[str], list[tuple[bytes, dict[str, str]]]]:
    registros = _registros(bruto)
    cabecalho, campos_cabecalho = registros[0]
    nomes = [n.strip() for n in campos_cabecalho]
    dados = [(cru, dict(zip(nomes, campos, strict=False))) for cru, campos in registros[1:]]
    return cabecalho, nomes, dados


def _amostra(
    cabecalho: bytes,
    dados: list[tuple[bytes, dict[str, str]]],
    escolhe: Callable[[int, dict[str, str]], bool],
) -> tuple[bytes, int]:
    escolhidas = [cru for i, (cru, linha) in enumerate(dados) if escolhe(i, linha)]
    return cabecalho + b"".join(escolhidas), len(escolhidas)


def _cvm(valor: str) -> str:
    """CD_CVM: a CVM grava com zeros a esquerda ("009512") em alguns arquivos e sem em outros."""
    return valor.strip().lstrip("0")


def _cnpj(valor: str) -> str:
    return re.sub(r"\D", "", valor)


def _capturar_csv(
    bruto: bytes,
    saida: str,
    *,
    colunas: tuple[str, ...] = ("CD_CVM",),
    alvos: set[str] = ALVO,
    normaliza: Callable[[str], str] = _cvm,
    primeiras: int = 0,
    prefixo_conta: str | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Grava a amostra: linhas cuja coluna (a primeira de `colunas` presente) esta em
    `alvos`, mais as `primeiras` linhas. Se passar de 300 KB e houver `prefixo_conta`,
    fica so com as contas que comecam com ele. Devolve (nomes, todas as linhas lidas)."""
    if not alvos:
        raise RuntimeError("conjunto de alvos vazio (dependencia nao capturada)")
    cabecalho, nomes, dados = _ler(bruto)
    coluna = next((c for c in colunas if c in nomes), None)
    if coluna is None:
        raise RuntimeError(f"nenhuma de {colunas} em {nomes[:12]}")

    def alvo(linha: dict[str, str]) -> bool:
        return normaliza(linha.get(coluna, "")) in alvos

    bruto_amostra, linhas = _amostra(
        cabecalho, dados, lambda i, linha: i < primeiras or alvo(linha)
    )
    if prefixo_conta and len(bruto_amostra) > LIMITE_BYTES:
        print(f"    acima de 300 KB: restrito a contas '{prefixo_conta}*'")
        bruto_amostra, linhas = _amostra(
            cabecalho,
            dados,
            lambda _i, linha: alvo(linha)
            and linha.get("CD_CONTA", "").strip().startswith(prefixo_conta),
        )
    gravar(saida, bruto_amostra, linhas)
    presentes = {normaliza(linha.get(coluna, "")) for _, linha in dados}
    ausentes = sorted(alvos - presentes)
    if ausentes:
        print(f"    alvos ausentes em {coluna}: {ausentes}")
    return nomes, [linha for _, linha in dados]


def _capturar_membro(
    zip_bytes: bytes, membro: str, saida: str, extra: dict[str, Any]
) -> tuple[list[str], list[dict[str, str]]]:
    return _capturar_csv(_zip_csv(zip_bytes, membro), saida, **extra)


# -- capturas -----------------------------------------------------------------------
def _executar(erros: list[str], nome: str, funcao: Callable[..., Any], *args: Any) -> Any:
    try:
        return funcao(*args)
    except Exception as exc:
        print(f"  FALHOU {nome}: {exc}")
        erros.append(f"{nome}: {exc}")
        return None


def capturar_ipe(col: Collector) -> Counter[str]:
    zip_bytes = baixar(col, f"{CVM}/DOC/IPE/DADOS/ipe_cia_aberta_2026.zip", timeout=TIMEOUT_ZIP)
    nomes, linhas = _capturar_membro(
        zip_bytes,
        "ipe_cia_aberta_2026.csv",
        "cvm_ipe_2026_amostra.csv",
        {"colunas": ("CD_CVM", "Codigo_CVM"), "primeiras": IPE_PRIMEIRAS},
    )
    if "Categoria" not in nomes:
        raise RuntimeError(f"coluna Categoria ausente em {nomes[:12]}")
    return Counter(linha.get("Categoria", "") for linha in linhas)


def capturar_periodo(
    col: Collector, tipo: str, ano: int, cnpjs: set[str], erros: list[str]
) -> None:
    prefixo = tipo.lower()
    zip_bytes = baixar(
        col, f"{CVM}/DOC/{tipo}/DADOS/{prefixo}_cia_aberta_{ano}.zip", timeout=TIMEOUT_ZIP
    )
    # composicao_capital nao tem CD_CVM: filtra pelo CNPJ do cadastro.
    membros = [
        (f"{prefixo}_cia_aberta_{ano}.csv", f"cvm_{prefixo}_{ano}_principal_amostra.csv", {}),
        (
            f"{prefixo}_cia_aberta_DRE_con_{ano}.csv",
            f"cvm_{prefixo}_{ano}_dre_con_amostra.csv",
            {"prefixo_conta": "3."},
        ),
        (
            f"{prefixo}_cia_aberta_BPP_con_{ano}.csv",
            f"cvm_{prefixo}_{ano}_bpp_con_amostra.csv",
            {"prefixo_conta": "2.03"},
        ),
        (
            f"{prefixo}_cia_aberta_composicao_capital_{ano}.csv",
            f"cvm_{prefixo}_{ano}_composicao_capital_amostra.csv",
            {"colunas": ("CNPJ_CIA",), "alvos": cnpjs, "normaliza": _cnpj},
        ),
    ]
    for membro, saida, extra in membros:
        _executar(erros, saida, _capturar_membro, zip_bytes, membro, saida, extra)


def capturar_cadastro(col: Collector) -> set[str]:
    """Grava a amostra do cadastro e devolve os CNPJs das companhias-alvo."""
    bruto = baixar(col, f"{CVM}/CAD/DADOS/cad_cia_aberta.csv")
    nomes, linhas = _capturar_csv(
        bruto, "cvm_cad_cia_aberta_amostra.csv", primeiras=CADASTRO_PRIMEIRAS
    )
    if "CNPJ_CIA" not in nomes:
        raise RuntimeError(f"coluna CNPJ_CIA ausente em {nomes[:12]}")
    cnpjs = {_cnpj(linha["CNPJ_CIA"]) for linha in linhas if _cvm(linha.get("CD_CVM", "")) in ALVO}
    print(f"    CNPJs-alvo: {sorted(cnpjs)}")
    return cnpjs


def _valores(obj: Any, chave: str, contador: Counter[str]) -> None:
    """Conta os valores de `chave` em qualquer profundidade do JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == chave and isinstance(v, str):
                contador[v] += 1
            else:
                _valores(v, chave, contador)
    elif isinstance(obj, list):
        for item in obj:
            _valores(item, chave, contador)


def main() -> int:
    erros: list[str] = []
    proventos: list[bytes] = []
    with _Capturador() as col:
        print("== CVM cadastro (antes: fornece os CNPJs-alvo)")
        cnpjs = _executar(erros, "cvm_cad_cia_aberta", capturar_cadastro, col) or set()

        print("== CVM IPE 2026")
        categorias = _executar(erros, "cvm_ipe_2026", capturar_ipe, col) or Counter()

        print("== CVM ITR 2025")
        _executar(erros, "ITR 2025", capturar_periodo, col, "ITR", 2025, cnpjs, erros)
        print("== CVM DFP 2024")
        _executar(erros, "DFP 2024", capturar_periodo, col, "DFP", 2024, cnpjs, erros)

        print("== B3 cadastro e suplemento")
        b3 = [
            ("b3_getdetail_9512.json", "GetDetail", {"codeCVM": "9512", "language": "pt-br"}),
            ("b3_getdetail_12653.json", "GetDetail", {"codeCVM": "12653", "language": "pt-br"}),
            (
                "b3_supplement_PETR.json",
                "GetListedSupplementCompany",
                {"issuingCompany": "PETR", "language": "pt-br"},
            ),
            (
                "b3_supplement_KLBN.json",
                "GetListedSupplementCompany",
                {"issuingCompany": "KLBN", "language": "pt-br"},
            ),
        ]
        for saida, metodo, parametros in b3:
            _executar(erros, saida, _grava_b3, col, saida, metodo, parametros)

        print("== B3 indices IDIV, SMLL, IFIX (2022)")
        for indice in ("IDIV", "SMLL", "IFIX"):
            _executar(
                erros,
                f"b3_indices_{indice}_2022",
                _grava_b3_indice,
                col,
                f"b3_indices_{indice.lower()}_2022.json",
                indice,
                2022,
            )

        print("== B3 proventos (pagina 1)")
        for saida, pregao, busca in [
            ("b3_cashdividends_PETROBRAS_p1.json", "PETROBRAS", "PETROBRAS"),
            ("b3_cashdividends_KLABIN_p1.json", "KLABIN S/A", "KLABIN"),
            ("b3_cashdividends_USIMINAS_p1.json", "USIMINAS", "USIMINAS"),
        ]:
            corpo = _executar(erros, saida, capturar_proventos, col, saida, pregao, busca)
            if corpo is not None:
                proventos.append(corpo)

        print("== BCB SGS 12 e 433 (2026)")
        for serie in (12, 433):
            url = f"{BCB}.{serie}/dados?formato=json&dataInicial=01/01/2026&dataFinal=31/12/2026"
            _executar(
                erros,
                f"bcb_sgs_{serie}",
                lambda s=serie, u=url: gravar(f"bcb_sgs_{s}_2026.json", baixar(col, u)),
            )

        print("== Yahoo indices")
        for ticker, saida in [
            ("IDIV.SA", "yahoo_chart_idiv.json"),
            ("IVVB11.SA", "yahoo_chart_ivvb11.json"),
            ("SMLL.SA", "yahoo_chart_smll.json"),
        ]:
            url = f"{YAHOO}/{ticker}?range=1mo&interval=1d"
            _executar(
                erros,
                saida,
                lambda s=saida, u=url: gravar(s, baixar(col, u, headers=NAVEGADOR)),
            )

    print("== Resumo")
    tipos: Counter[str] = Counter()
    acoes: Counter[str] = Counter()
    for corpo in proventos:
        dados_json = json.loads(corpo)
        _valores(dados_json, "typeStock", tipos)
        _valores(dados_json, "corporateAction", acoes)
    print(f"typeStock (3 JSONs de proventos): {dict(tipos.most_common())}")
    print(f"corporateAction (3 JSONs de proventos): {dict(acoes.most_common())}")
    print(f"Categoria (IPE 2026, arquivo inteiro): {dict(categorias.most_common())}")

    if erros:
        print("FALHAS:")
        for erro in erros:
            print(f"  - {erro}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
