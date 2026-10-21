"""Comando `coletar`: validacao dos parametros e montagem do plano, sem rede nem banco."""

from __future__ import annotations

import re
from datetime import date

import pytest

from tcc_scraper.config import IBOV_TICKERS, Settings
from tcc_scraper.orquestrador import coletores
from tcc_scraper.orquestrador.coletar import montar_plano

STATS = {"total": 1, "ok": 1, "erro": 0, "itens": 5, "saturadas": 0, "interrompido": 0}
FINBERT = {"gravados": 7, "interrompido": 0}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        gemini_api_key="", data_start=date(2025, 9, 1), data_fim_coleta=date(2026, 10, 19)
    )


class _Obs:
    """Observador de progresso falso: os coletores falsos nao chamam nenhum metodo."""

    def plano(self, total: int) -> None:
        return None

    def unidade(self, *args, **kwargs) -> None:
        return None

    def deve_parar(self) -> bool:
        return False

    def log(self, *args, **kwargs) -> None:
        return None


class _Coletor:
    """Duble de classe de coletor: registra o `run` e devolve estatisticas fixas."""

    def __init__(self, registro: list, nome: str) -> None:
        self._registro = registro
        self._nome = nome

    def __enter__(self) -> _Coletor:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def run(self, *, observador, **kwargs) -> dict:
        self._registro.append(("run", self._nome, kwargs))
        return dict(STATS)


def _funcao(registro: list, nome: str, retorno_fn):
    """Duble de funcao (finbert, juiza): registra os kwargs e devolve o resultado."""

    def falsa(**kwargs):
        registro.append(("func", nome, kwargs))
        return retorno_fn()

    return falsa


def _resultado_juiza():
    from tcc_scraper.llm.juiza import ResultadoJuiza

    return ResultadoJuiza(
        pendentes=10,
        regra=4,
        jev=2,
        gemini=3,
        erros=1,
        runs=2,
        tokens_entrada=100,
        tokens_saida=20,
        interrompido=0,
    )


def _instalar(monkeypatch, registro: list) -> None:
    """Troca o resolvedor de alvos: classes viram duble de coletor, funcoes viram duble."""

    def falso(alvo: str):
        nome = alvo.split(":")[1]
        if nome == "score_pending":
            return _funcao(registro, nome, lambda: dict(FINBERT))
        if nome == "julgar_pendentes":
            return _funcao(registro, nome, _resultado_juiza)

        def fabrica(**kwargs):
            registro.append(("init", nome, kwargs))
            return _Coletor(registro, nome)

        return fabrica

    monkeypatch.setattr(coletores, "resolver", falso)


def test_contrato_literal_e_aceito(settings: Settings) -> None:
    plano = montar_plano(
        {
            "itens": [{"coletor": "cvm_itr_dfp", "anos": [2024, 2025], "refazer": True}],
            "montar_entrega": ["balancos"],
            "origem_verificacoes": ["balancos_contas_novas"],
        },
        settings,
    )

    assert [e.id for e in plano.etapas] == ["cvm_itr_dfp", "build_entrega"]
    assert [e.ordem for e in plano.etapas] == [1, 2]
    assert plano.verificacoes == ("balancos_contas_novas",)


def test_origem_verificacoes_texto_vira_lista(settings: Settings) -> None:
    plano = montar_plano(
        {"itens": [{"coletor": "bcb"}], "origem_verificacoes": "balancos_contas_novas"},
        settings,
    )

    assert plano.verificacoes == ("balancos_contas_novas",)


RECUSAS = [
    ({}, "itens vazios"),
    ({"itens": []}, "itens vazios"),
    ({"itens": [{"coletor": "xpto"}]}, "coletor desconhecido"),
    ({"itens": [{"coletor": "bcb", "anos": [2025]}]}, "não aceita o parâmetro anos"),
    ({"itens": [{"coletor": "cvm_itr_dfp", "anos": [2019]}]}, "fora da janela"),
    ({"itens": [{"coletor": "cvm_ipe", "anos": [2024]}]}, "fora da janela"),
    ({"itens": [{"coletor": "bcb"}], "montar_entrega": ["xpto"]}, "tabela de entrega inválida"),
    ({"itens": [{"coletor": "finbert", "refazer": True}]}, "refazer não se aplica"),
    ({"itens": [{"coletor": "build_entrega"}]}, "use montar_entrega"),
    ({"itens": [{"coletor": "bcb"}, {"coletor": "bcb"}]}, "coletor repetido"),
    ({"itens": [{"coletor": "google_news", "desde": "2026-01-01"}]}, "desde e ate"),
    (
        {"itens": [{"coletor": "google_news", "desde": "2020-01-01", "ate": "2020-02-01"}]},
        "fora da janela",
    ),
    ({"itens": [{"coletor": "yahoo", "tickers": ["XPTO3"]}]}, "ticker fora do universo"),
    ({"itens": [{"coletor": "bcb"}], "xpto": 1}, "parâmetro desconhecido"),
    ({"itens": [{"coletor": "bcb", "refazer": "sim"}]}, "refazer deve ser"),
]


