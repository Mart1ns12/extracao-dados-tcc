"""Coletor dos índices IDIV, SMLL e IFIX pela B3 (indexStatisticsProxy), um ano por chamada.

O Yahoo devolve só 1 dia de histórico para esses índices (`range=5y` trouxe 1 ponto, medido),
por isso eles saem do Yahoo e vêm da B3. Endpoint GetPortfolioDay, com o parâmetro `year`:
a resposta traz 31 linhas (dia do mês) e, em cada uma, `rateValue1`..`rateValue12`, que é o
fechamento do dia `day` no mês N. O valor vem em texto pt-BR ("6.831,31") e é null quando não
houve pregão ou o dia não existe no mês.

Gravação em raw_prices com ticker = código do índice (IDIV, SMLL, IFIX), a mesma chave que
INDICES_YAHOO usa na entrega. source='b3'. Só o fechamento vem nesse endpoint: open, high,
low e volume ficam nulos. adj_close recebe o próprio close, como já acontece com os índices
do Yahoo. Upsert por (ticker, date): recoletar corrige o dado sem duplicar.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

from tcc_scraper.collectors.b3_base import B3Collector, decimal_b3
from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.config import INDICES_YAHOO
from tcc_scraper.db.models import Price
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)

INDEX_BASE = "https://sistemaswebb3-listados.b3.com.br/indexStatisticsProxy/IndexCall"
METODO_DIA = "GetPortfolioDay"
MESES = range(1, 13)


class B3IndicesCollector(B3Collector):
    """IDIV, SMLL e IFIX por ano civil, do price_start (com aquecimento) até o fim da coleta."""

    name = "b3_indices"

    def __init__(self, anos: list[int] | None = None, **kw) -> None:
        super().__init__(**kw)
        if anos is None:
            anos = list(range(self.settings.price_start.year, self.settings.fim_coleta.year + 1))
        self.anos = list(anos)

    # -- contrato do Collector ----------------------------------------------------
    def work_units(self) -> Iterable[WorkUnit]:
        # Uma unidade por (índice, ano): o endpoint entrega o ano inteiro numa chamada.
        for indice in INDICES_YAHOO:
            for ano in self.anos:
                yield WorkUnit(key=f"b3|{indice}|{ano}", payload={"indice": indice, "ano": ano})

    def process(self, unit: WorkUnit) -> UnitResult:
        indice = unit.payload["indice"]
        ano = unit.payload["ano"]
        # Janela do ano limitada ao período do dataset (com aquecimento) e ao fim da coleta.
        inicio = max(dt.date(ano, 1, 1), self.settings.price_start)
        fim = min(dt.date(ano, 12, 31), self.settings.fim_coleta)
        if inicio > fim:
            return UnitResult(items_found=0)

        corpo = self.get_json(
            METODO_DIA, {"index": indice, "language": "pt-br", "year": str(ano)}, base=INDEX_BASE
        )
        if not isinstance(corpo, dict):
            # Sem JSON não há o que gravar: levantar marca a unidade como erro no checkpoint.
            raise RuntimeError(f"B3 não devolveu {indice} de {ano}")

        linhas = parse_portfolio_day(corpo, ticker=indice, ano=ano, inicio=inicio, fim=fim)
        self._persist(linhas)
        return UnitResult(items_found=len(linhas))

    # -- escrita ------------------------------------------------------------------
    def _persist(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        payload = montar_raw_prices(linhas)
        with session_scope() as session:
            # Inclui as colunas que a B3 não traz: se houver linha do Yahoo na mesma data
            # (o 1 dia antigo), open/high/low/volume dela são zerados em vez de sobrarem.
            bulk_upsert(
                session,
                Price.__table__,
                payload,
                index_elements=["ticker", "date"],
                update_columns=["open", "high", "low", "close", "adj_close", "volume", "source"],
            )


def parse_portfolio_day(
    payload: dict[str, Any],
    *,
    ticker: str,
    ano: int,
    inicio: dt.date,
    fim: dt.date,
) -> list[dict]:
    """Uma linha {ticker, date, close} por fechamento válido do ano, entre inicio e fim.

    Ignora: valor null ou vazio (sem pregão), combinação dia/mês que não existe (31/02, 29/02
    em ano não bissexto) e datas fora do intervalo (ex.: depois de fim, que é a data de hoje
    na coleta diária). Os blocos min/max do payload não entram.
    """
    linhas: list[dict] = []
    for registro in payload.get("results") or []:
        dia = registro.get("day")
        for mes in MESES:
            fechamento = decimal_b3(registro.get(f"rateValue{mes}"))
            if fechamento is None:
                continue
            try:
                data = dt.date(ano, mes, dia)
            except (TypeError, ValueError):
                # O servidor não deveria mandar esses valores; o parser não confia só nele.
                continue
            if not inicio <= data <= fim:
                continue
            linhas.append({"ticker": ticker, "date": data, "close": float(fechamento)})
    linhas.sort(key=lambda r: r["date"])
    return linhas


def montar_raw_prices(linhas: list[dict]) -> list[dict]:
    """Linhas de parse_portfolio_day no formato de raw_prices (open/high/low/volume = None)."""
    return [
        {
            "ticker": r["ticker"],
            "date": r["date"],
            "open": None,
            "high": None,
            "low": None,
            "close": r["close"],
            "adj_close": r["close"],
            "volume": None,
            "source": "b3",
        }
        for r in linhas
    ]
