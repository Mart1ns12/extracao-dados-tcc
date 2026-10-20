"""Executor sem banco e sem rede: RepositorioMemoria, API falsa com respx e relogio injetado."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import httpx
import pytest
import respx

from tcc_scraper.config import Settings
from tcc_scraper.executor.comandos import Aplicador, HistoricoIndisponivel
from tcc_scraper.executor.loop import Executor
from tcc_scraper.executor.painel_client import Comando, PainelClient
from tcc_scraper.orquestrador import etapas as E
from tcc_scraper.orquestrador.daily import Daily
from tcc_scraper.orquestrador.etapas import EtapaDef, ResultadoEtapa
from tcc_scraper.orquestrador.repositorio import RepositorioMemoria

URL = "http://painel.test"
# 18:30 UTC = 15:30 em Sao Paulo: antes do horario do cron.
AGORA = datetime(2026, 10, 19, 18, 30, tzinfo=UTC)
COMANDOS = f"{URL}/api/executor/comandos"


class Relogio:
    def __init__(self, inicio: datetime = AGORA) -> None:
        self.t = inicio

    def __call__(self) -> datetime:
        return self.t


class HistoricoFalso:
    def __init__(self, bloqueia: bool = False, ocupado: bool = False) -> None:
        self._bloqueia = bloqueia
        self._ocupado = ocupado
        self.disparos: list[tuple[list[int], str]] = []
        self.controles: list[str] = []

    @property
    def ocupado(self) -> bool:
        return self._ocupado

    def bloqueia(self) -> bool:
        return self._bloqueia

    def liberar(self) -> None:
        self._bloqueia = False

    def disparar(self, anos, modo):
        self.disparos.append((anos, modo))
        return "hist-1"

    def controlar(self, acao):
        self.controles.append(acao)
        return acao

    def recuperar_apos_reinicio(self) -> None:
        return None


class RepoQuebrado(RepositorioMemoria):
    def atual(self):
        raise RuntimeError("banco fora")


def _settings() -> Settings:
    return Settings(
        gemini_api_key="",
        painel_api_url=URL,
        executor_token="tok",
        executor_poll_s=0,
        executor_heartbeat_s=3600,
        executor_host="maquina-teste",
    )


def _rapida(eid: str, ordem: int) -> EtapaDef:
    def run(ctx, p):
        p.plano(1)
        p.unidade("u", ok=True, itens=1, saturada=False, erro=None)
        return ResultadoEtapa("concluida", itens=1)

    return EtapaDef(eid, ordem, f"nome {eid}", f"coletor {eid}", run)


def _comando(id_: str, tipo: str, **parametros) -> dict:
    return {
        "id": id_,
        "tipo": tipo,
        "parametros": parametros,
        "criado_em": "2026-10-19T18:00:00Z",
    }


def _esperar(daily: Daily) -> None:
    if daily._thread is not None:
        daily._thread.join(5)


def _executor(repo, daily, historico, relogio) -> Executor:
    client = PainelClient(URL, "tok", client=httpx.Client())
    return Executor(repo, daily, historico, client, _settings(), agora=relogio)


@pytest.fixture
def cenario(monkeypatch):
    monkeypatch.setattr(E, "ETAPAS", (_rapida("precos", 1), _rapida("noticias", 3)))
    repo = RepositorioMemoria()
    relogio = Relogio()
    daily = Daily(repo, settings=_settings(), agora=relogio)
    return repo, daily, relogio


# -- comandos e ack ----------------------------------------------------------------
def test_comando_daily_aplica_confirma_uma_vez_e_nao_reaplica(cenario):
    repo, daily, relogio = cenario
    historico = HistoricoFalso()
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(
            return_value=httpx.Response(200, json={"comandos": [_comando("c1", "daily")]})
        )
        ack = router.post(f"{URL}/api/executor/comandos/c1/ack").mock(
            return_value=httpx.Response(204)
        )
        ex = _executor(repo, daily, historico, relogio)

        ex.ciclo()
        _esperar(daily)
        assert ack.call_count == 1
        assert json.loads(ack.calls[-1].request.content) == {"ok": True, "erro": None}
        assert len(repo.execucoes) == 1

        ex.ciclo()  # o painel entregou de novo: ack ok, sem nova execucao
        _esperar(daily)
        assert ack.call_count == 2
        assert json.loads(ack.calls[-1].request.content) == {"ok": True, "erro": None}
        assert len(repo.execucoes) == 1


def test_pausar_sem_carga_confirma_com_motivo(cenario):
    repo, daily, relogio = cenario
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(
            return_value=httpx.Response(200, json={"comandos": [_comando("p1", "pausar")]})
        )
        ack = router.post(f"{URL}/api/executor/comandos/p1/ack").mock(
            return_value=httpx.Response(204)
        )
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)

        ex.ciclo()

        assert json.loads(ack.calls[-1].request.content) == {
            "ok": False,
            "erro": "carga histórica ainda não implementada",
        }


def test_tipo_desconhecido_e_recusado(cenario):
    repo, daily, _ = cenario
    aplicador = Aplicador(repo, daily, HistoricoIndisponivel(), _settings())
    assert aplicador.aplicar(Comando("x1", "xpto", {}, "")) == (False, "tipo desconhecido: xpto")


def test_historico_comando_repassa_anos_e_modo(cenario):
    repo, daily, _ = cenario
    historico = HistoricoFalso()
    aplicador = Aplicador(repo, daily, historico, _settings())
    cmd = Comando("h1", "historico", {"anos": [2024, 2025], "modo": "reprocessar_erros"}, "")
    assert aplicador.aplicar(cmd) == (True, None)
    assert historico.disparos == [([2024, 2025], "reprocessar_erros")]


# -- falhas de rede e de banco nao derrubam o loop ----------------------------------
def test_api_com_500_ou_rede_fora_nao_levanta(cenario):
    repo, daily, relogio = cenario
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(return_value=httpx.Response(500))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()
        assert not repo.execucoes

    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(side_effect=httpx.ConnectError("sem rede"))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()
        assert not repo.execucoes


def test_ack_falhando_nao_impede_o_ciclo(cenario):
    repo, daily, relogio = cenario
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(
            return_value=httpx.Response(200, json={"comandos": [_comando("c9", "daily")]})
        )
        router.post(f"{URL}/api/executor/comandos/c9/ack").mock(return_value=httpx.Response(500))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()
        _esperar(daily)
    assert len(repo.execucoes) == 1
    assert repo.comando_aplicado("c9")


def test_erro_no_banco_nao_levanta_do_ciclo(cenario):
    _, _, relogio = cenario
    repo = RepoQuebrado()
    daily = Daily(repo, settings=_settings(), agora=relogio)
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(return_value=httpx.Response(200, json={"comandos": []}))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()


# -- heartbeat ------------------------------------------------------------------------
def test_heartbeat_manda_ocupado_com_historico(cenario):
    repo, daily, relogio = cenario
    with respx.mock(assert_all_called=False) as router:
        hb = router.post(f"{URL}/api/executor/heartbeat").mock(return_value=httpx.Response(204))
        ex = _executor(repo, daily, HistoricoFalso(ocupado=True), relogio)
        ex.bater_heartbeat()
        body = json.loads(hb.calls[-1].request.content)
    assert body["host"] == "maquina-teste"
    assert body["ocupado_com"] == "historico"
    assert repo.heartbeats["maquina-teste"]["ocupado_com"] == "historico"


def test_ocupado_com_daily_enquanto_etapa_roda(cenario, monkeypatch):
    repo, daily, relogio = cenario
    liberar = threading.Event()

    def trava(ctx, p):
        liberar.wait(5)
        return ResultadoEtapa("concluida", itens=0)

    monkeypatch.setattr(E, "ETAPAS", (EtapaDef("precos", 1, "p", "c", trava),))
    ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
    eid = daily.criar("daily")
    daily.iniciar(eid)
    try:
        assert ex.ocupado_com() == "daily"
    finally:
        liberar.set()
        _esperar(daily)
    assert ex.ocupado_com() is None


# -- carga historica e agenda -----------------------------------------------------------
def test_historico_bloqueia_daily_e_inicia_quando_liberar(cenario):
    repo, daily, relogio = cenario
    historico = HistoricoFalso(bloqueia=True)
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(
            return_value=httpx.Response(200, json={"comandos": [_comando("c1", "daily")]})
        )
        ack = router.post(f"{URL}/api/executor/comandos/c1/ack").mock(
            return_value=httpx.Response(204)
        )
        ex = _executor(repo, daily, historico, relogio)

        ex.ciclo()
        exe = repo.atual()
        assert exe["status"] == "agendada"
        assert exe["aguardando"] == "historico"
        assert not daily.ocupado
        assert json.loads(ack.calls[-1].request.content) == {"ok": True, "erro": None}

        historico.liberar()
        ex.ciclo()
        _esperar(daily)
    assert repo.execucoes[exe["id"]]["status"] == "concluida"
    assert repo.execucoes[exe["id"]]["aguardando"] is None


def test_cron_dispara_as_1830_uma_vez_e_nao_faz_ack(cenario):
    repo, daily, relogio = cenario
    relogio.t = datetime(2026, 10, 19, 21, 31, tzinfo=UTC)  # 18:31 em Sao Paulo
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(return_value=httpx.Response(200, json={"comandos": []}))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)

        ex.ciclo()
        _esperar(daily)
        ex.ciclo()
        _esperar(daily)

        assert len(repo.execucoes) == 1
        (exe,) = repo.execucoes.values()
        assert exe["origem"] == "cron"
        assert repo.comando_aplicado("cron-2026-10-19")
        assert not any("/ack" in str(c.request.url) for c in router.calls)


def test_cron_nao_dispara_antes_do_horario(cenario):
    repo, daily, relogio = cenario  # 15:30 em Sao Paulo
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(return_value=httpx.Response(200, json={"comandos": []}))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()
    assert not repo.execucoes


# -- cliente HTTP ------------------------------------------------------------------------
def test_cliente_le_comandos_e_manda_bearer():
    with respx.mock(assert_all_called=False) as router:
        rota = router.get(COMANDOS).mock(
            return_value=httpx.Response(
                200, json={"comandos": [_comando("c1", "etapa", etapa="noticias")]}
            )
        )
        cmds = PainelClient(URL, "segredo", client=httpx.Client()).comandos()
    assert cmds == [
        Comando("c1", "etapa", {"etapa": "noticias"}, "2026-10-19T18:00:00Z"),
    ]
    assert rota.calls[-1].request.headers["Authorization"] == "Bearer segredo"


def test_cliente_comandos_com_500_devolve_none():
    with respx.mock(assert_all_called=False) as router:
        router.get(COMANDOS).mock(return_value=httpx.Response(500))
        assert PainelClient(URL, "tok", client=httpx.Client()).comandos() is None


def test_cliente_ack_409_conta_como_confirmado_e_404_como_falha():
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{URL}/api/executor/comandos/a/ack").mock(return_value=httpx.Response(409))
        router.post(f"{URL}/api/executor/comandos/b/ack").mock(return_value=httpx.Response(404))
        cliente = PainelClient(URL, "tok", client=httpx.Client())
        assert cliente.ack("a", True, None) is True
        assert cliente.ack("b", True, None) is False
