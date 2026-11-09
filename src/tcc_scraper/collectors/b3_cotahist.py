"""Precos da B3 (COTAHIST) para os papeis em que o Yahoo erra. Lista: CORRECOES_PRECO_B3.

O arquivo anual COTAHIST_A{ano}.ZIP traz, por papel e pregao, o preco na cotacao do papel.
Quando a cotacao e por lote (FATCOT > 1), o preco por acao e preco / FATCOT. Os grupamentos
do suplemento da B3 (fator < 1, convencao de entrega/builder.py) levam a serie para a base
atual do papel. A troca de codigo (fator_troca: JBSS3 -> JBSS32) entra no mesmo fator, e
os proventos em dinheiro (fatores_dividendos) ajustam o adj_close.

O resultado vai para data/correcoes_precos/<TICKER>.csv, versionado. O coletor do Yahoo le
esses CSVs e troca o trecho errado (aplicar_correcoes, em prices_yahoo.py).

Quando a correcao tem proventos_yahoo, os dividendos do Yahoo entram nos proventos do fator
(data-com = pregao anterior a data ex) e vao para data/proventos_yahoo/<TICKER>.csv, que o
builder da entrega le. A B3 vence onde ja tem a mesma data-com.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import unicodedata
import zipfile
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from tcc_scraper.collectors.b3_base import USER_AGENT_B3, data_b3, decimal_b3
from tcc_scraper.collectors.b3_indices import (
    INDEX_BASE,
    METODO_DIA,
    B3IndicesCollector,
    parse_portfolio_day,
)
from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.collectors.prices_yahoo import (
    TOLERANCIA_RAZAO_LACUNA,
    LacunaPreco,
    PriceRow,
    YahooPriceCollector,
    razao_mediana,
    tickers_padrao,
)
from tcc_scraper.config import (
    BENCHMARK_TICKER,
    CODIGOS_ANTERIORES_COTAHIST,
    CORRECOES_PRECO_B3,
    MACRO_YAHOO,
    MUDANCAS_TICKER,
    PROJECT_ROOT,
    CorrecaoPrecoB3,
    get_settings,
)
from tcc_scraper.db.models import Price
from tcc_scraper.db.models_fontes import RawB3Proventos, RawB3Suplemento
from tcc_scraper.db.session import session_scope

logger = logging.getLogger(__name__)

COTAHIST_URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP"
BVMF_DOMINIO = "bvmf.bmfbovespa.com.br"
FONTE = "b3_cotahist"
PASTA_CORRECOES = PROJECT_ROOT / "data" / "correcoes_precos"
PASTA_PROVENTOS_YAHOO = PROJECT_ROOT / "data" / "proventos_yahoo"
PASTA_LACUNAS = PROJECT_ROOT / "data" / "lacunas_precos"
COLUNAS_CSV = [
    "ticker", "date", "open", "high", "low", "close", "adj_close", "volume",
    "codneg", "fatcot", "fator_grupamento", "fator_troca", "fator_dividendos",
]
COLUNAS_PROVENTOS_YAHOO = [
    "ticker", "issuing_company", "ticker_negociado", "data_ex", "data_com",
    "valor_por_acao", "fonte",
]
COLUNAS_LACUNAS = [
    "ticker", "date", "codneg", "open", "high", "low", "close", "volume", "fonte",
    "ref_anterior", "ant_open", "ant_high", "ant_low", "ant_close",
    "ref_seguinte", "seg_open", "seg_high", "seg_low", "seg_close",
]
# Rotulos da B3 (corporateAction, sem acento, em maiusculas) que pagam dinheiro ao acionista.
TIPOS_PROVENTO_DINHEIRO = frozenset(
    {"DIVIDENDO", "JRS CAP PROPRIO", "RENDIMENTO", "RESTITUICAO DE CAPITAL"}
)
# Abaixo disso a conta mudaria a serie sem aviso: provento maior que metade do preco.
FATOR_DIVIDENDO_MINIMO = Decimal("0.5")


class CotahistCollector(Collector):
    """Baixa o COTAHIST anual do site da B3, com throttle e retry da base.

    Nao e um coletor em lote: gerar_correcoes chama baixar_ano direto. Nada e gravado no
    banco, por isso o ciclo work_units/process (com checkpoint) nao e usado.
    """

    name = "b3_cotahist"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.throttle.configure(BVMF_DOMINIO, self.settings.throttle_b3)

    def baixar_ano(self, ano: int) -> bytes:
        """Conteudo bruto do ZIP anual (~95 MB). HTTP de erro vira RuntimeError."""
        url = COTAHIST_URL.format(ano=ano)
        resposta = self.fetch(url, headers={"User-Agent": USER_AGENT_B3})
        if resposta.status_code >= 400:
            raise RuntimeError(f"b3_cotahist: {url} devolveu HTTP {resposta.status_code}")
        logger.info("b3_cotahist: %d bytes de COTAHIST_A%d", len(resposta.content), ano)
        return resposta.content

    # -- contrato do Collector (nao usado aqui) ------------------------------------
    def work_units(self) -> Iterable[WorkUnit]:
        return iter(())

    def process(self, unit: WorkUnit) -> UnitResult:
        raise NotImplementedError("use baixar_ano")


def _campo(linha: str, inicio: int, fim: int) -> str:
    """Campo pelas posicoes 1-indexadas do layout oficial, pontas incluidas."""
    return linha[inicio - 1:fim]


def _centavos(texto: str) -> Decimal:
    """Preco com 2 decimais implicitos: '000000004125' vira Decimal('41.25')."""
    return Decimal(int(texto)) / Decimal(100)


def parse_linha(linha: str) -> dict | None:
    """Registro de cotacao (TIPREG 01). Outros tipos de registro devolvem None."""
    linha = linha.rstrip("\r\n")
    if _campo(linha, 1, 2) != "01":
        return None
    return {
        "data": dt.datetime.strptime(_campo(linha, 3, 10), "%Y%m%d").date(),
        "codneg": _campo(linha, 13, 24).strip(),
        "tpmerc": _campo(linha, 25, 27),
        "preabe": _centavos(_campo(linha, 57, 69)),
        "premax": _centavos(_campo(linha, 70, 82)),
        "premin": _centavos(_campo(linha, 83, 95)),
        "preult": _centavos(_campo(linha, 109, 121)),
        "quatot": int(_campo(linha, 153, 170)),
        "voltot": _centavos(_campo(linha, 171, 188)),
        "fatcot": int(_campo(linha, 211, 217)),
        "codisi": _campo(linha, 231, 242).strip(),
    }


def ler_cotahist(conteudo_zip: bytes, codigos: set[str]) -> list[dict]:
    """Registros do mercado a vista (TPMERC 010) cujo CODNEG esta em `codigos`.

    O TXT e latin-1 e e lido direto do ZIP, em memoria: nada vai para o disco.
    """
    registros: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(conteudo_zip)) as zf:
        nomes = [nome for nome in zf.namelist() if nome.upper().endswith(".TXT")]
        if not nomes:
            raise ValueError("COTAHIST sem arquivo .TXT dentro do ZIP")
        for nome in nomes:
            with zf.open(nome) as arquivo:
                for bruta in arquivo:
                    registro = parse_linha(bruta.decode("latin-1"))
                    if registro and registro["tpmerc"] == "010" and registro["codneg"] in codigos:
                        registros.append(registro)
    return registros


def grupamentos_do_suplemento(
    payloads: Iterable[dict], isin: str
) -> list[tuple[dt.date, Decimal]]:
    """(lastDatePrior, fator) dos GRUPAMENTO do ISIN, em ordem de data.

    O fator e a razao de acoes (0,01 = 100 para 1), como em entrega/builder.py. Evento
    invalido levanta erro: um fator errado mudaria a serie sem aviso.
    """
    eventos: list[tuple[dt.date, Decimal]] = []
    for payload in payloads:
        if (payload.get("label") or "").strip().upper() != "GRUPAMENTO":
            continue
        if payload.get("isinCode") != isin:
            continue
        data = data_b3(payload.get("lastDatePrior"))
        fator = decimal_b3(str(payload.get("factor")))
        if data is None or fator is None or fator <= 0:
            raise ValueError(f"grupamento invalido para {isin}: {payload!r}")
        eventos.append((data, fator))
    return sorted(eventos)


def _fator_acumulado(data: dt.date, grupamentos: list[tuple[dt.date, Decimal]]) -> Decimal:
    """Produto dos fatores cujo lastDatePrior e >= data: o pregao ainda esta na base antiga."""
    fator = Decimal(1)
    for data_limite, valor in grupamentos:
        if data_limite >= data:
            fator *= valor
    return fator


def _sem_acento(texto: str) -> str:
    """Maiusculas e sem acento: 'RESTITUIÇÃO DE CAPITAL' vira 'RESTITUICAO DE CAPITAL'."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c)).upper().strip()


