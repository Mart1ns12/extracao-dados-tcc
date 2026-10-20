"""Cadastro das companhias na B3 (listedCompaniesProxy/GetDetail), chaveado pelo código CVM.

Uma chamada por código CVM do universo. O JSON inteiro vai para a coluna payload: hoje usamos
só um subconjunto, mas a resposta original permite reprocessar sem rede.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable

from tcc_scraper.collectors.b3_base import B3Collector
from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.collectors.cvm_base import cnpj_digitos
from tcc_scraper.config import TICKER_CODE_CVM
from tcc_scraper.db.models_fontes import RawB3Cadastro
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)


def classificacao_b3(texto: str | None) -> tuple[str | None, str | None, str | None]:
    """Separa "Setor / Subsetor / Segmento" da B3 em três partes.

    A B3 usa ponto onde o português pede vírgula ("Petróleo. Gás"): ". " vira ", ".
    """
    if not texto or not texto.strip():
        return (None, None, None)
    partes: list[str | None] = [p.strip().replace(". ", ", ") for p in texto.split("/")]
    partes += [None] * max(0, 3 - len(partes))
    return (partes[0], partes[1], partes[2])


def parse_get_detail(payload: dict, cd_cvm: int) -> dict:
    """Linha de raw_b3_cadastro a partir do JSON do GetDetail.

    other_codes junta os tickers de otherCodes com o código principal, sem repetição.
    """
    codigos = [c.get("code") for c in payload.get("otherCodes") or []] + [payload.get("code")]
    return {
        "cd_cvm": cd_cvm,
        "cnpj": cnpj_digitos(payload.get("cnpj") or "") or None,
        "trading_name": payload.get("tradingName"),
        "issuing_company": payload.get("issuingCompany"),
        "industry_classification": payload.get("industryClassification"),
        "other_codes": list(dict.fromkeys(c for c in codigos if c)),
        "payload": payload,
        "fetched_at": dt.datetime.now(dt.UTC),
    }


class B3CadastroCollector(B3Collector):
    name = "b3_cadastro"

    def __init__(self, codigos: list[int] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.codigos = codigos if codigos is not None else sorted(set(TICKER_CODE_CVM.values()))

    def work_units(self) -> Iterable[WorkUnit]:
        for cd_cvm in self.codigos:
            yield WorkUnit(key=f"detail|{cd_cvm}", payload={"cd_cvm": cd_cvm})

    def process(self, unit: WorkUnit) -> UnitResult:
        cd_cvm = unit.payload["cd_cvm"]
        payload = self.get_json("GetDetail", {"codeCVM": str(cd_cvm), "language": "pt-br"})
        if not isinstance(payload, dict):
            logger.warning("b3_cadastro: sem detalhe para o codigo CVM %s", cd_cvm)
            return UnitResult(items_found=0)
        reg = parse_get_detail(payload, cd_cvm)
        with session_scope() as session:
            bulk_upsert(
                session,
                RawB3Cadastro.__table__,
                [reg],
                index_elements=["cd_cvm"],
                update_columns=[
                    "cnpj", "trading_name", "issuing_company", "industry_classification",
                    "other_codes", "payload", "fetched_at",
                ],
            )
        return UnitResult(items_found=1)
