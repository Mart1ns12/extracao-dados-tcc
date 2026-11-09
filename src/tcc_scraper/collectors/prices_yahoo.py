"""Coleta de precos diarios via Yahoo Finance chart API.

Fonte primaria de preco. Escolhida sobre a Brapi por medicao (2026-09-07): entrega
5 anos com adjclose sem chave, contra 1 ano da Brapi no plano gratuito. Cobre no
mesmo endpoint acoes .SA, o indice ^BVSP, Brent (BZ=F) e USD/BRL (USDBRL=X).
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.config import (
    BENCHMARK_TICKER,
    FATORES_COTACAO_YAHOO,
    IVVB11,
    MACRO_YAHOO,
    FatorCotacaoYahoo,
    get_settings,
    universo_coleta,
    yahoo_symbol,
)
from tcc_scraper.db.models import Price
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)

CHART_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart"


@dataclass(frozen=True)
class PriceRow:
    ticker: str
    date: dt.date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None
    source: str = "yahoo"


@dataclass(frozen=True)
class LacunaPreco:
    """Pregao que o Yahoo nao tem, com o preco por acao da B3 (sem ajuste) e os vizinhos."""

    ticker: str
    date: dt.date
    codneg: str
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None
    fonte: str  # "b3_cotahist" (papel) ou "b3" (^BVSP, IBOV da B3)
    ref_anterior: dt.date | None = None
    ant: tuple[float | None, ...] = (None, None, None, None)  # O, H, L, C da B3
    ref_seguinte: dt.date | None = None
    seg: tuple[float | None, ...] = (None, None, None, None)


def parse_chart(payload: dict, ticker: str) -> list[PriceRow]:
    """Converte a resposta do chart API em linhas diarias.

    O Yahoo devolve arrays paralelos (timestamp, open, high, ...) com null nos dias
    sem negocio. Linhas sem close sao descartadas: nao representam pregao e
    contaminariam o calculo de retornos.
    """
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        logger.warning("resposta do Yahoo sem dados para %s", ticker)
        return []

    adj = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose")
    fuso = _fuso_pregao(result.get("meta", {}))

    rows: list[PriceRow] = []
    for i, ts in enumerate(timestamps):
        close = _at(quote.get("close"), i)
        if close is None:
            continue
        volume = _at(quote.get("volume"), i)
        rows.append(
            PriceRow(
                ticker=ticker,
                # O timestamp marca a abertura do pregao; a data sai no fuso da bolsa.
                # Em UTC, o cambio (Europe/London, barra as 00:00 locais) caia na vespera
                # durante o horario de verao britanico: domingo no lugar de sexta.
                date=dt.datetime.fromtimestamp(ts, tz=fuso).date(),
                open=_at(quote.get("open"), i),
                high=_at(quote.get("high"), i),
                low=_at(quote.get("low"), i),
                close=close,
                # Sem adjclose (indices como ^BVSP e IDIV nao trazem), o proprio close:
                # preferivel a perder a linha.
                adj_close=_adj_ou_close(adj, i, close),
                volume=int(volume) if volume is not None else None,
            )
        )
    rows.sort(key=lambda r: r.date)
    return rows


def parse_dividendos(payload: dict, fuso: dt.tzinfo | None = None) -> list[tuple[dt.date, float]]:
    """Dividendos do chart API (events=div): (data ex, valor por acao), em ordem de data.

    A data ex sai no fuso da bolsa, como em parse_chart (`fuso` padrao: o do `meta`). O valor
    e o `amount` do evento, por acao do papel consultado. Sem eventos, devolve lista vazia.
    """
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        logger.warning("resposta do Yahoo sem dados de dividendos")
        return []
    eventos = (result.get("events") or {}).get("dividends") or {}
    if fuso is None:
        fuso = _fuso_pregao(result.get("meta", {}))
    dividendos: list[tuple[dt.date, float]] = []
    for chave, evento in eventos.items():
        ts = evento.get("date", int(chave))
        valor = evento.get("amount")
        if valor is None:
            continue
        dividendos.append((dt.datetime.fromtimestamp(ts, tz=fuso).date(), float(valor)))
    return sorted(dividendos)


def _fuso_pregao(meta: dict) -> dt.tzinfo:
    """Fuso da bolsa (`exchangeTimezoneName`); UTC se ausente ou desconhecido.

    Nao usa `gmtoffset`: e o deslocamento de hoje, nao o de cada data do historico.
    """
    nome = meta.get("exchangeTimezoneName")
    if not nome:
        return dt.UTC
    try:
        return ZoneInfo(nome)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("fuso desconhecido do Yahoo: %s; usando UTC", nome)
        return dt.UTC


def _at(seq: list | None, index: int) -> float | None:
    if not seq or index >= len(seq):
        return None
    return seq[index]


def _adj_ou_close(adj: list | None, index: int, close: float) -> float:
    """adjclose do dia; cai no close quando a serie inteira ou o dia vem nulo."""
    ajustado = _at(adj, index)
    return ajustado if ajustado is not None else close


def _emendar_base_de_ajuste(
    correcao: list[PriceRow], rows: list[PriceRow], fim: dt.date
) -> list[PriceRow]:
    """Reescala o adj_close da correcao pela base do Yahoo logo depois da janela.

    O Yahoo ajusta cada dia pelos proventos posteriores a ele. A correcao so tem os proventos
    ate `fim`; o fator (adj / close) do primeiro pregao seguinte leva os de depois. Sem
    pregao posterior com close no Yahoo, a correcao fica como esta.
    """
    posteriores = sorted((r for r in rows if r.date > fim and r.close), key=lambda r: r.date)
    if not posteriores or posteriores[0].adj_close is None:
        return correcao
    referencia = posteriores[0]
    fator = referencia.adj_close / referencia.close
    return [
        replace(r, adj_close=r.adj_close * fator) if r.adj_close is not None else r
        for r in correcao
    ]


# Salto relativo maximo entre o ultimo close antes da janela e o primeiro dentro dela. Acima
# dele o Yahoo ainda cota por lote (27,10 / 0,30 = 90x); abaixo, ja cota por acao.
SALTO_MAXIMO_FATOR = 0.5


def aplicar_fator_cotacao(
    rows: list[PriceRow],
    fatores: dict[str, FatorCotacaoYahoo],
    ticker: str | None = None,
) -> list[PriceRow]:
    """Divide por lote o preco do Yahoo na janela em que a B3 cota o papel por lote.

    open, high, low, close e adj_close da janela saem divididos pelo fator. O volume nao muda:
    o Yahoo ja o entrega em acoes. Se o close do primeiro pregao da janela esta perto do ultimo
    antes dela, o Yahoo ja cota por acao: nada muda, com aviso. `ticker` segue a regra de
    aplicar_correcoes.
    """
    alvo = ticker or (rows[0].ticker if rows else None)
    regra = fatores.get(alvo) if alvo else None
    if regra is None:
        return rows
    inicio = regra.inicio
    fim = regra.fim

    def na_janela(r: PriceRow) -> bool:
        return r.date >= inicio and (fim is None or r.date <= fim)

    antes = max(
        (r for r in rows if r.date < inicio and r.close),
        key=lambda r: r.date,
        default=None,
    )
    dentro = min(
        (r for r in rows if na_janela(r) and r.close),
        key=lambda r: r.date,
        default=None,
    )
    if antes and dentro:
        salto = dentro.close / antes.close
        if abs(salto - 1) <= SALTO_MAXIMO_FATOR:
            logger.warning(
                "%s: preco de %s ja por acao no Yahoo (salto %.2fx); fator %d nao aplicado",
                alvo,
                dentro.date,
                salto,
                regra.fator,
            )
            return rows

    def dividir(valor: float | None) -> float | None:
        return valor / regra.fator if valor is not None else None

    return [
        replace(
            r,
            open=dividir(r.open),
            high=dividir(r.high),
            low=dividir(r.low),
            close=dividir(r.close),
            adj_close=dividir(r.adj_close),
        )
        if na_janela(r)
        else r
        for r in rows
    ]


def aplicar_correcoes(
    rows: list[PriceRow],
    correcoes: dict[str, list[PriceRow]],
    ticker: str | None = None,
) -> list[PriceRow]:
    """Troca o trecho do Yahoo pela serie da B3 quando o ticker tem correcao.

    Descarta as linhas do Yahoo entre a menor e a maior data da correcao e mete as da
    correcao no lugar. Fora dessa janela o Yahoo fica. `ticker` so importa quando `rows`
    esta vazio: sem linhas do Yahoo, o ticker nao sai delas. Se o Yahoo tem pregoes depois
    da janela, o adj_close da correcao e reescalado pela base dele (_emendar_base_de_ajuste).
    """
    alvo = ticker or (rows[0].ticker if rows else None)
    correcao = correcoes.get(alvo) if alvo else None
    if not correcao:
        return rows
    inicio = min(r.date for r in correcao)
    fim = max(r.date for r in correcao)
    mantidas = [r for r in rows if not inicio <= r.date <= fim]
    correcao = _emendar_base_de_ajuste(correcao, rows, fim)
    return sorted([*mantidas, *correcao], key=lambda r: r.date)


# Quanto a razao Yahoo/B3 pode variar entre o pregao antes e o depois da lacuna. Acima disso
# a escala nao e estavel e a lacuna fica de fora.
TOLERANCIA_RAZAO_LACUNA = 0.003


def razao_mediana(yahoo: Iterable[float | None], b3: Iterable[float | None]) -> float | None:
    """Mediana de y/b nos pares em que os dois existem e b > 0. Sem pares, None."""
    razoes = [
        y / b for y, b in zip(yahoo, b3, strict=True) if y is not None and b is not None and b > 0
    ]
    return statistics.median(razoes) if razoes else None


def _ohlc(r: PriceRow) -> tuple[float | None, ...]:
    return (r.open, r.high, r.low, r.close)


def _escala_da_lacuna(
    lacuna: LacunaPreco, por_data: dict[dt.date, PriceRow], alvo: str | None
) -> tuple[float, float] | None:
    """(razao Yahoo/B3, fator de ajuste) medidos nos vizinhos; None se nao der para medir."""
    y_ant = por_data.get(lacuna.ref_anterior)
    y_seg = por_data.get(lacuna.ref_seguinte)
    if y_ant is None or y_seg is None:
        logger.warning("%s %s: falta pregao vizinho no Yahoo; lacuna pulada", alvo, lacuna.date)
        return None
    r_ant = razao_mediana(_ohlc(y_ant), lacuna.ant)
    r_seg = razao_mediana(_ohlc(y_seg), lacuna.seg)
    if not r_ant or not r_seg or abs(r_ant / r_seg - 1) > TOLERANCIA_RAZAO_LACUNA:
        logger.warning(
            "%s %s: razao Yahoo/B3 instavel (%s, %s); lacuna pulada",
            alvo, lacuna.date, r_ant, r_seg,
        )
        return None
    fator_adj = 1.0
    if y_seg.adj_close is not None and y_seg.close:
        fator_adj = y_seg.adj_close / y_seg.close
    return r_seg, fator_adj


def _pregao_da_lacuna(lacuna: LacunaPreco, razao: float, fator_adj: float) -> PriceRow:
    """Pregao da B3 na escala do Yahoo: preco x razao, volume / razao, adj pelo fator."""

    def escala(valor: float | None) -> float | None:
        return valor * razao if valor is not None else None

    close = lacuna.close * razao
    return PriceRow(
        ticker=lacuna.ticker,
        date=lacuna.date,
        open=escala(lacuna.open),
        high=escala(lacuna.high),
        low=escala(lacuna.low),
        close=close,
        adj_close=close * fator_adj,
        volume=round(lacuna.volume / razao) if lacuna.volume is not None else None,
        source=lacuna.fonte,
    )


def completar_lacunas(
    rows: list[PriceRow],
    lacunas: dict[str, list[LacunaPreco]],
    ticker: str | None = None,
) -> list[PriceRow]:
    """Acrescenta os pregoes que o Yahoo nao tem, com o preco da B3 na escala do Yahoo.

    Data que ja esta em `rows` fica como esta. Indice (sem pregao de referencia) entra com o
    preco da B3 como veio. Com vizinhos, a escala e a razao Yahoo/B3 medida antes e depois da
    lacuna: se as duas nao batem (TOLERANCIA_RAZAO_LACUNA) ou falta vizinho, a lacuna e pulada
    com aviso. Lacuna fora do periodo coletado (coleta incremental do daily) e ignorada sem
    aviso. `ticker` segue a regra de aplicar_correcoes.
    """
    alvo = ticker or (rows[0].ticker if rows else None)
    lista = lacunas.get(alvo) if alvo else None
    if not lista:
        return rows
    por_data = {r.date: r for r in rows}
    periodo = (min(por_data), max(por_data)) if por_data else None
    completadas: list[PriceRow] = []
    for lacuna in lista:
        if lacuna.date in por_data:
            continue
        if periodo and not periodo[0] <= lacuna.date <= periodo[1]:
            continue
        if lacuna.ref_seguinte is None:
            completadas.append(_pregao_da_lacuna(lacuna, 1.0, 1.0))
            continue
        escala = _escala_da_lacuna(lacuna, por_data, alvo)
        if escala is not None:
            completadas.append(_pregao_da_lacuna(lacuna, *escala))
    if not completadas:
        return rows
    return sorted([*rows, *completadas], key=lambda r: r.date)


def tickers_padrao() -> list[str]:
    """Simbolos coletados por padrao: universo, Ibovespa, macro e IVVB11.

    IDIV, SMLL e IFIX nao estao aqui: o Yahoo so devolve 1 dia deles. Vem da B3
    (collectors/b3_indices.py).
    """
    return [*universo_coleta(), BENCHMARK_TICKER, *MACRO_YAHOO.values(), IVVB11]


class YahooPriceCollector(Collector):
    """Coleta OHLCV de acoes, benchmark e series macro do Yahoo."""

    name = "yahoo"

    def __init__(self, tickers: Iterable[str] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tickers = list(tickers) if tickers is not None else tickers_padrao()
        self.default_delay = self.settings.throttle_yahoo
        self.throttle.configure("query1.finance.yahoo.com", self.settings.throttle_yahoo)

    def work_units(self) -> Iterable[WorkUnit]:
        # Uma unidade por ticker: o Yahoo entrega o periodo inteiro numa requisicao,
        # entao nao ha ganho em fatiar por data.
        for ticker in self.tickers:
            yield WorkUnit(key=ticker, payload={"ticker": ticker})

    def process(self, unit: WorkUnit) -> UnitResult:
        ticker = unit.payload["ticker"]
        settings = get_settings()
        symbol = yahoo_symbol(ticker)

        params = {
            # price_start, nao data_start: precisa do historico anterior a janela do
            # dataset para as medias de 20 pregoes ja saírem preenchidas na 1a linha.
            "period1": int(
                dt.datetime.combine(settings.price_start, dt.time.min, tzinfo=dt.UTC).timestamp()
            ),
            # period2 e exclusivo: o dia fim_coleta entra inteiro.
            "period2": int(
                dt.datetime.combine(
                    settings.fim_coleta + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC
                ).timestamp()
            ),
            "interval": "1d",
            "events": "div,split",
        }
        url = f"{CHART_ENDPOINT}/{urllib.parse.quote(symbol)}?{urllib.parse.urlencode(params)}"
        rows = parse_chart(self.fetch(url).json(), ticker=ticker)
        rows = aplicar_fator_cotacao(rows, FATORES_COTACAO_YAHOO, ticker=ticker)
        # Import local: b3_cotahist importa PriceRow daqui, e o import no topo daria ciclo.
        from tcc_scraper.collectors.b3_cotahist import carregar_correcoes, carregar_lacunas

        rows = aplicar_correcoes(rows, carregar_correcoes(), ticker=ticker)
        rows = completar_lacunas(rows, carregar_lacunas(), ticker=ticker)

        self._persist(rows)
        return UnitResult(items_found=len(rows))

    def baixar_dividendos(
        self, simbolo: str, inicio: dt.date, fim: dt.date
    ) -> list[tuple[dt.date, float]]:
        """Dividendos (data ex, valor por acao) do Yahoo entre `inicio` e `fim`, inclusive.

        Mesmo chart API de process, so com events=div. Nao grava nada: quem chama decide.
        """
        params = {
            "period1": int(
                dt.datetime.combine(inicio, dt.time.min, tzinfo=dt.UTC).timestamp()
            ),
            # period2 e exclusivo, como em process.
            "period2": int(
                dt.datetime.combine(
                    fim + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC
                ).timestamp()
            ),
            "interval": "1d",
            "events": "div,split",
        }
        url = f"{CHART_ENDPOINT}/{urllib.parse.quote(simbolo)}?{urllib.parse.urlencode(params)}"
        return parse_dividendos(self.fetch(url).json())

    def _persist(self, rows: list[PriceRow]) -> None:
        """Upsert por (ticker, date) em lote: recoletar corrige o dado sem duplicar."""
        if not rows:
            return
        payload = [
            {
                "ticker": r.ticker, "date": r.date, "open": r.open, "high": r.high,
                "low": r.low, "close": r.close, "adj_close": r.adj_close,
                "volume": r.volume, "source": r.source,
            }
            for r in rows
        ]
        with session_scope() as session:
            bulk_upsert(
                session,
                Price.__table__,
                payload,
                index_elements=["ticker", "date"],
                update_columns=[
                    "open", "high", "low", "close", "adj_close", "volume", "source"
                ],
            )