def _tipo_acao(codigo: str) -> str:
    """typeStock da B3 do codigo negociado: sufixo 3 e ON (AZUL53, JBSS3, NTCO3)."""
    return "ON" if codigo.endswith("3") else "PN"


def _fator_dividendos(
    data: dt.date, dividendos: Sequence[tuple[dt.date, Decimal]]
) -> Decimal:
    """Produto dos f cuja data-com e >= data: a linha ainda nao viu aquele provento."""
    fator = Decimal(1)
    for data_com, valor in dividendos:
        if data_com >= data:
            fator *= valor
    return fator


def fatores_dividendos(
    proventos: Iterable[dict], linhas_cotahist: list[dict], type_stock: str
) -> list[tuple[dt.date, Decimal]]:
    """(data_com, f) por data-com com proventos em dinheiro, com f = 1 - soma / close.

    proventos: registros de RawB3Proventos em dict (type_stock, corporate_action, value_cash,
    last_date_prior_ex, quoted_per_shares). Entram so o type_stock pedido e os rotulos de
    TIPOS_PROVENTO_DINHEIRO. Por acao = value_cash / quoted_per_shares (padrao 1). O close e o
    preco por acao (preult / fatcot) do ultimo pregao ate a data-com: a mesma base do valor.
    Data-com anterior a primeira linha nao toca nenhuma linha da serie e e ignorada.
    """
    precos = sorted((r["data"], r["preult"] / Decimal(r["fatcot"])) for r in linhas_cotahist)
    somas: dict[dt.date, Decimal] = {}
    for provento in proventos:
        if (provento.get("type_stock") or "").strip().upper() != type_stock:
            continue
        if _sem_acento(provento.get("corporate_action") or "") not in TIPOS_PROVENTO_DINHEIRO:
            continue
        data_com = provento.get("last_date_prior_ex")
        valor = provento.get("value_cash")
        if data_com is None or valor is None:
            raise ValueError(f"provento sem data-com ou valor: {provento!r}")
        quoted = decimal_b3(provento.get("quoted_per_shares")) or Decimal(1)
        somas[data_com] = somas.get(data_com, Decimal(0)) + Decimal(str(valor)) / quoted

    fatores: list[tuple[dt.date, Decimal]] = []
    for data_com in sorted(somas):
        anteriores = [preco for data, preco in precos if data <= data_com]
        if not anteriores:
            continue
        close = anteriores[-1]
        if close <= 0:
            raise ValueError(f"preco zero antes da data-com {data_com}")
        fator = 1 - somas[data_com] / close
        if fator < FATOR_DIVIDENDO_MINIMO:
            raise ValueError(
                f"fator de dividendo {fator:.4f} em {data_com} abaixo de "
                f"{FATOR_DIVIDENDO_MINIMO}: provento maior que metade do preco"
            )
        fatores.append((data_com, fator))
    return fatores


