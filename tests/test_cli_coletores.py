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


# -- build-entrega, judge-news, historico ----------------------------------------------
def test_build_entrega_repassa_tabelas_e_csv_e_imprime_contagem(monkeypatch) -> None:
    chamadas: list = []

    def falso(**kwargs):
        chamadas.append(kwargs)
        return {"fato_entrega_a": 7}

    monkeypatch.setattr("tcc_scraper.entrega.builder.build_entrega", falso)

    r = runner.invoke(app, ["build-entrega", "--csv", "--tabela", "A", "--tabela", "B"])

    assert r.exit_code == 0, r.output
    assert chamadas == [{"tabelas": ["A", "B"], "csv": True}]
    assert "fato_entrega_a" in r.output
    assert "csv:" in r.output


def test_build_entrega_sem_flags_monta_todas_e_nao_mostra_csv(monkeypatch) -> None:
    chamadas: list = []

    def falso(**kwargs):
        chamadas.append(kwargs)
        return {"fato_entrega_a": 1}

    monkeypatch.setattr("tcc_scraper.entrega.builder.build_entrega", falso)

    r = runner.invoke(app, ["build-entrega"])

    assert r.exit_code == 0, r.output
    assert chamadas == [{"tabelas": None, "csv": False}]
    assert "csv:" not in r.output


def _juiza_falso(monkeypatch, chamadas: list) -> None:
    from tcc_scraper.llm.juiza import ResultadoJuiza

    def falso(**kwargs):
        chamadas.append(kwargs)
        return ResultadoJuiza(
            pendentes=5,
            regra=2,
            jev=1,
            llm=1,
            erros=0,
            runs=1,
            tokens_entrada=10,
            tokens_saida=5,
        )

    monkeypatch.setattr("tcc_scraper.llm.juiza.julgar_pendentes", falso)


def test_judge_news_repassa_limite_e_imprime_contagens(monkeypatch) -> None:
    chamadas: list = []
    _juiza_falso(monkeypatch, chamadas)

    r = runner.invoke(app, ["judge-news", "--limite", "50"])

    assert r.exit_code == 0, r.output
    assert chamadas == [{"limite_itens": 50}]
    assert "juiza de noticias" in r.output
    assert "pendentes" in r.output


def test_judge_news_backlog_repassa_anos_e_orcamento(monkeypatch) -> None:
    chamadas: list = []
    _juiza_falso(monkeypatch, chamadas)

    r = runner.invoke(app, [
        "judge-news", "--backlog", "--orcamento-usd", "2.5",
        "--anos", "2025", "--anos", "2026", "--limite", "100",
    ])

    assert r.exit_code == 0, r.output
    assert chamadas == [{
        "limite_itens": 100, "modo": "backlog", "anos": [2025, 2026], "orcamento_usd": 2.5,
    }]


def test_judge_news_status_mostra_fila_e_gasto_sem_julgar(monkeypatch) -> None:
    chamadas: list = []
    _juiza_falso(monkeypatch, chamadas)
    monkeypatch.setattr(
        "tcc_scraper.llm.juiza.contar_pendentes",
        lambda max_tentativas: {"pendentes": 7, "esgotados": 1, "julgados": 9},
    )
    monkeypatch.setattr(
        "tcc_scraper.llm.juiza_custo.gasto_usd",
        lambda finalidade, desde=None: 1.25 if finalidade == "backlog" else 0.5,
    )

    r = runner.invoke(app, ["judge-news", "--status"])

    assert r.exit_code == 0, r.output
    assert chamadas == []
    assert "gasto_backlog_usd" in r.output
    assert "1.25" in r.output


def _historico_falso(monkeypatch, registro: list) -> None:
    class Falso:
        def __init__(self, repo, settings=None) -> None:
            registro.append(("init", repo))

        def disparar(self, anos, modo="pendentes") -> str:
            registro.append(("disparar", anos, modo))
            return "exec-7"

        def join(self, timeout=None) -> None:
            registro.append(("join",))

    monkeypatch.setattr("tcc_scraper.orquestrador.historico.Historico", Falso)
    monkeypatch.setattr(
        "tcc_scraper.orquestrador.repositorio.RepositorioBanco",
        lambda *a, **k: "repo-falso",
    )


def test_historico_dispara_com_anos_e_modo_e_espera(monkeypatch) -> None:
    registro: list = []
    _historico_falso(monkeypatch, registro)

    r = runner.invoke(
        app, ["historico", "--anos", "2026", "--anos", "2025", "--modo", "reprocessar_erros"]
    )

    assert r.exit_code == 0, r.output
    assert ("init", "repo-falso") in registro
    assert ("disparar", [2026, 2025], "reprocessar_erros") in registro
    assert registro[-1] == ("join",)
    assert "exec-7" in r.output


def test_historico_sem_anos_recusa_sem_disparar(monkeypatch) -> None:
    registro: list = []
    _historico_falso(monkeypatch, registro)

    r = runner.invoke(app, ["historico"])

    assert r.exit_code == 2
    assert not any(c[0] == "disparar" for c in registro)


# -- validate-cadastro: deslistado sem codigo vira aviso --------------------------------
def test_validate_cadastro_deslistado_sem_codigo_vira_aviso(monkeypatch) -> None:
    _universo(monkeypatch, ["AAAA3", "CRFB3"], {"AAAA3": 1})
    _banco_falso(monkeypatch, [(1, ["AAAA3"])])

    r = runner.invoke(app, ["validate-cadastro"])

    assert r.exit_code == 0, r.output
    assert "AVISO" in r.output
    assert "CRFB3" in r.output
    assert "avisos: 1" in r.output
    assert "falhas: 0" in r.output
