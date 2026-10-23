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
    juiza_preco_entrada_usd_mtok=0.25,
    juiza_preco_saida_usd_mtok=1.50,
    jev_preco_entrada_usd_mtok=0.042,
    juiza_orcamento_mensal_usd=3.0,
    juiza_backlog_verba_usd=15.0,
    daily_tz="America/Sao_Paulo",
)


def test_custo_gemini_um_milhao_de_entrada_custa_o_preco_da_tabela() -> None:
    assert juiza_custo.custo_gemini(1_000_000, 0, 0, PRECOS) == pytest.approx(0.25)


def test_custo_gemini_pensamento_e_cobrado_como_saida() -> None:
    assert juiza_custo.custo_gemini(0, 400_000, 600_000, PRECOS) == pytest.approx(1.5)


def test_custo_gemini_none_conta_zero() -> None:
    assert juiza_custo.custo_gemini(None, None, None, PRECOS) == 0.0


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
