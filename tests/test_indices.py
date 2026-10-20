"""Testes do coletor de CDI e IPCA (SGS 12 e 433) com fixtures, sem rede e sem banco."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from tcc_scraper.collectors import indices as indices_mod
from tcc_scraper.collectors.indices import IndicesBcbCollector
from tcc_scraper.collectors.macro_bcb import parse_sgs
from tcc_scraper.config import get_settings
from tcc_scraper.db.models import Macro

FIXTURES = Path(__file__).parent / "fixtures"


def _carregar(nome: str) -> list[dict]:
    return json.loads((FIXTURES / nome).read_text(encoding="utf-8"))


class _RespostaFalsa:
    def __init__(self, payload: list[dict]) -> None:
        self._payload = payload

    def json(self) -> list[dict]:
        return self._payload


@contextmanager
def _sessao_falsa():
    yield None


@pytest.fixture
def coletor():
    c = IndicesBcbCollector(anos=[2026])
    yield c
    c.close()


def test_parse_sgs_cdi_devolve_floats_e_datas() -> None:
    rows = parse_sgs(_carregar("bcb_sgs_12_2026.json"), series="cdi")

    assert rows
    assert all(r.series == "cdi" for r in rows)
    assert all(isinstance(r.date, dt.date) for r in rows)
    assert all(isinstance(r.value, float) for r in rows)


def test_work_units_uma_por_serie_e_ano() -> None:
    c = IndicesBcbCollector(anos=[2025, 2026])
    try:
        chaves = [u.key for u in c.work_units()]
    finally:
        c.close()

    assert len(chaves) == 4
    assert set(chaves) == {
        "sgs|cdi|2025",
        "sgs|cdi|2026",
        "sgs|ipca|2025",
        "sgs|ipca|2026",
    }


def test_process_ipca_normaliza_data_para_dia_1(coletor, monkeypatch) -> None:
    unidade = next(u for u in coletor.work_units() if u.key == "sgs|ipca|2026")
    resposta = _RespostaFalsa(_carregar("bcb_sgs_433_2026.json"))
    monkeypatch.setattr(coletor, "fetch", lambda url, **kw: resposta)
    capturadas: list[list] = []
    monkeypatch.setattr(coletor, "_persist", lambda rows: capturadas.append(rows))

    coletor.process(unidade)

    rows = capturadas[0]
    assert rows
    assert all(r.series == "ipca" for r in rows)
    assert all(r.date.day == 1 for r in rows)


def test_janela_do_ano_corrente_termina_em_fim_coleta(coletor, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "data_start", dt.date(2026, 1, 1))
    monkeypatch.setattr(settings, "data_fim_coleta", dt.date(2026, 10, 14))
    params_vistos: list[dict] = []

    def fetch_falso(url, params=None, **kw):
        params_vistos.append(params)
        return _RespostaFalsa(_carregar("bcb_sgs_12_2026.json"))

    monkeypatch.setattr(coletor, "fetch", fetch_falso)
    monkeypatch.setattr(coletor, "_persist", lambda rows: None)

    unidade = next(u for u in coletor.work_units() if u.key == "sgs|cdi|2026")
    coletor.process(unidade)

    assert params_vistos[0]["dataInicial"] == "01/01/2026"
    assert params_vistos[0]["dataFinal"] == "14/10/2026"


def test_janela_do_ano_inicial_comeca_em_price_start(monkeypatch) -> None:
    """Ano 2025 com aquecimento: a janela nao pode comecar antes de price_start (15/01/2025)."""
    settings = get_settings()
    # data_start = 01/03/2025 e aquecimento de 45 dias -> price_start = 15/01/2025, dentro de 2025.
    monkeypatch.setattr(settings, "price_warmup_days", 45)
    monkeypatch.setattr(settings, "data_start", dt.date(2025, 3, 1))
    monkeypatch.setattr(settings, "data_fim_coleta", dt.date(2026, 10, 14))
    c = IndicesBcbCollector(anos=[2025])
    params_vistos: list[dict] = []

    def fetch_falso(url, params=None, **kw):
        params_vistos.append(params)
        return _RespostaFalsa([])

    try:
        monkeypatch.setattr(c, "fetch", fetch_falso)
        unidade = next(u for u in c.work_units() if u.key == "sgs|cdi|2025")
        c.process(unidade)
    finally:
        c.close()

    assert settings.price_start == dt.date(2025, 1, 15)
    assert params_vistos[0]["dataInicial"] == "15/01/2025"
    assert params_vistos[0]["dataFinal"] == "31/12/2025"


def test_process_grava_uma_linha_por_registro_via_bulk_upsert(coletor, monkeypatch) -> None:
    esperado = len(_carregar("bcb_sgs_12_2026.json"))
    resposta = _RespostaFalsa(_carregar("bcb_sgs_12_2026.json"))
    monkeypatch.setattr(coletor, "fetch", lambda url, **kw: resposta)
    chamadas: list[dict] = []

    def upsert_falso(session, tabela, linhas, index_elements, update_columns):
        chamadas.append(
            {
                "tabela": tabela,
                "linhas": linhas,
                "index_elements": index_elements,
                "update_columns": update_columns,
            }
        )

    monkeypatch.setattr(indices_mod, "session_scope", _sessao_falsa)
    monkeypatch.setattr(indices_mod, "bulk_upsert", upsert_falso)

    unidade = next(u for u in coletor.work_units() if u.key == "sgs|cdi|2026")
    resultado = coletor.process(unidade)

    assert resultado.items_found == esperado
    assert len(chamadas) == 1
    assert chamadas[0]["tabela"] is Macro.__table__
    assert len(chamadas[0]["linhas"]) == esperado
    assert chamadas[0]["index_elements"] == ["series", "date"]
    assert chamadas[0]["update_columns"] == ["value"]
