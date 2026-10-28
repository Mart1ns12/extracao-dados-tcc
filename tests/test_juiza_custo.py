"""Custo em US$ e tetos da juiza (llm/juiza_custo.py). Nada toca banco nem rede.

`gasto_usd` e trocado por valores falsos, ou a sessao do banco e substituida por uma falsa
que so captura o SQL.
"""
from __future__ import annotations

import contextlib
import datetime as dt
from types import SimpleNamespace

import pytest

from tcc_scraper.llm import juiza_custo

PRECOS = SimpleNamespace(
    juiza_provedor="gemini",
    juiza_preco_entrada_usd_mtok=0.25,
    juiza_preco_saida_usd_mtok=1.50,
    jev_preco_entrada_usd_mtok=0.042,
    juiza_orcamento_mensal_usd=3.0,
    juiza_backlog_verba_usd=15.0,
    daily_tz="America/Sao_Paulo",
)


def test_custo_llm_gemini_um_milhao_de_entrada_custa_o_preco_da_tabela() -> None:
    assert juiza_custo.custo_llm(1_000_000, 0, s=PRECOS) == pytest.approx(0.25)


def test_custo_llm_gemini_pensamento_e_cobrado_como_saida() -> None:
    assert juiza_custo.custo_llm(0, 400_000, tokens_pensamento=600_000,
                                 s=PRECOS) == pytest.approx(1.5)


def test_custo_llm_gemini_none_conta_zero() -> None:
    assert juiza_custo.custo_llm(None, None, s=PRECOS) == 0.0


def test_custo_jev_so_cobra_a_entrada() -> None:
    assert juiza_custo.custo_jev(1_000_000, PRECOS) == pytest.approx(0.042)
    assert juiza_custo.custo_jev(None, PRECOS) == 0.0


def test_inicio_do_mes_considera_o_fuso_local() -> None:
    # 2026-10-01 01:00 UTC ainda e 30/09 22:00 em Sao Paulo: o mes local e setembro.
    agora = dt.datetime(2026, 10, 1, 1, 0, tzinfo=dt.UTC)
    assert juiza_custo.inicio_mes_utc(agora, "America/Sao_Paulo") == dt.datetime(
        2026, 9, 1, 3, 0, tzinfo=dt.UTC)


def test_teto_diario_desconta_o_gasto_de_producao_do_mes(monkeypatch) -> None:
    chamadas: list[tuple] = []

    def falso(finalidade, desde=None):
        chamadas.append((finalidade, desde))
        return 1.2

    monkeypatch.setattr(juiza_custo, "gasto_usd", falso)

    assert juiza_custo.teto_usd("diario", None, PRECOS) == pytest.approx(1.8)
    assert chamadas[0][0] == "producao"
    assert chamadas[0][1] is not None, "o diario so soma o gasto do mes corrente"


def test_teto_backlog_usa_a_verba_menos_o_gasto_do_backlog(monkeypatch) -> None:
    def falso(finalidade, desde=None):
        return 14.0 if finalidade == "backlog" else 99.0

    monkeypatch.setattr(juiza_custo, "gasto_usd", falso)

    assert juiza_custo.teto_usd("backlog", None, PRECOS) == pytest.approx(1.0)


def test_orcamento_informado_vale_quando_e_menor(monkeypatch) -> None:
    monkeypatch.setattr(juiza_custo, "gasto_usd", lambda finalidade, desde=None: 0.0)

    assert juiza_custo.teto_usd("diario", 0.5, PRECOS) == pytest.approx(0.5)
    assert juiza_custo.teto_usd("backlog", 20.0, PRECOS) == pytest.approx(15.0)


def test_teto_nunca_fica_negativo(monkeypatch) -> None:
    monkeypatch.setattr(juiza_custo, "gasto_usd", lambda finalidade, desde=None: 5.0)

    assert juiza_custo.teto_usd("diario", None, PRECOS) == 0.0


def test_modo_de_teto_desconhecido_levanta() -> None:
    with pytest.raises(ValueError):
        juiza_custo.teto_usd("outro", None, PRECOS)