def _ajustar(
    linhas: list[dict],
    ticker: str,
    grupamentos: list[tuple[dt.date, Decimal]],
    fator_troca: Decimal = Decimal(1),
    dividendos: Sequence[tuple[dt.date, Decimal]] = (),
) -> list[tuple[PriceRow, dict]]:
    """Linhas da COTAHIST na base atual do papel, com os campos de auditoria de cada uma.

    fator_troca: acoes do ticker atual por acao do codigo negociado (JBSS3 -> JBSS32 = 0,5).
    Preco por acao sobe na mesma proporcao e o volume cai, como no grupamento.
    dividendos: (data_com, f) de fatores_dividendos. adj_close = close x f das datas-com a
    frente da linha.
    """
    datas_vistas: set[dt.date] = set()
    saida: list[tuple[PriceRow, dict]] = []
    for registro in sorted(linhas, key=lambda r: r["data"]):
        if registro["data"] in datas_vistas:
            raise ValueError(f"{ticker}: mais de uma linha da COTAHIST em {registro['data']}")
        datas_vistas.add(registro["data"])

        fatcot = Decimal(registro["fatcot"])
        fator_grupamento = _fator_acumulado(registro["data"], grupamentos)
        # Grupamento e troca de codigo se compoem no mesmo fator: preco e volume usam o total.
        fator = fator_grupamento * fator_troca
        fator_div = _fator_dividendos(registro["data"], dividendos)
        close = _por_acao(registro["preult"], fatcot, fator)
        linha = PriceRow(
            ticker=ticker,
            date=registro["data"],
            open=_por_acao(registro["preabe"], fatcot, fator),
            high=_por_acao(registro["premax"], fatcot, fator),
            low=_por_acao(registro["premin"], fatcot, fator),
            close=close,
            adj_close=close * float(fator_div),
            volume=round(Decimal(registro["quatot"]) * fator),
            source=FONTE,
        )
        auditoria = {
            "codneg": registro["codneg"],
            "fatcot": registro["fatcot"],
            "fator_grupamento": str(fator_grupamento),
            "fator_troca": str(fator_troca),
            "fator_dividendos": str(fator_div),
        }
        saida.append((linha, auditoria))
    return saida


def _por_acao(preco: Decimal, fatcot: Decimal, fator: Decimal) -> float:
    """Preco do lote dividido por FATCOT (vira por acao) e pelo fator acumulado."""
    return float(preco / fatcot / fator)


def serie_corrigida(
    linhas: list[dict],
    ticker: str,
    grupamentos: list[tuple[dt.date, Decimal]],
    fator_troca: Decimal = Decimal(1),
    dividendos: Sequence[tuple[dt.date, Decimal]] = (),
) -> list[PriceRow]:
    """Serie do papel na base atual, pronta para o upsert de prices (source b3_cotahist)."""
    ajustadas = _ajustar(linhas, ticker, grupamentos, fator_troca, dividendos)
    return [linha for linha, _ in ajustadas]