@pytest.mark.parametrize(("parametros", "trecho"), RECUSAS)
def test_recusa_com_mensagem_do_contrato(parametros, trecho: str, settings: Settings) -> None:
    with pytest.raises(ValueError, match=re.escape(trecho)):
        montar_plano(parametros, settings)


def test_cvm_refazer_passa_anos_ordenados_e_resume_false(monkeypatch, settings) -> None:
    registro: list = []
    _instalar(monkeypatch, registro)
    plano = montar_plano(
        {"itens": [{"coletor": "cvm_itr_dfp", "anos": [2025, 2024], "refazer": True}]},
        settings,
    )

    resultado = plano.etapas[0].run(None, _Obs())

    assert resultado.status == "concluida"
    assert resultado.itens == 5
    assert ("init", "CvmDemonstracoesCollector", {"anos": [2024, 2025]}) in registro
    assert ("run", "CvmDemonstracoesCollector", {"resume": False}) in registro


def test_google_news_sem_refazer_usa_checkpoint(monkeypatch, settings) -> None:
    registro: list = []
    _instalar(monkeypatch, registro)
    plano = montar_plano({"itens": [{"coletor": "google_news"}]}, settings)

    plano.etapas[0].run(None, _Obs())

    assert ("run", "GoogleNewsCollector", {"resume": True}) in registro


def test_yahoo_sem_refazer_nao_tem_checkpoint(monkeypatch, settings) -> None:
    registro: list = []
    _instalar(monkeypatch, registro)
    plano = montar_plano({"itens": [{"coletor": "yahoo"}]}, settings)

    plano.etapas[0].run(None, _Obs())

    assert ("init", "YahooPriceCollector", {}) in registro
    assert ("run", "YahooPriceCollector", {"resume": False}) in registro


def test_google_news_tickers_viram_dict_e_periodo_vira_date(monkeypatch, settings) -> None:
    registro: list = []
    _instalar(monkeypatch, registro)
    plano = montar_plano(
        {
            "itens": [
                {
                    "coletor": "google_news",
                    "tickers": ["PETR4"],
                    "desde": "2026-01-05",
                    "ate": "2026-01-12",
                }
            ]
        },
        settings,
    )

    plano.etapas[0].run(None, _Obs())

    init = [kw for tipo, nome, kw in registro if tipo == "init" and nome == "GoogleNewsCollector"]
    assert init[0]["tickers"] == {"PETR4": IBOV_TICKERS["PETR4"]}
    assert init[0]["desde"] == date(2026, 1, 5)
    assert init[0]["ate"] == date(2026, 1, 12)


def test_juiza_recebe_limite_do_settings(monkeypatch, settings) -> None:
    registro: list = []
    _instalar(monkeypatch, registro)
    plano = montar_plano({"itens": [{"coletor": "juiza"}]}, settings)

    resultado = plano.etapas[0].run(None, _Obs())

    chamada = [kw for tipo, nome, kw in registro if nome == "julgar_pendentes"]
    assert chamada[0]["limite_itens"] == settings.juiza_max_itens_dia
    assert resultado.status == "concluida"
    assert resultado.itens == 9


def test_finbert_recebe_so_observador_e_status_vem_do_dict(monkeypatch, settings) -> None:
    registro: list = []
    _instalar(monkeypatch, registro)
    plano = montar_plano({"itens": [{"coletor": "finbert"}]}, settings)

    resultado = plano.etapas[0].run(None, _Obs())

    chamada = [kw for tipo, nome, kw in registro if nome == "score_pending"]
    assert set(chamada[0]) == {"observador"}
    assert resultado.status == "concluida"
    assert resultado.itens == 7


def test_montar_entrega_chama_build_entrega_com_a_tabela(monkeypatch, settings) -> None:
    chamadas: list = []

    def falso(**kwargs):
        chamadas.append(kwargs)
        return {"balancos": 3}

    monkeypatch.setattr("tcc_scraper.entrega.builder.build_entrega", falso)
    plano = montar_plano(
        {"itens": [{"coletor": "bcb"}], "montar_entrega": ["balancos"]}, settings
    )

    assert [e.id for e in plano.etapas] == ["bcb", "build_entrega"]
    resultado = plano.etapas[-1].run(None, _Obs())

    assert resultado.status == "concluida"
    assert resultado.itens == 3
    assert chamadas[0]["tabelas"] == ["balancos"]
