"""Comandos collect-wp e processar-wp: validacao do recorte e repasse ao coletor e ao processamento.

Coletor e processamento sao duble (o enrich/processar_wp.py pode nao existir ainda).
Sem rede e sem banco.
"""

from __future__ import annotations

import datetime as dt
import sys
import types

import pytest
from typer.testing import CliRunner

from tcc_scraper.cli import app
from tcc_scraper.config import get_settings

STATS_VAZIO = {"total": 0, "ok": 0, "erro": 0, "itens": 0, "saturadas": 0, "interrompido": 0}


def _coletor_falso(construido: list[dict]):
    class Falso:
        def __init__(self, **kwargs) -> None:
            construido.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> None:
            return None

        def run(self, *, resume=True, observador=None) -> dict:
            return dict(STATS_VAZIO)

    return Falso


def _processar_falso(monkeypatch, chamadas: list[dict]) -> None:
    def processar_wp(**kwargs) -> dict:
        chamadas.append(kwargs)
        return dict(STATS_VAZIO)

    modulo = types.ModuleType("tcc_scraper.enrich.processar_wp")
    modulo.processar_wp = processar_wp
    monkeypatch.setitem(sys.modules, "tcc_scraper.enrich.processar_wp", modulo)


def test_collect_wp_repassa_o_recorte_ao_coletor(monkeypatch) -> None:
    construido: list[dict] = []
    monkeypatch.setattr(
        "tcc_scraper.collectors.wordpress.WordPressCollector", _coletor_falso(construido)
    )

    resultado = CliRunner().invoke(
        app, ["collect-wp", "--desde", "2026-01-01", "--ate", "2026-02-01"]
    )

    assert resultado.exit_code == 0, resultado.output
    assert construido == [{"desde": dt.date(2026, 1, 1), "ate": dt.date(2026, 2, 1)}]


def test_collect_wp_sem_recorte_deixa_o_coletor_na_janela_padrao(monkeypatch) -> None:
    construido: list[dict] = []
    monkeypatch.setattr(
        "tcc_scraper.collectors.wordpress.WordPressCollector", _coletor_falso(construido)
    )

    resultado = CliRunner().invoke(app, ["collect-wp"])

    assert resultado.exit_code == 0, resultado.output
    assert construido == [{"desde": None, "ate": None}]


@pytest.mark.parametrize(
    "argumentos",
    [
        ["collect-wp", "--desde", "2026-01-01"],
        ["collect-wp", "--ate", "2026-02-01"],
        ["collect-wp", "--desde", "2026-02-01", "--ate", "2026-01-01"],
        ["collect-wp", "--desde", "nao-e-data", "--ate", "2026-01-01"],
        ["processar-wp", "--ate", "2026-02-01"],
        ["processar-wp", "--desde", "2026-02-01", "--ate", "2026-02-01"],
    ],
)
def test_recorte_invalido_e_recusado(argumentos) -> None:
    resultado = CliRunner().invoke(app, argumentos)

    assert resultado.exit_code != 0


def test_processar_wp_repassa_o_recorte_e_o_refazer(monkeypatch) -> None:
    chamadas: list[dict] = []
    _processar_falso(monkeypatch, chamadas)

    resultado = CliRunner().invoke(
        app,
        ["processar-wp", "--desde", "2026-01-01", "--ate", "2026-02-01", "--refazer"],
    )

    assert resultado.exit_code == 0, resultado.output
    assert chamadas == [
        {"desde": dt.date(2026, 1, 1), "ate": dt.date(2026, 2, 1), "refazer": True}
    ]


def test_processar_wp_sem_recorte_usa_a_janela_das_noticias(monkeypatch) -> None:
    chamadas: list[dict] = []
    _processar_falso(monkeypatch, chamadas)
    s = get_settings()

    resultado = CliRunner().invoke(app, ["processar-wp"])

    assert resultado.exit_code == 0, resultado.output
    esperado = {
        "desde": s.inicio_noticias,
        "ate": s.fim_coleta + dt.timedelta(days=1),
        "refazer": False,
    }
    assert chamadas == [esperado]
