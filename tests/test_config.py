from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tcc_scraper.config import (
    APELIDOS,
    CD_CVM_BANCOS_SEGURADORAS,
    CONTAS_CVM,
    DOMINIOS_WP,
    IBOV_SECTORS,
    IBOV_TICKERS,
    MUDANCAS_TICKER,
    NOMES_EMPRESA,
    TICKER_CODE_CVM,
    UNITS,
    UNIVERSO_HISTORICO,
    WP_SITES,
    Settings,
    conta_ajuste_dfc,
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


def test_mudancas_ticker_tem_data():
    for antigo, _novo, data in MUDANCAS_TICKER:
        assert data is not None, antigo


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


def test_codigos_de_bancos_e_seguradoras_existem_em_ticker_code_cvm():
    assert set(TICKER_CODE_CVM.values()) >= CD_CVM_BANCOS_SEGURADORAS


def test_todo_seguro_do_ibov_esta_na_lista_de_bancos_e_seguradoras():
    for ticker, setor in IBOV_SECTORS.items():
        if setor == "seguros":
            assert TICKER_CODE_CVM.get(ticker) in CD_CVM_BANCOS_SEGURADORAS, ticker


def test_todo_banco_do_ibov_esta_na_lista_exceto_b3_e_itausa():
    for ticker, setor in IBOV_SECTORS.items():
        if setor == "bancos_financeiro" and ticker not in {"B3SA3", "ITSA4"}:
            cd = TICKER_CODE_CVM.get(ticker)
            if cd is None:
                continue
            assert cd in CD_CVM_BANCOS_SEGURADORAS, ticker


def test_conta_ajuste_dfc_so_aceita_o_quarto_nivel_de_6_01_01():
    assert conta_ajuste_dfc("6.01.01.04")
    for cd in ("6.01.01", "6.01.01.04.01", "6.01.02.01"):
        assert not conta_ajuste_dfc(cd), cd


def test_contas_cvm_tem_bpa_e_os_quatro_codigos_de_emprestimo():
    assert CONTAS_CVM["BPA"] == {"1.01.01": "caixa", "1.01.02": "aplicacoes_financeiras"}
    emprestimos = {"2.01.04.01", "2.01.04.02", "2.02.01.01", "2.02.01.02"}
    assert emprestimos <= set(CONTAS_CVM["BPP"])


# Juiza: modo do Jev, modelo e chave por provedor. Limpa o ambiente para nao herdar
# variaveis da maquina; _env_file=None impede a leitura do .env.
VARS_JUIZA = (
    "JUIZA_JEV_MODO",
    "JUIZA_JEV_DECIDE",
    "JUIZA_MODELO",
    "JUIZA_PROVEDOR",
    "JUIZA_JEV_LIMIAR_ENTREGA",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)


@pytest.fixture
def ambiente_juiza_limpo(monkeypatch):
    for nome in VARS_JUIZA:
        monkeypatch.delenv(nome, raising=False)


def test_jev_modo_padrao_e_entrega(ambiente_juiza_limpo):
    assert Settings(_env_file=None).jev_modo_efetivo == "entrega"


def test_jev_decide_sem_modo_vira_decide(ambiente_juiza_limpo):
    assert Settings(_env_file=None, juiza_jev_decide=True).jev_modo_efetivo == "decide"


def test_jev_decide_nao_sobrescreve_modo_explicito(ambiente_juiza_limpo):
    s = Settings(_env_file=None, juiza_jev_decide=True, juiza_jev_modo="triagem")
    assert s.jev_modo_efetivo == "triagem"


def test_jev_decide_pelo_ambiente_vira_decide(ambiente_juiza_limpo, monkeypatch):
    monkeypatch.setenv("JUIZA_JEV_DECIDE", "true")
    assert Settings(_env_file=None).jev_modo_efetivo == "decide"


def test_modelo_efetivo_segue_o_provedor(ambiente_juiza_limpo):
    openai = Settings(_env_file=None, juiza_provedor="openai")
    gemini = Settings(_env_file=None, juiza_provedor="gemini")
    assert openai.juiza_modelo_efetivo == "gpt-5.6-luna"
    assert gemini.juiza_modelo_efetivo == "gemini-3.1-flash-lite"


def test_modelo_explicito_prevalece_sobre_o_padrao(ambiente_juiza_limpo):
    assert Settings(_env_file=None, juiza_modelo="x").juiza_modelo_efetivo == "x"


def test_chave_llm_segue_o_provedor(ambiente_juiza_limpo):
    kwargs = {"openai_api_key": "sk-1", "gemini_api_key": "g-1", "anthropic_api_key": "a-1"}
    openai = Settings(_env_file=None, juiza_provedor="openai", **kwargs)
    gemini = Settings(_env_file=None, juiza_provedor="gemini", **kwargs)
    anthropic = Settings(_env_file=None, juiza_provedor="anthropic", **kwargs)
    assert openai.juiza_chave_llm == "sk-1"
    assert gemini.juiza_chave_llm == "g-1"
    assert anthropic.juiza_chave_llm == "a-1"


def test_provedor_padrao_da_juiza_e_anthropic(ambiente_juiza_limpo):
    s = Settings(_env_file=None)
    assert s.juiza_provedor == "anthropic"
    assert s.juiza_modelo_efetivo == "claude-haiku-5-5"


def test_anthropic_tem_os_parametros_padrao(ambiente_juiza_limpo):
    s = Settings(_env_file=None)
    assert s.anthropic_timeout_s == 120.0
    assert s.anthropic_max_attempts == 3
    assert s.anthropic_effort == "medium"
    assert s.anthropic_max_output_tokens == 16000


def test_limiar_entrega_fora_da_faixa_e_rejeitado(ambiente_juiza_limpo):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, juiza_jev_limiar_entrega=0.5)


def test_wp_sites_tem_os_tres_veiculos_com_endpoint():
    endpoints = {s.nome: s.endpoint for s in WP_SITES}
    assert set(endpoints) == {"infomoney", "moneytimes", "exame"}
    assert all(e.startswith("https://") and "/wp-json/wp/v2/posts" in e for e in endpoints.values())


def test_wp_endpoint_exame_termina_com_barra():
    exame = next(s for s in WP_SITES if s.nome == "exame")
    assert exame.endpoint.endswith("/posts/")


def test_dominios_wp_sao_os_tres_veiculos():
    assert {"infomoney.com.br", "moneytimes.com.br", "exame.com"} == DOMINIOS_WP
