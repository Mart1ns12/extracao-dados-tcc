"""Precos da B3 (COTAHIST) para os papeis em que o Yahoo erra. Lista: CORRECOES_PRECO_B3.

O arquivo anual COTAHIST_A{ano}.ZIP traz, por papel e pregao, o preco na cotacao do papel.
Quando a cotacao e por lote (FATCOT > 1), o preco por acao e preco / FATCOT. Os grupamentos
do suplemento da B3 (fator < 1, convencao de entrega/builder.py) levam a serie para a base
atual do papel.

O resultado vai para data/correcoes_precos/<TICKER>.csv, versionado. O coletor do Yahoo le
esses CSVs e troca o trecho errado (aplicar_correcoes, em prices_yahoo.py).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import zipfile
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from tcc_scraper.collectors.b3_base import USER_AGENT_B3, data_b3, decimal_b3
from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.collectors.prices_yahoo import PriceRow
from tcc_scraper.config import CORRECOES_PRECO_B3, PROJECT_ROOT
from tcc_scraper.db.models_fontes import RawB3Suplemento
from tcc_scraper.db.session import session_scope

logger = logging.getLogger(__name__)

COTAHIST_URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP"
BVMF_DOMINIO = "bvmf.bmfbovespa.com.br"
FONTE = "b3_cotahist"
PASTA_CORRECOES = PROJECT_ROOT / "data" / "correcoes_precos"
COLUNAS_CSV = [
    "ticker", "date", "open", "high", "low", "close", "adj_close", "volume",
    "codneg", "fatcot", "fator_grupamento",
]


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


def _ajustar(
    linhas: list[dict], ticker: str, grupamentos: list[tuple[dt.date, Decimal]]
) -> list[tuple[PriceRow, dict]]:
    """Linhas da COTAHIST na base atual do papel, com os campos de auditoria de cada uma."""
    datas_vistas: set[dt.date] = set()
    saida: list[tuple[PriceRow, dict]] = []
    for registro in sorted(linhas, key=lambda r: r["data"]):
        if registro["data"] in datas_vistas:
            raise ValueError(f"{ticker}: mais de uma linha da COTAHIST em {registro['data']}")
        datas_vistas.add(registro["data"])

        fatcot = Decimal(registro["fatcot"])
        fator = _fator_acumulado(registro["data"], grupamentos)
        linha = PriceRow(
            ticker=ticker,
            date=registro["data"],
            open=_por_acao(registro["preabe"], fatcot, fator),
            high=_por_acao(registro["premax"], fatcot, fator),
            low=_por_acao(registro["premin"], fatcot, fator),
            close=_por_acao(registro["preult"], fatcot, fator),
            adj_close=_por_acao(registro["preult"], fatcot, fator),
            volume=round(Decimal(registro["quatot"]) * fator),
            source=FONTE,
        )
        auditoria = {
            "codneg": registro["codneg"],
            "fatcot": registro["fatcot"],
            "fator_grupamento": str(fator),
        }
        saida.append((linha, auditoria))
    return saida


def _por_acao(preco: Decimal, fatcot: Decimal, fator: Decimal) -> float:
    """Preco do lote dividido por FATCOT (vira por acao) e pelo fator acumulado."""
    return float(preco / fatcot / fator)


def serie_corrigida(
    linhas: list[dict], ticker: str, grupamentos: list[tuple[dt.date, Decimal]]
) -> list[PriceRow]:
    """Serie do papel na base atual, pronta para o upsert de prices (source b3_cotahist)."""
    return [linha for linha, _ in _ajustar(linhas, ticker, grupamentos)]


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


def _gravar_csv(caminho: Path, ajustadas: list[tuple[PriceRow, dict]]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", newline="", encoding="utf-8") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(COLUNAS_CSV)
        for linha, auditoria in ajustadas:
            escritor.writerow([
                linha.ticker, linha.date.isoformat(), linha.open, linha.high, linha.low,
                linha.close, linha.adj_close, linha.volume, auditoria["codneg"],
                auditoria["fatcot"], auditoria["fator_grupamento"],
            ])


def gerar_correcoes(pasta: Path = PASTA_CORRECOES) -> dict[str, object]:
    """Grava um CSV por entrada de CORRECOES_PRECO_B3 em `pasta`.

    Baixa os anos da janela da B3 e le os grupamentos do banco. Nao grava no banco.
    """
    stats: dict[str, object] = {}
    with CotahistCollector() as coletor:
        for ticker, correcao in CORRECOES_PRECO_B3.items():
            # Emissor = ticker sem o numero da classe (AZUL3 -> AZUL), como no suplemento.
            issuing_company = ticker.rstrip("0123456789")
            grupamentos = _grupamentos_do_banco(issuing_company, correcao.isin)

            linhas: list[dict] = []
            for ano in range(correcao.inicio.year, correcao.fim.year + 1):
                conteudo = coletor.baixar_ano(ano)
                linhas += ler_cotahist(conteudo, set(correcao.codigos))
            linhas = [r for r in linhas if correcao.inicio <= r["data"] <= correcao.fim]

            ajustadas = _ajustar(linhas, ticker, grupamentos)
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