def test_gasto_usd_filtra_pela_finalidade(monkeypatch) -> None:
    sql: list[str] = []

    class SessaoFalsa:
        def execute(self, consulta):
            sql.append(str(consulta.compile(compile_kwargs={"literal_binds": True})))
            return SimpleNamespace(scalar=lambda: 2.5)

    @contextlib.contextmanager
    def scope():
        yield SessaoFalsa()

    monkeypatch.setattr(juiza_custo, "session_scope", scope)

    assert juiza_custo.gasto_usd("producao") == 2.5
    assert "'producao'" in sql[0] and "custo_usd" in sql[0]


OPENAI_SEM_OVERRIDE = SimpleNamespace(
    juiza_provedor="openai",
    juiza_preco_entrada_usd_mtok=None,
    juiza_preco_cache_usd_mtok=None,
    juiza_preco_saida_usd_mtok=None,
)


def test_openai_sem_override_usa_a_tabela_de_entrada_saida_e_cache() -> None:
    assert juiza_custo.custo_llm(1_000_000, 0, s=OPENAI_SEM_OVERRIDE) == pytest.approx(0.20)
    assert juiza_custo.custo_llm(0, 1_000_000, s=OPENAI_SEM_OVERRIDE) == pytest.approx(1.20)
    assert juiza_custo.custo_llm(1_000_000, 0, tokens_cache=1_000_000,
                                 s=OPENAI_SEM_OVERRIDE) == pytest.approx(0.02)


def test_override_do_env_vence_a_tabela() -> None:
    s = SimpleNamespace(**{**OPENAI_SEM_OVERRIDE.__dict__, "juiza_preco_saida_usd_mtok": 2.0})

    assert juiza_custo.precos_llm(s) == (0.20, 0.02, 2.0)
    assert juiza_custo.custo_llm(0, 1_000_000, s=s) == pytest.approx(2.0)


def test_custo_resposta_ignora_raciocinio_da_openai() -> None:
    resposta = SimpleNamespace(tokens_entrada=0, tokens_saida=0, tokens_raciocinio=500_000)

    assert juiza_custo.custo_resposta(resposta, OPENAI_SEM_OVERRIDE) == 0.0


def test_cache_maior_que_a_entrada_e_limitado() -> None:
    # 1M de entrada com 3M de cache: so 1M e cobrado, a preco de cache.
    custo = juiza_custo.custo_llm(1_000_000, 0, tokens_cache=3_000_000, s=OPENAI_SEM_OVERRIDE)

    assert custo == pytest.approx(0.02)


ANTHROPIC_SEM_OVERRIDE = SimpleNamespace(
    juiza_provedor="anthropic",
    juiza_preco_entrada_usd_mtok=None,
    juiza_preco_cache_usd_mtok=None,
    juiza_preco_saida_usd_mtok=None,
)


def test_anthropic_sem_override_usa_a_tabela_haiku_5_5() -> None:
    # Haiku 5.5, prompt ate 100K: entrada 0.10, cache 0.01, saida 0.50 US$/MTok.
    assert juiza_custo.precos_llm(ANTHROPIC_SEM_OVERRIDE) == (0.10, 0.01, 0.50)
    assert juiza_custo.custo_llm(1_000_000, 0, s=ANTHROPIC_SEM_OVERRIDE) == pytest.approx(0.10)
    assert juiza_custo.custo_llm(0, 1_000_000, s=ANTHROPIC_SEM_OVERRIDE) == pytest.approx(0.50)
    assert juiza_custo.custo_llm(1_000_000, 0, tokens_cache=1_000_000,
                                 s=ANTHROPIC_SEM_OVERRIDE) == pytest.approx(0.01)


def test_anthropic_nao_cobra_pensamento_duas_vezes() -> None:
    # Pensamento ja esta em output_tokens: tokens_pensamento=None, saida conta uma vez so.
    resposta = SimpleNamespace(tokens_entrada=0, tokens_saida=400_000,
                               tokens_cache=None, tokens_pensamento=None)

    assert juiza_custo.custo_resposta(resposta, ANTHROPIC_SEM_OVERRIDE) == pytest.approx(0.20)


def test_gemini_soma_o_pensamento_como_saida() -> None:
    s = SimpleNamespace(juiza_provedor="gemini", juiza_preco_entrada_usd_mtok=None,
                        juiza_preco_cache_usd_mtok=None, juiza_preco_saida_usd_mtok=None)
    resposta = SimpleNamespace(tokens_entrada=0, tokens_saida=400_000,
                               tokens_pensamento=600_000)

    assert juiza_custo.custo_resposta(resposta, s) == pytest.approx(1.5)
