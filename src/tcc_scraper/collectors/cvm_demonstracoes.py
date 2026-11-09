"""Demonstracoes (ITR/DFP) e quantidade de acoes da CVM, por companhia e data de referencia.

Cada zip anual de ITR/DFP traz dez CSVs que a entrega usa: o principal (data de recebimento
de cada documento), DRE, BPA, BPP e DFC (metodo indireto, so o MI), cada um consolidado e
individual (controladora), e a composicao do capital. O parser e puro; o coletor baixa o zip,
filtra pelo universo e grava em lote, append-only. A consolidada e a individual vao ao raw com
a coluna consolidacao; o builder escolhe qual usar.

Ficam no raw as linhas ULTIMO e PENULTIMO. O PENULTIMO e o comparativo do exercicio anterior,
reapresentado pela companhia; o builder usa so o ULTIMO. Na DRE, as subcontas 3.01.NN entram
para o ajuste IFRS 17.

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
from tcc_scraper.config import (
    CONTAS_CVM,
    CONTAS_CVM_AUXILIARES,
    codigos_cvm_universo,
    conta_ajuste_dfc,
    conta_divida_em_outros,
    conta_pl_por_rotulo,
)
from tcc_scraper.db.models_fontes import RawCvmAcoes, RawCvmBalanco
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

# Sufixos dos CSVs dentro do zip (o principal depende do ano e e montado no process).
# _con_ e a consolidada; _ind_ e a individual (controladora).
_SUFIXO_DRE = "_DRE_con_"
_SUFIXO_BPA = "_BPA_con_"
_SUFIXO_BPP = "_BPP_con_"
_SUFIXO_DFC_MI = "_DFC_MI_con_"
_SUFIXO_DRE_IND = "_DRE_ind_"
_SUFIXO_BPA_IND = "_BPA_ind_"
_SUFIXO_BPP_IND = "_BPP_ind_"
_SUFIXO_DFC_MI_IND = "_DFC_MI_ind_"
_SUFIXO_COMPOSICAO = "_composicao_capital_"

# Demonstracao gravada para cada sufixo de demonstracao (consolidada e individual).
_DEMONSTRACAO_POR_SUFIXO = {
    _SUFIXO_DRE: "DRE",
    _SUFIXO_DRE_IND: "DRE",
    _SUFIXO_BPA: "BPA",
    _SUFIXO_BPA_IND: "BPA",
    _SUFIXO_BPP: "BPP",
    _SUFIXO_BPP_IND: "BPP",
    _SUFIXO_DFC_MI: "DFC",
    _SUFIXO_DFC_MI_IND: "DFC",
}

# Chaves do dict que parse_demonstracoes recebe: papel do CSV, independente do nome no zip.
# As chaves sem _IND sao a consolidada; as com _IND, a individual.
CHAVE_PRINCIPAL = "principal"
CHAVE_DRE = "DRE"
CHAVE_BPA = "BPA"
CHAVE_BPP = "BPP"
CHAVE_DFC = "DFC"
CHAVE_DRE_IND = "DRE_ind"
CHAVE_BPA_IND = "BPA_ind"
CHAVE_BPP_IND = "BPP_ind"
CHAVE_DFC_IND = "DFC_ind"
CHAVE_COMPOSICAO = "composicao"

# DRE e DFC sao fluxo de um periodo (inicio e fim). BPA e BPP sao saldo em uma data.
_COM_PERIODO = frozenset({"DRE", "DFC"})

# Chaves primarias das tabelas raw (index_elements do bulk_upsert).
_PK_BALANCO = (
    "cnpj",
    "tipo_documento",
    "dt_refer",
    "versao",
    "consolidacao",
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


def _ordem_sem_acento(ordem: str | None) -> str:
    """ORDEM_EXERC sem acento e em maiuscula ("ÚLTIMO" vira "ULTIMO"). Vazio se nao houver."""
    if not ordem:
        return ""
    sem_acento = unicodedata.normalize("NFKD", ordem).encode("ascii", "ignore").decode("ascii")
    return sem_acento.strip().upper()


def _ultimo(ordem: str | None) -> bool:
    """ORDEM_EXERC "ÚLTIMO" (ou "ULTIMO" sem acento): exercicio mais recente, nao o penultimo."""
    return _ordem_sem_acento(ordem) == "ULTIMO"


def _exercicio_aceito(ordem: str | None) -> bool:
    """ÚLTIMO ou PENÚLTIMO. O PENÚLTIMO e o comparativo do exercicio anterior, reapresentado."""
    return _ordem_sem_acento(ordem) in ("ULTIMO", "PENULTIMO")


def conta_subreceita_dre(cd_conta: str) -> bool:
    """True para 3.01.NN (3 niveis): subcontas da receita.

    O builder procura nelas a despesa de seguros (IFRS 17).
    """
    return cd_conta.startswith("3.01.") and cd_conta.count(".") == 2


def conta_coletada(demonstracao: str, cd_conta: str, ds_conta: str | None) -> bool:
    """True se a conta vai ao raw: as contas de CONTAS_CVM e, no BPP, as achadas pelo rotulo.

    O PL (2.07 em banco, 2.05 em individual) e a divida em "Outros" (Allos) so casam pela
    descricao; o codigo sozinho nao basta.
    """
    if demonstracao == "DRE":
        return cd_conta in CONTAS_CVM["DRE"] or conta_subreceita_dre(cd_conta)
    if demonstracao == "BPA":
        return cd_conta in CONTAS_CVM["BPA"]
    if demonstracao == "BPP":
        return (
            cd_conta in CONTAS_CVM["BPP"]
            or cd_conta in CONTAS_CVM_AUXILIARES["BPP"]
            or conta_pl_por_rotulo(cd_conta, ds_conta)
            or conta_divida_em_outros(cd_conta, ds_conta) is not None
        )
    if demonstracao == "DFC":
        return conta_ajuste_dfc(cd_conta)
    return False


def _balanco(
    r: dict,
    tipo: str,
    demonstracao: str,
    consolidacao: str,
    universo: set[int],
    recebimento: dict[tuple[str, dt.date | None, int | None], dt.date | None],
) -> dict | None:
    """Uma linha de DRE, BPA, BPP ou DFC. BPA e BPP sao saldo em uma data: inicio = fim."""
    cd_cvm = _inteiro(r.get("CD_CVM"))
    cnpj = cnpj_digitos(r.get("CNPJ_CIA"))
    dt_refer = data_cvm(r.get("DT_REFER"))
    versao = _inteiro(r.get("VERSAO"))
    dt_fim = data_cvm(r.get("DT_FIM_EXERC"))
    dt_ini = data_cvm(r.get("DT_INI_EXERC")) if demonstracao in _COM_PERIODO else dt_fim
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
        "consolidacao": consolidacao,
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
    """Converte os CSVs de um zip ITR/DFP em linhas de v1-raw_cvm_balancos e v1-raw_cvm_acoes.

    csvs usa as chaves CHAVE_PRINCIPAL, CHAVE_DRE, CHAVE_BPA, CHAVE_BPP, CHAVE_DFC, as de
    CHAVE_DRE_IND a CHAVE_DFC_IND (individuais) e CHAVE_COMPOSICAO (chave ausente conta como
    lista vazia). So entram companhias de
    cd_cvm_universo. Entram ORDEM_EXERC ULTIMO e PENULTIMO (o PENULTIMO e o comparativo
    reapresentado). Linha repetida na mesma chave primaria: vale a ultima, exceto que um
    PENULTIMO nao sobrescreve um ULTIMO. Na DRE, as subcontas 3.01.NN tambem entram (IFRS 17).
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
    for chave, demonstracao, consolidacao in (
        (CHAVE_DRE, "DRE", "con"),
        (CHAVE_BPA, "BPA", "con"),
        (CHAVE_BPP, "BPP", "con"),
        (CHAVE_DFC, "DFC", "con"),
        (CHAVE_DRE_IND, "DRE", "ind"),
        (CHAVE_BPA_IND, "BPA", "ind"),
        (CHAVE_BPP_IND, "BPP", "ind"),
        (CHAVE_DFC_IND, "DFC", "ind"),
    ):
        for r in csvs.get(chave, []):
            if not _exercicio_aceito(r.get("ORDEM_EXERC")):
                continue
            if not conta_coletada(demonstracao, r.get("CD_CONTA") or "", r.get("DS_CONTA")):
                continue
            linha = _balanco(r, tipo, demonstracao, consolidacao, cd_cvm_universo, recebimento)
            if linha is None:
                continue
            k = tuple(linha[c] for c in _PK_BALANCO)
            anterior = balancos.get(k)
            # PENULTIMO nao sobrescreve ULTIMO na mesma chave primaria.
            if anterior and _ultimo(anterior["ordem_exerc"]) and not _ultimo(linha["ordem_exerc"]):
                continue
            balancos[k] = linha

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
    """ITR e DFP da CVM: um zip por (tipo, ano). Grava v1-raw_cvm_balancos e v1-raw_cvm_acoes."""

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
        universo = codigos_cvm_universo()

        def manter(sufixo: str, r: dict) -> bool:
            # A composicao nao tem CD_CVM (filtrada pelo CNPJ no parser); o resto, pelo universo.
            if sufixo == _SUFIXO_COMPOSICAO:
                return True
            if _inteiro(r.get("CD_CVM")) not in universo:
                return False
            demonstracao = _DEMONSTRACAO_POR_SUFIXO.get(sufixo)
            # Demonstracao: so a conta que o parser usaria (o CSV individual e grande).
            if demonstracao is None:
                return True
            return conta_coletada(demonstracao, r.get("CD_CONTA") or "", r.get("DS_CONTA"))

        brutos = ler_csvs_do_zip(
            self.baixar_zip(url),
            (
                principal,
                _SUFIXO_DRE,
                _SUFIXO_BPA,
                _SUFIXO_BPP,
                _SUFIXO_DFC_MI,
                _SUFIXO_DRE_IND,
                _SUFIXO_BPA_IND,
                _SUFIXO_BPP_IND,
                _SUFIXO_DFC_MI_IND,
                _SUFIXO_COMPOSICAO,
            ),
            manter=manter,
        )
        csvs = {
            CHAVE_PRINCIPAL: brutos[principal],
            CHAVE_DRE: brutos[_SUFIXO_DRE],
            CHAVE_BPA: brutos[_SUFIXO_BPA],
            CHAVE_BPP: brutos[_SUFIXO_BPP],
            CHAVE_DFC: brutos[_SUFIXO_DFC_MI],
            CHAVE_DRE_IND: brutos[_SUFIXO_DRE_IND],
            CHAVE_BPA_IND: brutos[_SUFIXO_BPA_IND],
            CHAVE_BPP_IND: brutos[_SUFIXO_BPP_IND],
            CHAVE_DFC_IND: brutos[_SUFIXO_DFC_MI_IND],
            CHAVE_COMPOSICAO: brutos[_SUFIXO_COMPOSICAO],
        }
        balancos, acoes = parse_demonstracoes(csvs, tipo, cd_cvm_universo=universo)
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
