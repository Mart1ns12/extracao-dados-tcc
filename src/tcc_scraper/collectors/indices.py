"""Indices de referencia do Banco Central: CDI diario (SGS 12) e IPCA mensal (SGS 433).

Mesma API do SGS usada em macro_bcb (sem chave, sem limite documentado). A diferenca
e o fatiamento: a serie diaria do SGS aceita no maximo 10 anos por requisicao, entao
cada unidade de trabalho e um par (serie, ano) -- retomavel e pequeno.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import replace

from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.collectors.macro_bcb import SGS_ENDPOINT, MacroRow, parse_sgs
from tcc_scraper.config import INDICES_BCB
from tcc_scraper.db.models import Macro
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)


class IndicesBcbCollector(Collector):
    """Coleta CDI (SGS 12) e IPCA (SGS 433) em fatias anuais."""

    name = "indices_bcb"

    def __init__(self, anos: list[int] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        if anos is None:
            anos = list(range(self.settings.price_start.year, self.settings.fim_coleta.year + 1))
        self.anos = list(anos)
        # Mesmo mecanismo do BcbCollector: o dominio do BCB ganha o intervalo proprio.
        self.throttle.configure("api.bcb.gov.br", self.settings.throttle_bcb)

    def work_units(self) -> Iterable[WorkUnit]:
        for serie, code in INDICES_BCB.items():
            for ano in self.anos:
                yield WorkUnit(
                    key=f"sgs|{serie}|{ano}",
                    payload={"serie": serie, "code": code, "ano": ano},
                )

    def process(self, unit: WorkUnit) -> UnitResult:
        serie = unit.payload["serie"]
        ano = unit.payload["ano"]
        # Janela do ano limitada ao periodo do dataset (com aquecimento) e ao fim da coleta.
        inicio = max(dt.date(ano, 1, 1), self.settings.price_start)
        fim = min(dt.date(ano, 12, 31), self.settings.fim_coleta)
        if inicio > fim:
            return UnitResult(items_found=0)

        url = SGS_ENDPOINT.format(code=unit.payload["code"])
        params = {
            "formato": "json",
            "dataInicial": f"{inicio:%d/%m/%Y}",
            "dataFinal": f"{fim:%d/%m/%Y}",
        }
        rows = parse_sgs(self.fetch(url, params=params).json(), series=serie)
        if serie == "ipca":
            # IPCA e mensal: a chave (series, date) fica estavel so se o dia for sempre 1.
            rows = [replace(r, date=r.date.replace(day=1)) for r in rows]

        self._persist(rows)
        return UnitResult(items_found=len(rows))

    def _persist(self, rows: list[MacroRow]) -> None:
        if not rows:
            return
        payload = [{"series": r.series, "date": r.date, "value": r.value} for r in rows]
        with session_scope() as session:
            bulk_upsert(
                session,
                Macro.__table__,
                payload,
                index_elements=["series", "date"],
                update_columns=["value"],
            )
