"""Cadastro das companhias abertas da CVM (cad_cia_aberta.csv): CNPJ, código CVM, situação e setor.

Um único CSV com o cadastro inteiro, em latin-1 e separado por ";". A unidade de trabalho é o
próprio arquivo, com chave pelo Last-Modified: se a CVM não atualizou, a unidade já concluída
é pulada. A tabela é chaveada pelo CNPJ e sobrescrita, porque o cadastro corrige registros.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from collections.abc import Iterable

from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.collectors.cvm_base import (
    CVM_BASE,
    TIMEOUT_ZIP,
    CvmCollector,
    cnpj_digitos,
    last_modified,
)
from tcc_scraper.config import TICKER_CODE_CVM
from tcc_scraper.db.models_fontes import RawCvmCadastro
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)

URL_CAD_CIA_ABERTA = f"{CVM_BASE}/CAD/DADOS/cad_cia_aberta.csv"


def _cd_cvm(s: str | None) -> int | None:
    """Código CVM como inteiro; a CVM traz às vezes zeros à esquerda. Inválido vira None."""
    try:
        return int((s or "").strip())
    except ValueError:
        return None


def parse_cad_cia_aberta(linhas: Iterable[dict], cd_cvm_universo: set[int]) -> list[dict]:
    """Filtra o cadastro pelo universo de códigos CVM e devolve uma linha por CNPJ.

    O CSV repete companhias em mais de uma linha; a última vence, para o upsert não receber
    a mesma chave duas vezes no mesmo lote.
    """
    agora = dt.datetime.now(dt.UTC)
    por_cnpj: dict[str, dict] = {}
    for linha in linhas:
        cd_cvm = _cd_cvm(linha.get("CD_CVM"))
        if cd_cvm is None or cd_cvm not in cd_cvm_universo:
            continue
        cnpj = cnpj_digitos(linha.get("CNPJ_CIA", ""))
        if not cnpj:
            continue
        por_cnpj[cnpj] = {
            "cnpj": cnpj,
            "cd_cvm": cd_cvm,
            "denom_social": (linha.get("DENOM_SOCIAL") or "").strip(),
            "sit": (linha.get("SIT") or "").strip() or None,
            "setor_ativ": (linha.get("SETOR_ATIV") or "").strip() or None,
            "fetched_at": agora,
        }
    return list(por_cnpj.values())


class CvmCadastroCollector(CvmCollector):
    name = "cvm_cadastro"

    def work_units(self) -> Iterable[WorkUnit]:
        hoje = dt.date.today().isoformat()
        lm = last_modified(self, URL_CAD_CIA_ABERTA)
        yield WorkUnit(key=f"cad|{lm or hoje}", payload={"url": URL_CAD_CIA_ABERTA})

    def process(self, unit: WorkUnit) -> UnitResult:
        texto = self.fetch(unit.payload["url"], timeout=TIMEOUT_ZIP).content.decode("latin-1")
        linhas = csv.DictReader(io.StringIO(texto), delimiter=";")
        regs = parse_cad_cia_aberta(linhas, set(TICKER_CODE_CVM.values()))
        with session_scope() as session:
            bulk_upsert(
                session,
                RawCvmCadastro.__table__,
                regs,
                index_elements=["cnpj"],
                update_columns=["cd_cvm", "denom_social", "sit", "setor_ativ", "fetched_at"],
            )
        return UnitResult(items_found=len(regs))