def _grupamentos_do_banco(issuing_company: str, isin: str) -> list[tuple[dt.date, Decimal]]:
    """Le o suplemento (so leitura) e devolve os grupamentos do ISIN."""
    with session_scope() as session:
        payloads = session.scalars(
            select(RawB3Suplemento.payload).where(
                RawB3Suplemento.issuing_company == issuing_company,
                RawB3Suplemento.tipo == "stock",
            )
        ).all()
    return grupamentos_do_suplemento(payloads, isin)


def _proventos_do_banco(issuing_company: str) -> list[dict]:
    """Proventos da empresa em raw_b3_proventos (so leitura), no formato de fatores_dividendos."""
    with session_scope() as session:
        linhas = session.execute(
            select(
                RawB3Proventos.type_stock,
                RawB3Proventos.corporate_action,
                RawB3Proventos.value_cash,
                RawB3Proventos.last_date_prior_ex,
                RawB3Proventos.quoted_per_shares,
            ).where(RawB3Proventos.issuing_company == issuing_company)
        ).all()
    return [
        {
            "type_stock": tipo,
            "corporate_action": acao,
            "value_cash": valor,
            "last_date_prior_ex": data_com,
            "quoted_per_shares": quoted,
        }
        for tipo, acao, valor, data_com, quoted in linhas
    ]


