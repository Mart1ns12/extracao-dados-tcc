"""Comandos Typer dos coletores novos e do validate-cadastro, com coletores e banco falsos."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from tcc_scraper.cli import app

runner = CliRunner()

STATS = {"total": 1, "ok": 1, "erro": 0, "itens": 3, "saturadas": 0}


def _falso(nome: str, registro: list):
    """Classe que registra o construtor e o run, e devolve estatisticas fixas."""

    class Falso:
        def __init__(self, *args, **kwargs) -> None:
            registro.append(("init", nome, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> None:
            return None

        def run(self, *, resume=True, limit=None, apenas_erros=False, observador=None):
            registro.append(("run", nome, resume))
            return dict(STATS)

    return Falso


@pytest.fixture
def registro(monkeypatch) -> list:
    """Troca os coletores reais dos comandos novos por falsos que anotam a chamada."""
    reg: list = []
    alvos = {
        "tcc_scraper.collectors.cvm_demonstracoes.CvmDemonstracoesCollector": "cvm_itr_dfp",
        "tcc_scraper.collectors.cvm_ipe.CvmIpeCollector": "cvm_ipe",
        "tcc_scraper.collectors.cvm_cadastro.CvmCadastroCollector": "cvm_cadastro",
        "tcc_scraper.collectors.b3_cadastro.B3CadastroCollector": "b3_cadastro",
        "tcc_scraper.collectors.b3_proventos.B3ProventosCollector": "b3_proventos",
        "tcc_scraper.collectors.indices.IndicesBcbCollector": "indices_bcb",
    }
    for alvo, nome in alvos.items():
        monkeypatch.setattr(alvo, _falso(nome, reg))
    return reg


def test_collect_cvm_repassa_anos_e_no_resume_e_imprime_relatorio(registro) -> None:
    r = runner.invoke(app, ["collect-cvm", "--anos", "2025", "--anos", "2026", "--no-resume"])

    assert r.exit_code == 0, r.output
    assert ("init", "cvm_itr_dfp", {"anos": [2025, 2026]}) in registro
    assert ("run", "cvm_itr_dfp", False) in registro
    assert "ITR/DFP" in r.output


def test_collect_cvm_sem_anos_usa_janela_padrao(registro) -> None:
    r = runner.invoke(app, ["collect-cvm"])

    assert r.exit_code == 0, r.output
    assert ("init", "cvm_itr_dfp", {"anos": None}) in registro
    assert ("run", "cvm_itr_dfp", True) in registro


def test_collect_ipe_roda_o_coletor_com_anos(registro) -> None:
    r = runner.invoke(app, ["collect-ipe", "--anos", "2026"])

    assert r.exit_code == 0, r.output
    assert ("init", "cvm_ipe", {"anos": [2026]}) in registro
    assert "fatos relevantes" in r.output


def test_collect_cadastro_roda_cvm_e_b3_e_imprime_dois_relatorios(registro) -> None:
    r = runner.invoke(app, ["collect-cadastro", "--no-resume"])

    assert r.exit_code == 0, r.output
    assert ("run", "cvm_cadastro", False) in registro
    assert ("run", "b3_cadastro", False) in registro
    assert registro.index(("run", "cvm_cadastro", False)) < registro.index(
        ("run", "b3_cadastro", False)
    )
    assert "cadastro (CVM)" in r.output
    assert "cadastro (B3)" in r.output


def test_collect_proventos_roda_o_coletor(registro) -> None:
    r = runner.invoke(app, ["collect-proventos"])

    assert r.exit_code == 0, r.output
    assert ("run", "b3_proventos", True) in registro
    assert "proventos (B3)" in r.output


def test_collect_indices_repassa_anos(registro) -> None:
    r = runner.invoke(app, ["collect-indices", "--anos", "2025"])

    assert r.exit_code == 0, r.output
    assert ("init", "indices_bcb", {"anos": [2025]}) in registro
    assert "indices" in r.output


# -- validate-cadastro ---------------------------------------------------------------
class _Resultado:
    def __init__(self, linhas: list) -> None:
        self._linhas = linhas

    def all(self) -> list:
        return self._linhas


class _Sessao:
    def __init__(self, linhas: list) -> None:
        self._linhas = linhas

    def execute(self, _stmt) -> _Resultado:
        return _Resultado(self._linhas)


def _banco_falso(monkeypatch, linhas: list) -> None:
    """linhas: (cd_cvm, other_codes) de raw_b3_cadastro."""

    @contextmanager
    def falso():
        yield _Sessao(linhas)

    monkeypatch.setattr("tcc_scraper.db.session.session_scope", falso)


def _universo(monkeypatch, universo: list[str], codigos: dict[str, int]) -> None:
    monkeypatch.setattr("tcc_scraper.config.universo_coleta", lambda: universo)
    monkeypatch.setattr("tcc_scraper.config.TICKER_CODE_CVM", codigos)


def test_validate_cadastro_sai_zero_quando_todos_os_tickers_batem(monkeypatch) -> None:
    _universo(monkeypatch, ["AAAA3", "BBBB3"], {"AAAA3": 1, "BBBB3": 2})
    _banco_falso(monkeypatch, [(1, ["AAAA3", "AAAA4"]), (2, ["BBBB3"])])

    r = runner.invoke(app, ["validate-cadastro"])

    assert r.exit_code == 0, r.output
    assert "validos: 2" in r.output
    assert "falhas: 0" in r.output


def test_validate_cadastro_sai_um_quando_falta_codigo_ou_ticker(monkeypatch) -> None:
    # ZZZZ3 nao tem codigo CVM; CCCC3 tem codigo, mas nao aparece em other_codes desse codigo.
    _universo(
        monkeypatch,
        ["AAAA3", "CCCC3", "ZZZZ3"],
        {"AAAA3": 1, "CCCC3": 3},
    )
    _banco_falso(monkeypatch, [(1, ["AAAA3"]), (3, ["XXXX3"])])

    r = runner.invoke(app, ["validate-cadastro"])

    assert r.exit_code == 1
    assert "FALHA ZZZZ3" in r.output
    assert "FALHA CCCC3" in r.output
    assert "ok    AAAA3" in r.output
    assert "falhas: 2" in r.output
