"""Template e schema enviados ao Gemini (sem rede)."""
from __future__ import annotations

from datetime import date

from tcc_scraper.config import IBOV_SECTORS, IBOV_TICKERS
from tcc_scraper.llm.prompt import VERSAO, renderizar_prompt
from tcc_scraper.llm.schema import AnaliseDiaria, Setor, schema_para_gemini


def _sem_title_nem_null(node) -> bool:
    if isinstance(node, dict):
        if "title" in node:
            return False
        if node.get("type") == "null":
            return False
        return all(_sem_title_nem_null(v) for v in node.values())
    if isinstance(node, list):
        return all(_sem_title_nem_null(v) for v in node)
    return True


def test_prompt_tem_tickers_data_e_sem_dollar():
    r = renderizar_prompt(date(2026, 10, 14))
    for ticker in IBOV_TICKERS:
        assert ticker in r.texto, ticker
    assert "2026-10-14" in r.texto
    assert "$" not in r.texto


def test_sha256_estavel_entre_datas_e_versao_3():
    r1 = renderizar_prompt(date(2026, 10, 14))
    r2 = renderizar_prompt(date(2026, 10, 15))
    assert r1.sha256 == r2.sha256
    assert len(r1.sha256) == 64
    assert VERSAO == 3


def test_setores_do_prompt_sao_validos():
    assert set(IBOV_SECTORS) == set(IBOV_TICKERS)
    assert set(IBOV_SECTORS.values()) <= {s.value for s in Setor}


def test_schema_gemini_sem_title_nem_null():
    schema = schema_para_gemini(AnaliseDiaria)
    assert _sem_title_nem_null(schema)
    assert schema["properties"]["data_referencia"]  # schema real, nao vazio
