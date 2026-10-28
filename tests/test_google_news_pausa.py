"""Disjuntor do Google News: pausa progressiva em vez de abortar, e parada durante a pausa.

Sem rede e sem banco: fetch, checkpoint, relogio e sono sao substituidos. A pausa dorme
pelo RelogioFalso, entao 'esperar 30 min' custa zero segundos reais.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from pydantic import ValidationError

from tcc_scraper.collectors import google_news as google_news_mod
from tcc_scraper.collectors.base import AbortarColeta, FalhaBusca, WorkUnit
from tcc_scraper.collectors.google_news import GoogleNewsCollector
from tcc_scraper.config import GOOGLE_NEWS_LOCALES, Settings
from tests.relogio_falso import RelogioFalso

INICIO = dt.date(2026, 7, 1)
FIM = dt.date(2026, 7, 4)
AGORA = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.UTC)
LOG = "tcc_scraper.collectors.google_news"


def _falhas(n: int) -> list[FalhaBusca]:
    """n falhas de rede novas, como o fetch levanta depois das tentativas (HTTP 503)."""
    mensagem = "google_news: falha ao buscar https://news.google.com/rss (HTTP 503)"
    return [FalhaBusca(mensagem, "HTTP 503") for _ in range(n)]


def _sequencia(respostas: list):
    """Falso _fetch_window: cada chamada devolve a proxima resposta (ou levanta, se for excecao)."""
    fila = list(respostas)
    chamadas: list[tuple] = []

    def falso(*args):
        chamadas.append(args)
        valor = fila.pop(0)
        if isinstance(valor, Exception):
            raise valor
        return list(valor)

    return falso, chamadas


def _unidade(indice: int = 0) -> WorkUnit:
    return WorkUnit(
        key=f"PETR4|{indice}",
        payload={
            "ticker": "PETR4",
            "company": "Petrobras",
            "start": INICIO,
            "end": FIM,
            "locale": GOOGLE_NEWS_LOCALES[0],
        },
    )


def _instalar_tempo(monkeypatch, coletor, relogio: RelogioFalso) -> None:
    monkeypatch.setattr(coletor, "_relogio", relogio)
    monkeypatch.setattr(coletor, "_dormir", relogio.dormir)
    monkeypatch.setattr(coletor, "_agora", lambda: AGORA)


def _coletor(monkeypatch, relogio: RelogioFalso, **config) -> GoogleNewsCollector:
    coletor = GoogleNewsCollector(tickers={"PETR4": "Petrobras"}, desde=INICIO, ate=FIM)
    monkeypatch.setattr(coletor, "settings", Settings(gemini_api_key="", **config))
    _instalar_tempo(monkeypatch, coletor, relogio)
    return coletor


def _avisos(caplog) -> list[str]:
    registros = caplog.records
    return [r.getMessage() for r in registros if r.name == LOG and r.levelno == logging.WARNING]


# -- pausa em process --------------------------------------------------------------------


def test_dez_falhas_pausam_30_min_e_um_sucesso_zera_o_indice(monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=LOG)
    relogio = RelogioFalso()
    falso, _ = _sequencia(_falhas(10) + [[]] + _falhas(10))
    coletor = _coletor(monkeypatch, relogio, google_news_max_falhas_seguidas=10)
    monkeypatch.setattr(coletor, "_fetch_window", falso)

    for i in range(10):
        with pytest.raises(FalhaBusca):
            coletor.process(_unidade(i))  # a 10a falha dispara a pausa 1 (30 min)

    assert sum(relogio.dormidas) == 30 * 60
    assert coletor._pausas_seguidas == 1

    assert coletor.process(_unidade(10)).items_found == 0  # sucesso
    assert coletor._pausas_seguidas == 0

    for i in range(11, 21):
        with pytest.raises(FalhaBusca):
            coletor.process(_unidade(i))  # volta a ser a pausa 1, nao a 2

    avisos = _avisos(caplog)
    assert len(avisos) == 2
    assert avisos == [
        "google_news: provavel bloqueio (10 unidades seguidas falharam; ultima causa: HTTP 503); "
        "pausa de 30 min ate 12:30 UTC (pausa 1/3)"
    ] * 2


def test_falhas_continuas_usam_as_tres_pausas_e_depois_abortam(monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger=LOG)
    relogio = RelogioFalso()
    falso, chamadas = _sequencia(_falhas(40))
    coletor = _coletor(monkeypatch, relogio, google_news_max_falhas_seguidas=10)
    monkeypatch.setattr(coletor, "_fetch_window", falso)

    for i in range(39):  # pausas nas falhas 10, 20 e 30
        with pytest.raises(FalhaBusca):
            coletor.process(_unidade(i))
    with pytest.raises(AbortarColeta, match="unidades seguidas falharam") as info:
        coletor.process(_unidade(39))  # a 40a falha: as pausas acabaram

    assert "apos 3 pausas" in str(info.value)
    assert len(chamadas) == 40
    assert sum(relogio.dormidas) == (30 + 60 + 120) * 60

    avisos = _avisos(caplog)
    assert len(avisos) == 3
    assert "pausa de 30 min" in avisos[0] and "(pausa 1/3)" in avisos[0]
    assert "pausa de 60 min" in avisos[1] and "(pausa 2/3)" in avisos[1]
    assert "pausa de 120 min" in avisos[2] and "(pausa 3/3)" in avisos[2]


def test_pausas_invalidas_sao_erro_de_validacao() -> None:
    for valor in ["", "30,abc", "0", "30,-5", "nan"]:
        with pytest.raises(ValidationError, match="GOOGLE_NEWS_PAUSAS_MIN"):
            Settings(google_news_pausas_min=valor)


def test_pausas_padrao_viram_minutos_em_ordem() -> None:
    assert Settings(google_news_pausas_min="30,60,120").google_news_pausas == (30.0, 60.0, 120.0)


# -- run: parada durante a pausa e continuidade ------------------------------------------


class Observador:
    """Observador minimo: `parar` liga a parada, que o run e a pausa consultam por deve_parar."""

    def __init__(self) -> None:
        self.parar = False
        self.planos: list[int] = []
        self.unidades: list[tuple] = []

    def plano(self, total: int) -> None:
        self.planos.append(total)

    def unidade(self, key, *, ok, itens, saturada, erro) -> None:
        self.unidades.append((key, ok, erro))

    def deve_parar(self) -> bool:
        return self.parar


def _coletor_run(monkeypatch, relogio: RelogioFalso, respostas: list, n_unidades: int = 12):
    """Coletor com n_unidades janelas (uma por ticker, so pt) e checkpoint substituido."""
    monkeypatch.setattr(
        google_news_mod, "get_settings", lambda: Settings(google_news_idiomas="pt")
    )
    tickers = {f"TK{i:02d}": f"Empresa {i}" for i in range(n_unidades)}
    coletor = GoogleNewsCollector(
        tickers=tickers, desde=INICIO, ate=INICIO + dt.timedelta(days=7)
    )
    monkeypatch.setattr(
        coletor, "settings", Settings(gemini_api_key="", google_news_max_falhas_seguidas=10)
    )
    _instalar_tempo(monkeypatch, coletor, relogio)
    falso, chamadas = _sequencia(respostas)
    monkeypatch.setattr(coletor, "_fetch_window", falso)
    registros: list[tuple] = []
    monkeypatch.setattr(coletor, "completed_keys", lambda: set())
    monkeypatch.setattr(
        coletor, "_record", lambda unit, result, error: registros.append((unit.key, error))
    )
    return coletor, chamadas, registros


def test_parada_durante_a_pausa_encerra_o_run_sem_erro(monkeypatch) -> None:
    relogio = RelogioFalso()
    obs = Observador()
    coletor, chamadas, registros = _coletor_run(
        monkeypatch, relogio, _falhas(10) + [[], []]
    )
    passos = 0

    def sono_que_pede_parada(segundos: float) -> None:
        nonlocal passos
        relogio.dormir(segundos)
        passos += 1
        if passos == 3:  # o painel pede parada no meio da pausa
            obs.parar = True

    monkeypatch.setattr(coletor, "_dormir", sono_que_pede_parada)

    stats = coletor.run(observador=obs)  # nao levanta

    assert stats["interrompido"] == 1
    assert stats["ok"] == 0 and stats["erro"] == 9  # as 9 primeiras ficam com erro
    assert len(chamadas) == 10  # as unidades 11 e 12 nao foram executadas
    assert [k.split("|")[0] for k, _ in registros] == [f"TK{i:02d}" for i in range(9)]
    assert relogio.dormidas == [5.0, 5.0, 5.0]  # parou no passo de 5 s, nao no fim da pausa


class ObservadorDoPainel(Observador):
    """Como os observadores do daily e da carga: tem log e inicio_unidade (opcionais)."""

    def __init__(self) -> None:
        super().__init__()
        self.logs: list[tuple[str, str]] = []
        self.inicios: list[str] = []

    def log(self, nivel: str, mensagem: str) -> None:
        self.logs.append((nivel, mensagem))

    def inicio_unidade(self, key: str) -> None:
        self.inicios.append(key)


def test_pausa_aparece_no_painel_como_log_e_unidade_atual(monkeypatch) -> None:
    relogio = RelogioFalso()
    obs = ObservadorDoPainel()
    coletor, _, _ = _coletor_run(monkeypatch, relogio, _falhas(10) + [[], []])

    coletor.run(observador=obs)

    assert obs.logs == [(
        "aviso",
        "google_news: provavel bloqueio (10 unidades seguidas falharam; ultima causa: "
        "HTTP 503); pausa de 30 min ate 12:30 UTC (pausa 1/3)",
    )]
    assert "pausa por bloqueio ate 12:30 UTC (1/3)" in obs.inicios
    assert all(len(k) <= 128 for k in obs.inicios)  # cabe em unidade_atual (String(128))


def test_depois_da_pausa_o_run_segue_para_as_proximas_unidades(monkeypatch) -> None:
    relogio = RelogioFalso()
    coletor, _, registros = _coletor_run(monkeypatch, relogio, _falhas(10) + [[], []])

    stats = coletor.run()

    assert stats["interrompido"] == 0
    assert stats["ok"] == 2 and stats["erro"] == 10
    assert sum(relogio.dormidas) == 30 * 60
    sucessos = [k.split("|")[0] for k, erro in registros if erro is None]
    assert sucessos == ["TK10", "TK11"]