def _gravar_csv(caminho: Path, ajustadas: list[tuple[PriceRow, dict]]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", newline="", encoding="utf-8") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(COLUNAS_CSV)
        for linha, auditoria in ajustadas:
            escritor.writerow([
                linha.ticker, linha.date.isoformat(), linha.open, linha.high, linha.low,
                linha.close, linha.adj_close, linha.volume, auditoria["codneg"],
                auditoria["fatcot"], auditoria["fator_grupamento"], auditoria["fator_troca"],
                auditoria["fator_dividendos"],
            ])


def _baixar_proventos_yahoo(
    yahoo: YahooPriceCollector, correcoes: dict[str, CorrecaoPrecoB3]
) -> dict[str, list[tuple[dt.date, float]]]:
    """Dividendos do Yahoo por ticker, na janela de cada correcao com proventos_yahoo."""
    return {
        ticker: yahoo.baixar_dividendos(correcao.proventos_yahoo, correcao.inicio, correcao.fim)
        for ticker, correcao in correcoes.items()
        if correcao.proventos_yahoo
    }


def _proventos_yahoo_da_correcao(
    ticker: str,
    correcao: CorrecaoPrecoB3,
    issuing_company: str,
    dividendos_ex: Sequence[tuple[dt.date, float]],
    brutos: list[dict],
    inicio: dt.date,
    fim: dt.date,
    datas_b3: set[dt.date | None],
) -> list[dict]:
    """Dividendos do Yahoo com data-com = ultimo pregao anterior a data ex (na COTAHIST).

    Descarta a data-com que a B3 ja tem em `datas_b3` e as que caem fora de [inicio, fim].
    Cada linha sai no formato do CSV de proventos_yahoo.
    """
    pregoes = sorted({r["data"] for r in brutos})
    linhas: list[dict] = []
    for data_ex, valor in sorted(dividendos_ex):
        anteriores = [data for data in pregoes if data < data_ex]
        if not anteriores:
            logger.warning("proventos Yahoo de %s: %s sem pregao anterior; pulado", ticker, data_ex)
            continue
        data_com = anteriores[-1]
        if not inicio <= data_com <= fim or data_com in datas_b3:
            continue
        linhas.append({
            "ticker": ticker,
            "issuing_company": issuing_company,
            "ticker_negociado": correcao.codigos[0],
            "data_ex": data_ex,
            "data_com": data_com,
            "valor_por_acao": valor,
            "fonte": "yahoo",
        })
    return linhas


def _provento_de_yahoo(linha: dict, type_stock: str) -> dict:
    """Linha de proventos_yahoo no formato de _proventos_do_banco (entra em fatores_dividendos)."""
    return {
        "type_stock": type_stock,
        "corporate_action": "DIVIDENDO",
        "value_cash": linha["valor_por_acao"],
        "last_date_prior_ex": linha["data_com"],
        # String, como em RawB3Proventos.quoted_per_shares (decimal_b3 espera texto).
        "quoted_per_shares": "1",
    }


def _gravar_proventos_yahoo(caminho: Path, linhas: list[dict]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", newline="", encoding="utf-8") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(COLUNAS_PROVENTOS_YAHOO)
        for linha in linhas:
            escritor.writerow([
                linha["ticker"], linha["issuing_company"], linha["ticker_negociado"],
                linha["data_ex"].isoformat(), linha["data_com"].isoformat(),
                linha["valor_por_acao"], linha["fonte"],
            ])


def gerar_correcoes(
    pasta: Path = PASTA_CORRECOES,
    pasta_proventos: Path = PASTA_PROVENTOS_YAHOO,
    yahoo: YahooPriceCollector | None = None,
) -> dict[str, object]:
    """Grava um CSV por entrada de CORRECOES_PRECO_B3 em `pasta`.

    Baixa cada ano do COTAHIST uma vez, para todas as correcoes, e corta cada serie em
    max(inicio, price_start). Le grupamentos e proventos do banco. Correcao com proventos_yahoo
    ganha os dividendos do Yahoo (gravados em `pasta_proventos`). `yahoo` e injetavel para
    teste; sem ele, um coletor proprio so e criado se alguma correcao precisar. Nao grava no banco.
    """
    settings = get_settings()
    codigos = {codigo for correcao in CORRECOES_PRECO_B3.values() for codigo in correcao.codigos}
    anos = range(
        min(c.inicio.year for c in CORRECOES_PRECO_B3.values()),
        max(c.fim.year for c in CORRECOES_PRECO_B3.values()) + 1,
    )
    brutos: dict[str, list[dict]] = {ticker: [] for ticker in CORRECOES_PRECO_B3}
    with CotahistCollector() as coletor:
        for ano in anos:
            for registro in ler_cotahist(coletor.baixar_ano(ano), codigos):
                for ticker, correcao in CORRECOES_PRECO_B3.items():
                    if registro["codneg"] in correcao.codigos:
                        brutos[ticker].append(registro)

    dividendos_yahoo: dict[str, list[tuple[dt.date, float]]] = {}
    if any(c.proventos_yahoo for c in CORRECOES_PRECO_B3.values()):
        proprio = yahoo is None
        coletor_yahoo = YahooPriceCollector(tickers=[]) if proprio else yahoo
        try:
            dividendos_yahoo = _baixar_proventos_yahoo(coletor_yahoo, CORRECOES_PRECO_B3)
        finally:
            if proprio:
                coletor_yahoo.close()

    stats: dict[str, object] = {}
    for ticker, correcao in CORRECOES_PRECO_B3.items():
        # Emissor do suplemento: o do config; sem ele, o ticker sem o numero da classe.
        issuing_company = correcao.issuing_company or ticker.rstrip("0123456789")
        inicio = max(correcao.inicio, settings.price_start)
        fim = correcao.fim
        grupamentos = _grupamentos_do_banco(issuing_company, correcao.isin)
        proventos_banco = _proventos_do_banco(issuing_company)
        # Proventos fora da janela nao mudam nenhuma linha. Os de depois do fim ja estao no
        # Yahoo, que fornece a base de ajuste (aplicar_correcoes): conta-los aqui dobraria.
        proventos = [
            p for p in proventos_banco
            if p["last_date_prior_ex"] is None or inicio <= p["last_date_prior_ex"] <= fim
        ]
        tipo_acao = _tipo_acao(correcao.codigos[0])
        if correcao.proventos_yahoo:
            datas_b3 = {p["last_date_prior_ex"] for p in proventos_banco}
            yahoo_linhas = _proventos_yahoo_da_correcao(
                ticker, correcao, issuing_company, dividendos_yahoo.get(ticker, []),
                brutos[ticker], inicio, fim, datas_b3,
            )
            proventos = [
                *proventos, *(_provento_de_yahoo(linha_y, tipo_acao) for linha_y in yahoo_linhas)
            ]
            _gravar_proventos_yahoo(pasta_proventos / f"{ticker}.csv", yahoo_linhas)
            stats[f"{ticker} proventos yahoo"] = len(yahoo_linhas)
        dividendos = fatores_dividendos(proventos, brutos[ticker], tipo_acao)

        linhas = [r for r in brutos[ticker] if inicio <= r["data"] <= fim]
        ajustadas = _ajustar(
            linhas, ticker, grupamentos, Decimal(str(correcao.fator_troca)), dividendos
        )
        caminho = pasta / f"{ticker}.csv"
        _gravar_csv(caminho, ajustadas)
        stats[f"{ticker} linhas"] = len(ajustadas)
        stats[f"{ticker} arquivo"] = str(caminho)
    return stats


def _float_ou_none(texto: str) -> float | None:
    return float(texto) if texto else None


def _linha_csv(registro: dict) -> PriceRow:
    return PriceRow(
        ticker=registro["ticker"],
        date=dt.date.fromisoformat(registro["date"]),
        open=_float_ou_none(registro["open"]),
        high=_float_ou_none(registro["high"]),
        low=_float_ou_none(registro["low"]),
        close=_float_ou_none(registro["close"]),
        adj_close=_float_ou_none(registro["adj_close"]),
        volume=int(registro["volume"]) if registro["volume"] else None,
        source=FONTE,
    )


def carregar_correcoes(pasta: Path = PASTA_CORRECOES) -> dict[str, list[PriceRow]]:
    """Le os CSVs de correcao da pasta, indexados pelo ticker. Pasta ausente devolve vazio."""
    if not pasta.is_dir():
        logger.warning("correcoes de precos: pasta %s nao existe", pasta)
        return {}
    correcoes: dict[str, list[PriceRow]] = {}
    for caminho in sorted(pasta.glob("*.csv")):
        with caminho.open(newline="", encoding="utf-8") as arquivo:
            linhas = [_linha_csv(registro) for registro in csv.DictReader(arquivo)]
        if linhas:
            correcoes[linhas[0].ticker] = linhas
    return correcoes


def _lacuna_csv(registro: dict) -> LacunaPreco:
    def data(texto: str) -> dt.date | None:
        return dt.date.fromisoformat(texto) if texto else None

    def ohlc(prefixo: str) -> tuple[float | None, ...]:
        return tuple(
            _float_ou_none(registro[f"{prefixo}_{campo}"])
            for campo in ("open", "high", "low", "close")
        )

    return LacunaPreco(
        ticker=registro["ticker"],
        date=dt.date.fromisoformat(registro["date"]),
        codneg=registro["codneg"],
        open=_float_ou_none(registro["open"]),
        high=_float_ou_none(registro["high"]),
        low=_float_ou_none(registro["low"]),
        close=float(registro["close"]),
        volume=int(registro["volume"]) if registro["volume"] else None,
        fonte=registro["fonte"],
        ref_anterior=data(registro["ref_anterior"]),
        ant=ohlc("ant"),
        ref_seguinte=data(registro["ref_seguinte"]),
        seg=ohlc("seg"),
    )


def carregar_lacunas(pasta: Path = PASTA_LACUNAS) -> dict[str, list[LacunaPreco]]:
    """Le os CSVs de lacunas da pasta, indexados pelo ticker, em ordem de data.

    Pasta ausente devolve vazio.
    """
    if not pasta.is_dir():
        logger.warning("lacunas de precos: pasta %s nao existe", pasta)
        return {}
    lacunas: dict[str, list[LacunaPreco]] = {}
    for caminho in sorted(pasta.glob("*.csv")):
        with caminho.open(newline="", encoding="utf-8") as arquivo:
            for registro in csv.DictReader(arquivo):
                lacunas.setdefault(registro["ticker"], []).append(_lacuna_csv(registro))
    for lista in lacunas.values():
        lista.sort(key=lambda lacuna: lacuna.date)
    return lacunas


def _gravar_lacunas(caminho: Path, lacunas: list[LacunaPreco]) -> None:
    """Grava as lacunas no CSV de COLUNAS_LACUNAS. Campo None vira vazio."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", newline="", encoding="utf-8") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(COLUNAS_LACUNAS)
        for lacuna in lacunas:
            escritor.writerow([
                lacuna.ticker, lacuna.date.isoformat(), lacuna.codneg, lacuna.open,
                lacuna.high, lacuna.low, lacuna.close, lacuna.volume, lacuna.fonte,
                lacuna.ref_anterior.isoformat() if lacuna.ref_anterior else None,
                *lacuna.ant,
                lacuna.ref_seguinte.isoformat() if lacuna.ref_seguinte else None,
                *lacuna.seg,
            ])


def _calendario_ibov(anos: Iterable[int]) -> dict[dt.date, float]:
    """Fechamento do IBOV por pregao, da B3 (GetPortfolioDay), de price_start a fim_coleta.

    Nada e gravado aqui. Se a B3 nao devolver JSON de algum ano, levanta RuntimeError.
    """
    settings = get_settings()
    calendario: dict[dt.date, float] = {}
    with B3IndicesCollector(anos=[]) as coletor:
        for ano in anos:
            inicio = max(dt.date(ano, 1, 1), settings.price_start)
            fim = min(dt.date(ano, 12, 31), settings.fim_coleta)
            if inicio > fim:
                continue
            corpo = coletor.get_json(
                METODO_DIA,
                {"index": "IBOV", "language": "pt-br", "year": str(ano)},
                base=INDEX_BASE,
            )
            if not isinstance(corpo, dict):
                raise RuntimeError(f"B3 nao devolveu o IBOV de {ano}")
            linhas = parse_portfolio_day(
                corpo, ticker=BENCHMARK_TICKER, ano=ano, inicio=inicio, fim=fim
            )
            for linha in linhas:
                calendario[linha["date"]] = linha["close"]
    return calendario


def _precos_do_banco(tickers: Iterable[str]) -> dict[str, dict[dt.date, tuple]]:
    """Precos ja gravados em raw_prices por ticker: (open, high, low, close, source).

    So leitura. Linhas com close nulo ficam de fora.
    """
    lista = list(tickers)
    precos: dict[str, dict[dt.date, tuple]] = {ticker: {} for ticker in lista}
    consulta = select(
        Price.ticker, Price.date, Price.open, Price.high, Price.low, Price.close, Price.source
    ).where(Price.ticker.in_(lista), Price.close.is_not(None))
    with session_scope() as session:
        for ticker, data, abertura, maxima, minima, fechamento, fonte in session.execute(consulta):
            precos[ticker][data] = (abertura, maxima, minima, fechamento, fonte)
    return precos


def candidatos_codigo(ticker: str) -> list[str]:
    """Codigos que podem ter negociado `ticker` no COTAHIST, sem repetir.

    Ordem: o proprio ticker, os antigos de MUDANCAS_TICKER que viraram este e os de
    CODIGOS_ANTERIORES_COTAHIST.
    """
    antigos = [antigo for antigo, novo, _ in MUDANCAS_TICKER if novo == ticker]
    anteriores = CODIGOS_ANTERIORES_COTAHIST.get(ticker, ())
    return list(dict.fromkeys([ticker, *antigos, *anteriores]))


def encontrar_lacunas(
    calendario: dict[dt.date, float],
    precos_ticker: dict[dt.date, tuple],
    janelas_excluidas: Iterable[tuple[dt.date, dt.date]],
) -> list[dt.date]:
    """Pregoes do calendario sem linha do Yahoo, entre a 1a e a ultima data do ticker.

    Ficam de fora as janelas de correcao (que ja trazem a serie da B3). Um dia gravado antes
    como b3_cotahist continua sendo lacuna: gerar de novo nao muda o resultado.
    """
    if not precos_ticker:
        return []
    primeira, ultima = min(precos_ticker), max(precos_ticker)
    janelas = list(janelas_excluidas)
    return [
        data
        for data in sorted(calendario)
        if primeira <= data <= ultima
        and not (data in precos_ticker and precos_ticker[data][4] == "yahoo")
        and not any(inicio <= data <= fim for inicio, fim in janelas)
    ]


def _vizinhos(data: dt.date, validos: list[dt.date]) -> tuple[dt.date | None, dt.date | None]:
    """Data de `validos` (ordenada) logo antes e logo depois de `data`."""
    i = bisect_left(validos, data)
    j = bisect_right(validos, data)
    antes = validos[i - 1] if i > 0 else None
    depois = validos[j] if j < len(validos) else None
    return antes, depois


def _ohlc_por_acao(registro: dict | None) -> tuple[float, float, float, float] | None:
    """O/H/L/C por acao (preco do lote / FATCOT). Registro ausente ou preco zero vira None."""
    if registro is None:
        return None
    precos = (registro["preabe"], registro["premax"], registro["premin"], registro["preult"])
    if any(preco <= 0 for preco in precos):
        return None
    fatcot = Decimal(registro["fatcot"])
    abertura, maxima, minima, fechamento = (
        _por_acao(preco, fatcot, Decimal(1)) for preco in precos
    )
    return abertura, maxima, minima, fechamento


@dataclass(frozen=True)
class _Pendente:
    """Pregao de um papel sem Yahoo, com os dois vizinhos do Yahoo (O/H/L/C de cada um)."""

    ticker: str
    data: dt.date
    ref_anterior: dt.date
    ref_seguinte: dt.date
    yahoo_ant: tuple[float | None, ...]
    yahoo_seg: tuple[float | None, ...]


def _lacuna_do_papel(
    pendente: _Pendente, mapa: dict[tuple[str, dt.date], dict]
) -> tuple[LacunaPreco | None, float | None]:
    """Lacuna pelo codigo com a menor diferenca de razao nos vizinhos.

    Devolve (lacuna, diferenca). Lacuna None quando nenhum codigo tem os 3 pregoes ou quando
    a melhor diferenca passa de TOLERANCIA_RAZAO_LACUNA (diferenca None: sem candidato).
    """
    melhor: tuple[float, LacunaPreco] | None = None
    for codigo in candidatos_codigo(pendente.ticker):
        registro_dia = mapa.get((codigo, pendente.data))
        dia = _ohlc_por_acao(registro_dia)
        ant = _ohlc_por_acao(mapa.get((codigo, pendente.ref_anterior)))
        seg = _ohlc_por_acao(mapa.get((codigo, pendente.ref_seguinte)))
        if dia is None or ant is None or seg is None:
            continue
        r_ant = razao_mediana(pendente.yahoo_ant, ant)
        r_seg = razao_mediana(pendente.yahoo_seg, seg)
        if not r_ant or not r_seg:
            continue
        diferenca = abs(r_ant / r_seg - 1)
        if melhor is not None and diferenca >= melhor[0]:
            continue
        lacuna = LacunaPreco(
            ticker=pendente.ticker,
            date=pendente.data,
            codneg=codigo,
            open=dia[0],
            high=dia[1],
            low=dia[2],
            close=dia[3],
            volume=int(registro_dia["quatot"]),
            fonte=FONTE,
            ref_anterior=pendente.ref_anterior,
            ant=ant,
            ref_seguinte=pendente.ref_seguinte,
            seg=seg,
        )
        melhor = (diferenca, lacuna)
    if melhor is None:
        return None, None
    diferenca, lacuna = melhor
    if diferenca > TOLERANCIA_RAZAO_LACUNA:
        return None, diferenca
    return lacuna, diferenca


def gerar_lacunas(pasta: Path = PASTA_LACUNAS) -> dict[str, object]:
    """Grava `pasta/lacunas.csv` com os pregoes que o Yahoo nao tem (ver completar_lacunas).

    Pregao do calendario do IBOV (B3) sem linha do Yahoo e fora das correcoes. ^BVSP usa o
    proprio IBOV da B3. Papel sai do COTAHIST, pelo codigo com a razao Yahoo/B3 mais estavel
    nos vizinhos. Nao grava no banco: le raw_prices so para medir a escala.
    """
    settings = get_settings()
    macro = set(MACRO_YAHOO.values())
    tickers = [ticker for ticker in tickers_padrao() if ticker not in macro]
    calendario = _calendario_ibov(range(settings.price_start.year, settings.fim_coleta.year + 1))
    precos = _precos_do_banco(tickers)
    janelas: dict[str, list[tuple[dt.date, dt.date]]] = {
        ticker: [(min(r.date for r in rows), max(r.date for r in rows))]
        for ticker, rows in carregar_correcoes().items()
    }

    lacunas: list[LacunaPreco] = []
    pendentes: list[_Pendente] = []
    encontradas = 0
    rejeitadas = 0
    for ticker in tickers:
        precos_ticker = precos.get(ticker, {})
        janelas_ticker = janelas.get(ticker, [])
        dias = encontrar_lacunas(calendario, precos_ticker, janelas_ticker)
        encontradas += len(dias)
        if ticker == BENCHMARK_TICKER:
            lacunas.extend(
                LacunaPreco(
                    ticker=ticker, date=dia, codneg="IBOV", open=None, high=None, low=None,
                    close=calendario[dia], volume=None, fonte="b3",
                )
                for dia in dias
            )
            continue
        marcadas = set(dias)
        validos = sorted(
            data
            for data, preco in precos_ticker.items()
            if preco[4] == "yahoo"
            and data not in marcadas
            and not any(inicio <= data <= fim for inicio, fim in janelas_ticker)
        )
        for dia in dias:
            antes, depois = _vizinhos(dia, validos)
            if antes is None or depois is None:
                rejeitadas += 1
                logger.warning("lacuna %s %s: sem vizinho do Yahoo dos dois lados", ticker, dia)
                continue
            pendentes.append(
                _Pendente(
                    ticker=ticker,
                    data=dia,
                    ref_anterior=antes,
                    ref_seguinte=depois,
                    yahoo_ant=precos_ticker[antes][:4],
                    yahoo_seg=precos_ticker[depois][:4],
                )
            )

    anos = sorted(
        {ano for p in pendentes for ano in (p.data.year, p.ref_anterior.year, p.ref_seguinte.year)}
    )
    mapa: dict[tuple[str, dt.date], dict] = {}
    if pendentes:
        codigos = {codigo for p in pendentes for codigo in candidatos_codigo(p.ticker)}
        with CotahistCollector() as coletor:
            for ano in anos:
                for registro in ler_cotahist(coletor.baixar_ano(ano), codigos):
                    mapa[(registro["codneg"], registro["data"])] = registro

    for pendente in pendentes:
        lacuna, diferenca = _lacuna_do_papel(pendente, mapa)
        if lacuna is None:
            rejeitadas += 1
            logger.warning(
                "lacuna %s %s rejeitada: razao instavel ou sem candidato (diferenca %s)",
                pendente.ticker, pendente.data, diferenca,
            )
            continue
        lacunas.append(lacuna)

    lacunas.sort(key=lambda lacuna: (lacuna.ticker, lacuna.date))
    caminho = pasta / "lacunas.csv"
    _gravar_lacunas(caminho, lacunas)
    return {
        "lacunas": encontradas,
        "gravadas": len(lacunas),
        "rejeitadas": rejeitadas,
        "anos baixados": len(anos),
        "arquivo": str(caminho),
    }


def carregar_proventos_yahoo(pasta: Path = PASTA_PROVENTOS_YAHOO) -> list[dict]:
    """Le os CSVs de proventos_yahoo da pasta (dividendos do Yahoo). Pasta ausente devolve vazio.

    Cada dict: ticker (atual), issuing_company, ticker_negociado, data_ex, data_com,
    valor_por_acao (por acao do ticker negociado) e fonte.
    """
    if not pasta.is_dir():
        logger.warning("proventos Yahoo: pasta %s nao existe", pasta)
        return []
    proventos: list[dict] = []
    for caminho in sorted(pasta.glob("*.csv")):
        with caminho.open(newline="", encoding="utf-8") as arquivo:
            for registro in csv.DictReader(arquivo):
                proventos.append({
                    "ticker": registro["ticker"],
                    "issuing_company": registro["issuing_company"],
                    "ticker_negociado": registro["ticker_negociado"],
                    "data_ex": dt.date.fromisoformat(registro["data_ex"]),
                    "data_com": dt.date.fromisoformat(registro["data_com"]),
                    "valor_por_acao": float(registro["valor_por_acao"]),
                    "fonte": registro["fonte"],
                })
    return proventos
