"""CLI `judge-news --status`: configuracao do Jev e da LLM, sem imprimir chave. Sem rede, sem banco.

`get_settings`, `contar_pendentes` e `gasto_usd` sao trocados por valores falsos.
"""

from __future__ import annotations

from typer.testing import CliRunner

from tcc_scraper.cli import app
from tcc_scraper.config import Settings

runner = CliRunner()

CHAVE_LLM = "sk-llm-segredo-nao-pode-vazar"
CHAVE_JEV = "jev-segredo-nao-pode-vazar"


def _falsear_status(monkeypatch, **campos) -> None:
    base = {"gemini_api_key": "", "openai_api_key": "", "jev_api_key": "", "jev_enabled": True,
            "juiza_provedor": "openai"}
    base.update(campos)
    settings = Settings(**base)
    monkeypatch.setattr("tcc_scraper.config.get_settings", lambda: settings)
    monkeypatch.setattr("tcc_scraper.llm.juiza.contar_pendentes", lambda *a, **k: {"pendentes": 0})
    monkeypatch.setattr("tcc_scraper.llm.juiza_custo.gasto_usd", lambda *a, **k: 0.0)


def _status(monkeypatch, **campos):
    _falsear_status(monkeypatch, **campos)
    return runner.invoke(app, ["judge-news", "--status"], env={"COLUMNS": "200"})


def test_status_jev_ligado_sem_chave_mostra_ausente_e_o_aviso(monkeypatch) -> None:
    r = _status(monkeypatch, jev_enabled=True, jev_api_key="", openai_api_key=CHAVE_LLM)

    assert r.exit_code == 0, r.output
    assert "jev_chave" in r.output
    assert "AUSENTE" in r.output
    assert "jev_sem_chave" in r.output
    assert CHAVE_LLM not in r.output


def test_status_lista_llm_sem_chave_quando_o_provedor_nao_tem_chave(monkeypatch) -> None:
    r = _status(monkeypatch, jev_enabled=False, openai_api_key="", juiza_provedor="openai")

    assert r.exit_code == 0, r.output
    assert "jev_desligado" in r.output
    assert "openai_sem_chave" in r.output
    assert "llm_chave" in r.output


def test_status_com_chaves_presentes_nao_imprime_os_valores(monkeypatch) -> None:
    r = _status(monkeypatch, jev_enabled=True, jev_api_key=CHAVE_JEV, openai_api_key=CHAVE_LLM)

    assert r.exit_code == 0, r.output
    assert "presente" in r.output
    assert "jev_sem_chave" not in r.output
    assert CHAVE_JEV not in r.output
    assert CHAVE_LLM not in r.output
