"""Demonstracoes (ITR/DFP) e quantidade de acoes da CVM, por companhia e data de referencia.

Cada zip anual de ITR/DFP traz quatro CSVs que a entrega usa: o principal (data de
recebimento de cada documento), DRE e BPP consolidados e a composicao do capital. O
parser e puro; o coletor baixa o zip, filtra pelo universo e grava em lote, append-only.

Os valores vao como a CVM publica, sem aplicar ESCALA_MOEDA: a escala fica na coluna
escala_moeda e a conversao e responsabilidade da camada de features.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from collections.abc import Iterable
from zoneinfo import ZoneInfo

from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.collectors.cvm_base import (
    CVM_BASE,
    CvmCollector,
    cnpj_digitos,
    data_cvm,
    decimal_cvm,
    last_modified,
    ler_csvs_do_zip,
)
from tcc_scraper.config import CONTAS_CVM, TICKER_CODE_CVM
from tcc_scraper.db.models_fontes import RawCvmAcoes, RawCvmBalanco
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

# Sufixos dos CSVs dentro do zip (o principal depende do ano e e montado no process).
_SUFIXO_DRE = "_DRE_con_"
_SUFIXO_BPP = "_BPP_con_"
_SUFIXO_COMPOSICAO = "_composicao_capital_"

# Chaves do dict que parse_demonstracoes recebe: papel do CSV, independente do nome no zip.
CHAVE_PRINCIPAL = "principal"
CHAVE_DRE = "DRE"
CHAVE_BPP = "BPP"
CHAVE_COMPOSICAO = "composicao"

# Chaves primarias das tabelas raw (index_elements do bulk_upsert).
_PK_BALANCO = (
    "cnpj",
    "tipo_documento",
    "dt_refer",
    "versao",
    "demonstracao",
    "cd_conta",
    "dt_ini_exerc",
    "dt_fim_exerc",
)
_PK_ACOES = ("cnpj", "tipo_documento", "dt_refer", "versao")


def _inteiro(valor: str | None) -> int | None:
    """Inteiro de um campo da CVM. Vazio ou invalido vira None."""
    try:
        return int(valor.strip())
    except (AttributeError, ValueError):
        return None


def _quantidade(valor: str | None) -> int | None:
    """Quantidade de acoes. A CVM as vezes escreve "1234.0"; o banco guarda inteiro."""
    numero = decimal_cvm(valor)
    return int(numero) if numero is not None else None


def _ultimo(ordem: str | None) -> bool:
    """ORDEM_EXERC "ÚLTIMO" (ou "ULTIMO" sem acento): exercicio mais recente, nao o penultimo."""
    if not ordem:
        return False
    sem_acento = unicodedata.normalize("NFKD", ordem).encode("ascii", "ignore").decode("ascii")
    return sem_acento.strip().upper() == "ULTIMO"


def _balanco(
    r: dict,
    tipo: str,
    demonstracao: str,
    universo: set[int],
    recebimento: dict[tuple[str, dt.date | None, int | None], dt.date | None],
) -> dict | None:
    """Uma linha de DRE ou BPP. BPP e saldo em uma data: o inicio do exercicio e o proprio fim."""
    cd_cvm = _inteiro(r.get("CD_CVM"))
    cnpj = cnpj_digitos(r.get("CNPJ_CIA"))
    dt_refer = data_cvm(r.get("DT_REFER"))
    versao = _inteiro(r.get("VERSAO"))
    dt_fim = data_cvm(r.get("DT_FIM_EXERC"))
    dt_ini = data_cvm(r.get("DT_INI_EXERC")) if demonstracao == "DRE" else dt_fim
    valor = decimal_cvm(r.get("VL_CONTA"))
    if cd_cvm is None or cd_cvm not in universo or not cnpj or valor is None:
        return None
    if None in (dt_refer, versao, dt_ini, dt_fim):
        return None
    return {
        "cnpj": cnpj,
        "tipo_documento": tipo,
        "dt_refer": dt_refer,
        "versao": versao,
        "demonstracao": demonstracao,
        "cd_conta": r["CD_CONTA"],
        "dt_ini_exerc": dt_ini,
        "dt_fim_exerc": dt_fim,
        "cd_cvm": cd_cvm,
        "ds_conta": r.get("DS_CONTA") or None,
        "ordem_exerc": r["ORDEM_EXERC"],
        "escala_moeda": r.get("ESCALA_MOEDA") or "",
        "vl_conta": float(valor),
        "dt_receb": recebimento.get((cnpj, dt_refer, versao)),
    }


def parse_demonstracoes(
    csvs: dict[str, list[dict]], tipo: str, cd_cvm_universo: set[int]
) -> tuple[list[dict], list[dict]]:
    """Converte os CSVs de um zip ITR/DFP em linhas de raw_cvm_balancos e raw_cvm_acoes.

    csvs usa as chaves CHAVE_PRINCIPAL, CHAVE_DRE, CHAVE_BPP e CHAVE_COMPOSICAO. So entram
    companhias de cd_cvm_universo. Linha repetida na mesma chave primaria: vale a ultima.
    """
    cnpj_para_cd: dict[str, int] = {}
    recebimento: dict[tuple[str, dt.date | None, int | None], dt.date | None] = {}
    for r in csvs.get(CHAVE_PRINCIPAL, []):
        cnpj = cnpj_digitos(r.get("CNPJ_CIA"))
        cd_cvm = _inteiro(r.get("CD_CVM"))
        if cnpj and cd_cvm is not None:
            cnpj_para_cd[cnpj] = cd_cvm
        chave = (cnpj, data_cvm(r.get("DT_REFER")), _inteiro(r.get("VERSAO")))
        recebimento[chave] = data_cvm(r.get("DT_RECEB"))

    balancos: dict[tuple, dict] = {}
    for r in csvs.get(CHAVE_DRE, []):
        if not _ultimo(r.get("ORDEM_EXERC")) or r.get("CD_CONTA") not in CONTAS_CVM["DRE"]:
            continue
        linha = _balanco(r, tipo, "DRE", cd_cvm_universo, recebimento)
        if linha is not None:
            balancos[tuple(linha[c] for c in _PK_BALANCO)] = linha
    for r in csvs.get(CHAVE_BPP, []):
        if not _ultimo(r.get("ORDEM_EXERC")) or r.get("CD_CONTA") not in CONTAS_CVM["BPP"]:
            continue
        linha = _balanco(r, tipo, "BPP", cd_cvm_universo, recebimento)
        if linha is not None:
            balancos[tuple(linha[c] for c in _PK_BALANCO)] = linha

    acoes: dict[tuple, dict] = {}
    for r in csvs.get(CHAVE_COMPOSICAO, []):
        # A composicao nao traz CD_CVM: a companhia vem do CNPJ, pelo principal do mesmo zip.
        cnpj = cnpj_digitos(r.get("CNPJ_CIA"))
        cd_cvm = cnpj_para_cd.get(cnpj)
        dt_refer = data_cvm(r.get("DT_REFER"))
        versao = _inteiro(r.get("VERSAO"))
        if cd_cvm is None or cd_cvm not in cd_cvm_universo or None in (dt_refer, versao):
            continue
        linha = {
            "cnpj": cnpj,
            "tipo_documento": tipo,
            "dt_refer": dt_refer,
            "versao": versao,
            "cd_cvm": cd_cvm,
            "qt_on": _quantidade(r.get("QT_ACAO_ORDIN_CAP_INTEGR")),
            "qt_pn": _quantidade(r.get("QT_ACAO_PREF_CAP_INTEGR")),
            "qt_total": _quantidade(r.get("QT_ACAO_TOTAL_CAP_INTEGR")),
            "dt_receb": recebimento.get((cnpj, dt_refer, versao)),
        }
        acoes[tuple(linha[c] for c in _PK_ACOES)] = linha

    return list(balancos.values()), list(acoes.values())


class CvmDemonstracoesCollector(CvmCollector):
    """ITR e DFP da CVM: um zip por (tipo, ano). Grava raw_cvm_balancos e raw_cvm_acoes."""

    name = "cvm_itr_dfp"

    def __init__(
        self,
        anos: list[int] | None = None,
        tipos: tuple[str, ...] = ("ITR", "DFP"),
        **kw,
    ) -> None:
        super().__init__(**kw)
        if anos is None:
            anos = list(range(self.settings.data_start.year - 1, self.settings.fim_coleta.year + 1))
        self.anos = anos
        self.tipos = tipos

    def work_units(self) -> Iterable[WorkUnit]:
        hoje = dt.datetime.now(ZoneInfo(self.settings.daily_tz)).date()
        for tipo in self.tipos:
            for ano in self.anos:
                url = f"{CVM_BASE}/DOC/{tipo}/DADOS/{tipo.lower()}_cia_aberta_{ano}.zip"
                # Sem Last-Modified, a chave muda a cada dia e o zip e baixado de novo.
                lm = last_modified(self, url) or hoje.isoformat()
                yield WorkUnit(
                    key=f"{tipo}|{ano}|{lm}"[:128],
                    payload={"tipo": tipo, "ano": ano, "url": url},
                )

    def process(self, unit: WorkUnit) -> UnitResult:
        tipo, ano, url = unit.payload["tipo"], unit.payload["ano"], unit.payload["url"]
        principal = f"{tipo.lower()}_cia_aberta_{ano}.csv"
        brutos = ler_csvs_do_zip(
            self.baixar_zip(url),
            (principal, _SUFIXO_DRE, _SUFIXO_BPP, _SUFIXO_COMPOSICAO),
        )
        csvs = {
            CHAVE_PRINCIPAL: brutos[principal],
            CHAVE_DRE: brutos[_SUFIXO_DRE],
            CHAVE_BPP: brutos[_SUFIXO_BPP],
            CHAVE_COMPOSICAO: brutos[_SUFIXO_COMPOSICAO],
        }
        balancos, acoes = parse_demonstracoes(
            csvs, tipo, cd_cvm_universo=set(TICKER_CODE_CVM.values())
        )
        self._persist(balancos, acoes)
        return UnitResult(items_found=len(balancos) + len(acoes))

    def _persist(self, balancos: list[dict], acoes: list[dict]) -> None:
        """Append-only: update_columns=None, a linha ja gravada nunca e alterada."""
        agora = dt.datetime.now(dt.UTC)
        with session_scope() as session:
            if balancos:
                bulk_upsert(
                    session,
                    RawCvmBalanco.__table__,
                    [{**linha, "fetched_at": agora} for linha in balancos],
                    index_elements=_PK_BALANCO,
                    update_columns=None,
                )
            if acoes:
                bulk_upsert(
                    session,
                    RawCvmAcoes.__table__,
                    [{**linha, "fetched_at": agora} for linha in acoes],
                    index_elements=_PK_ACOES,
                    update_columns=None,
                )
