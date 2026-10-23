"""Coletor de IDIV, SMLL e IFIX pela B3 contra as fixtures reais (2022). Sem rede e sem banco."""

from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path

import pytest

from tcc_scraper.collectors.b3_base import url_b3
from tcc_scraper.collectors.b3_indices import (
    INDEX_BASE,
    B3IndicesCollector,
    montar_raw_prices,
    parse_portfolio_day,
)
from tcc_scraper.collectors.base import WorkUnit
from tcc_scraper.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
ANO = 2022
INICIO = dt.date(ANO, 1, 1)
FIM = dt.date(ANO, 12, 31)


def _carregar(nome: str) -> dict:
    return json.loads((FIXTURES / nome).read_text(encoding="utf-8"))


def _parse(payload: dict, *, ano: int = ANO, inicio: dt.date = INICIO, fim: dt.date = FIM):
    return parse_portfolio_day(payload, ticker="IDIV", ano=ano, inicio=inicio, fim=fim)


def _por_data(linhas: list[dict]) -> dict[dt.date, float]:
    return {r["date"]: r["close"] for r in linhas}


@pytest.fixture(scope="module")
def idiv_2022() -> dict:
    return _carregar("b3_indices_idiv_2022.json")


def test_formato_ptbr_e_mes_da_coluna_certos(idiv_2022: dict) -> None:
    # "6.831,31" e o fechamento do dia 1 na coluna rateValue2 = fevereiro.
    por_data = _por_data(_parse(idiv_2022))

    assert por_data[dt.date(2022, 2, 1)] == pytest.approx(6831.31)
    assert por_data[dt.date(2022, 4, 1)] == pytest.approx(7429.76)
    assert por_data[dt.date(2022, 12, 1)] == pytest.approx(7195.05)


def test_null_nao_vira_linha_e_a_contagem_bate_com_a_fixture(idiv_2022: dict) -> None:
    linhas = _parse(idiv_2022)
    nao_nulos = sum(
        1
        for registro in idiv_2022["results"]
        for chave, valor in registro.items()
        if chave.startswith("rateValue") and valor is not None
    )

    assert len(linhas) == nao_nulos == 250
    assert all(isinstance(r["close"], float) for r in linhas)
    # 1 de janeiro e feriado: rateValue1 do dia 1 vem null e nao pode virar linha.
    assert dt.date(2022, 1, 1) not in _por_data(linhas)


def test_dia_mes_que_nao_existe_e_ignorado() -> None:
    payload = {
        "results": [
            {"day": 31, "rateValue2": "1,00", "rateValue4": "2,00"},  # 31/02 e 31/04
            {"day": 29, "rateValue2": "3,00"},  # 29/02/2022 nao existe
            {"day": 30, "rateValue2": "4,00"},  # 30/02/2022 nao existe
        ]
    }

    assert _parse(payload) == []


def test_mesmo_dia_mes_bissexto_e_valido() -> None:
    payload = {"results": [{"day": 29, "rateValue2": "3,00"}]}

    linhas = _parse(payload, ano=2024, inicio=dt.date(2024, 1, 1), fim=dt.date(2024, 12, 31))

    assert _por_data(linhas) == {dt.date(2024, 2, 29): 3.0}


def test_futuras_e_fora_do_intervalo_sao_ignoradas(idiv_2022: dict) -> None:
    # fim = hoje na coleta diaria: nada depois dele pode entrar.
    linhas = _parse(idiv_2022, fim=dt.date(2022, 6, 30))

    assert linhas
    assert max(r["date"] for r in linhas) <= dt.date(2022, 6, 30)
    assert {r["date"].month for r in linhas} == {1, 2, 3, 4, 5, 6}


def test_inicio_do_intervalo_corta_o_inicio_do_ano(idiv_2022: dict) -> None:
    linhas = _parse(idiv_2022, inicio=dt.date(2022, 3, 1))

    assert min(r["date"] for r in linhas) >= dt.date(2022, 3, 1)


def test_valor_vazio_ou_nao_numerico_e_ignorado() -> None:
    payload = {"results": [{"day": 5, "rateValue1": "", "rateValue2": "abc", "rateValue3": "7,5"}]}

    assert _por_data(_parse(payload)) == {dt.date(2022, 3, 5): 7.5}


def test_min_e_max_nao_entram_no_resultado() -> None:
    payload = {
        "min": {"day": 0, "rateValue1": "9,00"},
        "max": {"day": 0, "rateValue1": "9,00"},
        "results": [],
    }

    assert _parse(payload) == []


def test_montar_raw_prices_so_grava_o_fechamento() -> None:
    linhas = [{"ticker": "IDIV", "date": dt.date(2022, 2, 1), "close": 6831.31}]

    assert montar_raw_prices(linhas) == [
        {
            "ticker": "IDIV",
            "date": dt.date(2022, 2, 1),
            "open": None,
            "high": None,
            "low": None,
            "close": 6831.31,
            "adj_close": 6831.31,
            "volume": None,
            "source": "b3",
        }
    ]


def test_unidades_cobrem_os_tres_indices_por_ano() -> None:
    with B3IndicesCollector(anos=[2021, 2022]) as c:
        chaves = [u.key for u in c.work_units()]

    assert chaves == [
        "b3|IDIV|2021", "b3|IDIV|2022",
        "b3|SMLL|2021", "b3|SMLL|2022",
        "b3|IFIX|2021", "b3|IFIX|2022",
    ]


def test_url_usa_o_proxy_de_indices_e_o_parametro_year() -> None:
    params = {"index": "IDIV", "language": "pt-br", "year": "2022"}
    url = url_b3("GetPortfolioDay", params, INDEX_BASE)

    assert url.startswith(f"{INDEX_BASE}/GetPortfolioDay/")
    codigo = url.rsplit("/", 1)[1]
    assert json.loads(base64.b64decode(codigo)) == {
        "index": "IDIV", "language": "pt-br", "year": "2022"
    }


def test_process_chama_a_b3_e_passa_as_linhas_ao_persist(monkeypatch) -> None:
    chamadas: list[tuple[str, dict, dict]] = []
    gravadas: list[dict] = []
    fixture = _carregar("b3_indices_idiv_2022.json")

    def get_json_falso(metodo, params, base=None):
        chamadas.append((metodo, params, {"base": base}))
        return fixture

    with B3IndicesCollector(anos=[ANO]) as c:
        monkeypatch.setattr(
            c, "settings",
            Settings(gemini_api_key="", data_start=INICIO, data_fim_coleta=FIM),
        )
        monkeypatch.setattr(c, "get_json", get_json_falso)
        monkeypatch.setattr(c, "_persist", lambda linhas: gravadas.extend(linhas))

        resultado = c.process(WorkUnit(key="b3|IDIV|2022", payload={"indice": "IDIV", "ano": ANO}))

    assert chamadas == [
        ("GetPortfolioDay",
         {"index": "IDIV", "language": "pt-br", "year": "2022"},
         {"base": INDEX_BASE}),
    ]
    assert resultado.items_found == 250
    assert all(r["ticker"] == "IDIV" for r in gravadas)


def test_process_sem_json_levanta_para_marcar_erro(monkeypatch) -> None:
    with B3IndicesCollector(anos=[ANO]) as c:
        monkeypatch.setattr(
            c, "settings",
            Settings(gemini_api_key="", data_start=INICIO, data_fim_coleta=FIM),
        )
        monkeypatch.setattr(c, "get_json", lambda metodo, params, base=None: None)

        with pytest.raises(RuntimeError):
            c.process(WorkUnit(key="b3|SMLL|2022", payload={"indice": "SMLL", "ano": ANO}))
