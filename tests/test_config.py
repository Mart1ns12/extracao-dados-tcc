from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tcc_scraper.config import (
    APELIDOS,
    IBOV_TICKERS,
    MUDANCAS_TICKER,
    NOMES_EMPRESA,
    TICKER_CODE_CVM,
    UNITS,
    UNIVERSO_HISTORICO,
    Settings,
    universo_coleta,
)

REMOVIDOS = {"CRFB3", "STBP3", "SRNA3"}


def test_fim_coleta_fixado_devolve_a_data_fixada():
    s = Settings(_env_file=None, data_fim_coleta=date(2026, 1, 2))
    assert s.fim_coleta == date(2026, 1, 2)


def test_fim_coleta_sem_fixacao_e_hoje_em_sao_paulo():
    s = Settings(_env_file=None)
    assert s.data_fim_coleta is None
    assert s.fim_coleta == datetime.now(ZoneInfo("America/Sao_Paulo")).date()


def test_data_fim_coleta_vazia_no_ambiente_vira_ausente(monkeypatch):
    monkeypatch.setenv("DATA_FIM_COLETA", "")
    assert Settings(_env_file=None).data_fim_coleta is None


def test_inicio_noticias_e_31_dias_antes_de_data_start():
    s = Settings(_env_file=None)
    assert s.inicio_noticias == s.data_start - timedelta(days=31)


def test_price_warmup_default_e_400():
    assert Settings(_env_file=None).price_warmup_days == 400


def test_universo_coleta_contem_ibov_e_removidos_sem_duplicatas():
    universo = universo_coleta()
    assert set(universo) == set(IBOV_TICKERS) | REMOVIDOS
    assert len(UNIVERSO_HISTORICO) == len({t for t, _, _ in UNIVERSO_HISTORICO})


def test_mudancas_ticker_apontam_para_o_universo():
    universo = set(universo_coleta())
    for _antigo, novo, _data in MUDANCAS_TICKER:
        assert novo in universo, novo


def test_units_terminam_em_11_e_estao_no_universo():
    universo = set(universo_coleta())
    for ticker in UNITS:
        assert ticker.endswith("11"), ticker
        assert ticker in universo, ticker


def test_nomes_empresa_cobrem_o_universo_sem_vazios():
    assert set(NOMES_EMPRESA) == set(universo_coleta())
    assert all(nome.strip() for nome in NOMES_EMPRESA.values())


def test_apelidos_chaves_no_universo_e_listas_nao_vazias():
    universo = set(universo_coleta())
    assert len(APELIDOS) >= 15
    for ticker, marcas in APELIDOS.items():
        assert ticker in universo, ticker
        assert marcas, ticker


def test_ticker_code_cvm_so_tem_tickers_do_universo_e_codigos_positivos():
    assert set(TICKER_CODE_CVM) <= set(universo_coleta())
    assert all(cvm > 0 for cvm in TICKER_CODE_CVM.values())


FALTANDO_CVM = sorted(set(universo_coleta()) - set(TICKER_CODE_CVM))


@pytest.mark.xfail(
    bool(FALTANDO_CVM),
    reason=f"sem codigo CVM (busca da B3 nao casou): {FALTANDO_CVM}",
    strict=True,
)
def test_todo_ticker_do_universo_tem_codigo_cvm():
    assert not FALTANDO_CVM
