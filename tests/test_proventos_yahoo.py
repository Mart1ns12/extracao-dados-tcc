"""Dividendos do Yahoo como complemento dos proventos da B3 (JBS S.A., 2020-2025).

Sem rede e sem banco: o chart do Yahoo e um dict montado aqui, o COTAHIST entra como registro
ja decodificado (como ler_cotahist devolve) e os proventos do banco sao monkeypatched.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tcc_scraper.collectors import b3_cotahist
from tcc_scraper.collectors.b3_cotahist import (
    carregar_correcoes,
    carregar_proventos_yahoo,
    gerar_correcoes,
)
from tcc_scraper.collectors.prices_yahoo import YahooPriceCollector, parse_dividendos
from tcc_scraper.config import CorrecaoPrecoB3
from tcc_scraper.entrega import builder as B

SP = ZoneInfo("America/Sao_Paulo")
ISIN_JBS = "BRJBSSACNOR8"


def _chart_com_dividendos(eventos: dict) -> dict:
    return {"chart": {
        "result": [{"meta": {"exchangeTimezoneName": "America/Sao_Paulo"},
                    "events": {"dividends": eventos}}],
        "error": None,
    }}


def _ts(ano: int, mes: int, dia: int, hora: int = 0) -> int:
    return int(dt.datetime(ano, mes, dia, hora, tzinfo=SP).timestamp())


# -- parse_dividendos ----------------------------------------------------------------------
def test_parse_dividendos_data_ex_no_fuso_da_bolsa_e_ordenada() -> None:
    # 22h de 28/04 em Sao Paulo ja e 01h de 29/04 em UTC: a data ex sai em 28/04.
    payload = _chart_com_dividendos({
        str(_ts(2021, 8, 17)): {"amount": 1.0013, "date": _ts(2021, 8, 17)},
        str(_ts(2020, 4, 28, 22)): {"amount": 0.5406, "date": _ts(2020, 4, 28, 22)},
    })
    assert parse_dividendos(payload) == [
        (dt.date(2020, 4, 28), 0.5406),
        (dt.date(2021, 8, 17), 1.0013),
    ]


def test_parse_dividendos_usa_a_chave_quando_falta_date() -> None:
    payload = _chart_com_dividendos({str(_ts(2024, 8, 20)): {"amount": 2.0}})
    assert parse_dividendos(payload) == [(dt.date(2024, 8, 20), 2.0)]


def test_parse_dividendos_sem_eventos_ou_sem_resultado_devolve_vazio() -> None:
    assert parse_dividendos(_chart_com_dividendos({})) == []
    assert parse_dividendos({"chart": {"result": [{"meta": {}}]}}) == []
    assert parse_dividendos({"chart": {"result": None}}) == []


# -- baixar_dividendos (throttle e fetch do Collector, sem rede) -----------------------------
def test_baixar_dividendos_pede_so_eventos_de_dividendo_e_le_o_json(monkeypatch) -> None:
    urls: list[str] = []

    class RespostaFalsa:
        def json(self) -> dict:
            return _chart_com_dividendos(
                {str(_ts(2025, 4, 30)): {"amount": 2.0, "date": _ts(2025, 4, 30)}}
            )

    def fetch_falso(url: str, **_) -> RespostaFalsa:
        urls.append(url)
        return RespostaFalsa()

    with YahooPriceCollector(tickers=[]) as coletor:
        monkeypatch.setattr(coletor, "fetch", fetch_falso)
        dividendos = coletor.baixar_dividendos(
            "JBSS32.SA", dt.date(2019, 12, 2), dt.date(2025, 6, 6)
        )

    assert dividendos == [(dt.date(2025, 4, 30), 2.0)]
    (url,) = urls
    assert "/JBSS32.SA?" in url
    assert "interval=1d" in url
    assert "events=div%2Csplit" in url
    # period2 e exclusivo: o dia 06/06 entra inteiro, entao o limite e 07/06 00h UTC.
    assert f"period2={int(dt.datetime(2025, 6, 7, tzinfo=dt.UTC).timestamp())}" in url


# -- gerar_correcoes com dividendos do Yahoo (sem rede, sem banco) ---------------------------
class _YahooFalso:
    def __init__(self, dividendos: list[tuple[dt.date, float]]) -> None:
        self.dividendos = dividendos
        self.chamadas: list[tuple[str, dt.date, dt.date]] = []

    def baixar_dividendos(self, simbolo: str, inicio: dt.date, fim: dt.date):
        self.chamadas.append((simbolo, inicio, fim))
        return self.dividendos


class _ColetorCotahistFalso:
    def __enter__(self):
        return self

    def __exit__(self, *_) -> bool:
        return False

    def baixar_ano(self, ano: int) -> bytes:
        return b""


def _registro_cotahist(data: dt.date) -> dict:
    """Registro ja decodificado (como ler_cotahist): JBSS3 a 10,00 por acao, sem grupamento."""
    return {
        "data": data, "codneg": "JBSS3", "tpmerc": "010",
        "preabe": Decimal("10.00"), "premax": Decimal("10.00"),
        "premin": Decimal("10.00"), "preult": Decimal("10.00"),
        "quatot": 1000, "voltot": Decimal("10000"), "fatcot": 1, "codisi": ISIN_JBS,
    }


def _provento_b3(data_com: dt.date, valor: float) -> dict:
    return {
        "type_stock": "ON", "corporate_action": "DIVIDENDO", "value_cash": valor,
        "last_date_prior_ex": data_com, "quoted_per_shares": None,
    }


def test_gerar_correcoes_complementa_proventos_com_yahoo_e_descarta_o_que_a_b3_ja_tem(
    tmp_path, monkeypatch
) -> None:
    dias = [dt.date(2026, 1, d) for d in range(12, 17)]
    correcoes = {
        "JBSS32": CorrecaoPrecoB3(
            codigos=("JBSS3",), isin=ISIN_JBS, inicio=dt.date(2026, 1, 12),
            fim=dt.date(2026, 1, 16), issuing_company="JBSS", fator_troca=0.5,
            proventos_yahoo="JBSS32.SA",
        ),
    }
    yahoo = _YahooFalso([
        (dt.date(2026, 1, 3), 0.2),   # sem pregao anterior na COTAHIST: pulado
        (dt.date(2026, 1, 13), 0.5),  # data-com 12/01, que a B3 ja tem: descartado
        (dt.date(2026, 1, 15), 1.0),  # data-com 14/01: entra
    ])
    monkeypatch.setattr(b3_cotahist, "CORRECOES_PRECO_B3", correcoes)
    monkeypatch.setattr(b3_cotahist, "CotahistCollector", _ColetorCotahistFalso)
    registros = [_registro_cotahist(d) for d in dias]
    monkeypatch.setattr(b3_cotahist, "ler_cotahist", lambda _conteudo, _codigos: registros)
    monkeypatch.setattr(
        b3_cotahist, "get_settings", lambda: type("S", (), {"price_start": dt.date(2026, 1, 1)})
    )
    monkeypatch.setattr(b3_cotahist, "_grupamentos_do_banco", lambda emissor, isin: [])
    monkeypatch.setattr(
        b3_cotahist,
        "_proventos_do_banco",
        lambda emissor: [_provento_b3(dt.date(2026, 1, 12), 0.5)] if emissor == "JBSS" else [],
    )

    pasta_proventos = tmp_path / "proventos"
    stats = gerar_correcoes(tmp_path / "correcoes", pasta_proventos, yahoo=yahoo)

    # A janela e a da correcao (sem o corte de price_start), como em gerar_correcoes.
    assert yahoo.chamadas == [("JBSS32.SA", dt.date(2026, 1, 12), dt.date(2026, 1, 16))]
    assert stats["JBSS32 proventos yahoo"] == 1

    (provento,) = carregar_proventos_yahoo(pasta_proventos)
    assert provento == {
        "ticker": "JBSS32", "issuing_company": "JBSS", "ticker_negociado": "JBSS3",
        "data_ex": dt.date(2026, 1, 15), "data_com": dt.date(2026, 1, 14),
        "valor_por_acao": 1.0, "fonte": "yahoo",
    }

    # JBSS3 a 10,00 com troca 0,5 = 20,00. Ate 12/01 pesam os dois dividendos (B3 0,50 e
    # Yahoo 1,00 sobre 10,00): f = 0,95 x 0,90. De 13/01 a 14/01 so o do Yahoo: f = 0,90.
    ajustadas = {r.date: r.adj_close for r in carregar_correcoes(tmp_path / "correcoes")["JBSS32"]}
    assert ajustadas[dt.date(2026, 1, 12)] == pytest.approx(20.0 * 0.95 * 0.90)
    assert ajustadas[dt.date(2026, 1, 13)] == pytest.approx(20.0 * 0.90)
    assert ajustadas[dt.date(2026, 1, 14)] == pytest.approx(20.0 * 0.90)
    assert ajustadas[dt.date(2026, 1, 15)] == pytest.approx(20.0)  # depois da data-com


def test_carregar_proventos_yahoo_pasta_ausente_devolve_vazio(tmp_path) -> None:
    assert carregar_proventos_yahoo(tmp_path / "nao_existe") == []


# -- montar_proventos com o complemento do Yahoo ---------------------------------------------
def _linha_yahoo(
    data_com: dt.date = dt.date(2024, 5, 1), valor: float = 1.0, negociado: str = "JBSS3"
) -> dict:
    return {
        "ticker": "JBSS32", "issuing_company": "JBSS", "ticker_negociado": negociado,
        "data_ex": data_com + dt.timedelta(days=1), "data_com": data_com,
        "valor_por_acao": valor, "fonte": "yahoo",
    }


def _prov_b3_jbs(data_com: dt.date, valor: float) -> dict:
    return {
        "issuing_company": "JBSS", "type_stock": "ON", "corporate_action": "DIVIDENDO",
        "value_cash": valor, "last_date_prior_ex": data_com, "quoted_per_shares": "1",
    }


def test_montar_proventos_yahoo_vira_dividendo_do_bdr_pela_mesma_conversao() -> None:
    # JBSS3 R$ 1,00 por acao = JBSS32 R$ 2,00 (fator 0,5), como no provento da B3.
    (linha,) = B.montar_proventos([], [], [], proventos_yahoo=[_linha_yahoo(valor=1.0)])
    assert linha["ticker"] == "JBSS32"
    assert linha["tipo"] == "DIVIDENDO"
    assert linha["data_com"] == dt.date(2024, 5, 1)
    assert linha["valor_por_acao"] == pytest.approx(2.0)
    assert linha["data_pagamento"] is None


def test_montar_proventos_yahoo_nao_duplica_o_que_a_b3_ja_tem() -> None:
    linhas = B.montar_proventos(
        [_prov_b3_jbs(dt.date(2024, 5, 1), 1.0)], [], [],
        proventos_yahoo=[_linha_yahoo(data_com=dt.date(2024, 5, 1), valor=9.0)],
    )
    assert len(linhas) == 1
    assert linhas[0]["valor_por_acao"] == pytest.approx(2.0)  # vale o da B3


def test_montar_proventos_yahoo_fora_do_periodo_do_emissor_nao_entra() -> None:
    # Depois de 2025-06-08 o JBSS3 nao e mais o papel negociado da JBS S.A.
    linhas = B.montar_proventos(
        [], [], [], proventos_yahoo=[_linha_yahoo(data_com=dt.date(2025, 6, 9))]
    )
    assert linhas == []
