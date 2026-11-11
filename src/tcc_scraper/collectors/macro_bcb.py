"""Series macroeconomicas oficiais do Banco Central.

SGS e PTAX sao APIs publicas, sem chave e sem limite documentado -- as fontes mais
estaveis do projeto. Preferidas a qualquer agregador de terceiros por serem a
origem do dado.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.config import BCB_SGS_SERIES, get_settings
from tcc_scraper.db.models import Macro
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)

SGS_ENDPOINT = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
PTAX_ENDPOINT = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
)


@dataclass(frozen=True)
class MacroRow:
    series: str
    date: dt.date
    value: float


def parse_sgs(payload: list[dict], series: str) -> list[MacroRow]:
    """Converte a resposta do SGS.

    O SGS devolve data como 'dd/mm/aaaa' e valor como string com ponto decimal.
    Registros ilegiveis sao descartados com log, nao propagados como None.
    """
    rows: list[MacroRow] = []
    for entry in payload:
        try:
            rows.append(
                MacroRow(
                    series=series,
                    date=dt.datetime.strptime(entry["data"], "%d/%m/%Y").date(),
                    value=float(entry["valor"]),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("registro SGS invalido em %s: %s (%s)", series, entry, exc)
    rows.sort(key=lambda r: r.date)
    return rows


def parse_ptax(payload: dict) -> list[MacroRow]:
    """Converte a resposta da PTAX usando cotacaoVenda.

    cotacaoVenda e a especificacao de docs/fontes_dataset.xlsx. dataHoraCotacao traz
    o horario do fechamento, mas so a data importa para o join diario.
    """
    rows: list[MacroRow] = []
    for entry in payload.get("value", []):
        try:
            rows.append(
                MacroRow(
                    series="ptax_venda",
                    date=dt.datetime.strptime(
                        entry["dataHoraCotacao"][:10], "%Y-%m-%d"
                    ).date(),
                    value=float(entry["cotacaoVenda"]),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("registro PTAX invalido: %s (%s)", entry, exc)
    rows.sort(key=lambda r: r.date)
    return rows


class BcbCollector(Collector):
    """Coleta Selic (SGS 432) e dolar PTAX."""

    name = "bcb"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.default_delay = self.settings.throttle_bcb
        for domain in ("api.bcb.gov.br", "olinda.bcb.gov.br"):
            self.throttle.configure(domain, self.settings.throttle_bcb)

    def work_units(self) -> Iterable[WorkUnit]:
        for name, code in BCB_SGS_SERIES.items():
            yield WorkUnit(key=f"sgs|{name}", payload={"kind": "sgs", "series": name, "code": code})
        yield WorkUnit(key="ptax", payload={"kind": "ptax"})

    def process(self, unit: WorkUnit) -> UnitResult:
        settings = get_settings()
        if unit.payload["kind"] == "sgs":
            url = SGS_ENDPOINT.format(code=unit.payload["code"])
            # Desde 1o de janeiro do ano de DATA_START: a Selic vai para a v1-indices
            # (SELIC), e a entrega pede o ano inteiro (a 432 tem um valor por dia corrido).
            inicio = dt.date(settings.data_start.year, 1, 1)
            params = {
                "formato": "json",
                "dataInicial": f"{inicio:%d/%m/%Y}",
                "dataFinal": f"{settings.fim_coleta:%d/%m/%Y}",
            }
            rows = parse_sgs(self.fetch(url, params=params).json(), series=unit.payload["series"])
        else:
            # A PTAX exige as datas entre aspas simples no formato MM-DD-AAAA.
            params = {
                "@dataInicial": f"'{settings.data_start:%m-%d-%Y}'",
                "@dataFinalCotacao": f"'{settings.fim_coleta:%m-%d-%Y}'",
                "$format": "json",
            }
            rows = parse_ptax(self.fetch(PTAX_ENDPOINT, params=params).json())

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
