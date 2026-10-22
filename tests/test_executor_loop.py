"""Executor sem banco e sem rede: RepositorioMemoria, API falsa com respx e relogio injetado."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from tcc_scraper.config import Settings
from tcc_scraper.executor.comandos import Aplicador, HistoricoIndisponivel
from tcc_scraper.executor.loop import Executor
from tcc_scraper.executor.painel_client import Comando, PainelClient
from tcc_scraper.orquestrador import coletores
from tcc_scraper.orquestrador import etapas as E
from tcc_scraper.orquestrador import historico as H
from tcc_scraper.orquestrador.daily import Daily
from tcc_scraper.orquestrador.etapas import EtapaDef, ResultadoEtapa
from tcc_scraper.orquestrador.historico import Historico
from tcc_scraper.orquestrador.repositorio import RepositorioMemoria

URL = "http://painel.test"
# 18:30 UTC = 15:30 em Sao Paulo: antes do horario do cron (daily_hora=18:30 nos settings).
AGORA = datetime(2026, 10, 19, 18, 30, tzinfo=UTC)
COMANDOS = f"{URL}/api/executor/comandos"
ACK = f"{URL}/api/executor/comandos/ack"


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
        # Fixo: os relogios dos testes (15:30, 18:31, 18:45) supoem o cron as 18:30.
        daily_hora="18:30",
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


def _carga(status: str) -> dict:
    return {
        "id": "hist-1", "status": status, "modo": "pendentes", "anos": [2026],
        "ano_atual": None, "coletor_atual": None, "unidade_atual": None,
        "iniciada_em": AGORA, "finalizada_em": None, "itens_por_min": 0.0, "erro": None,
        "atualizada_em": AGORA,
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
        router.post(COMANDOS).mock(
            return_value=httpx.Response(200, json={"comandos": [_comando("c1", "daily")]})
        )
        ack = router.post(ACK).mock(return_value=httpx.Response(204))
        ex = _executor(repo, daily, historico, relogio)

        ex.ciclo()
        _esperar(daily)
        assert ack.call_count == 1
        assert json.loads(ack.calls[-1].request.content) == {
            "id": "c1", "ok": True, "erro": None,
        }
        assert len(repo.execucoes) == 1

        ex.ciclo()  # o painel entregou de novo: ack ok, sem nova execucao
        _esperar(daily)
        assert ack.call_count == 2
        assert json.loads(ack.calls[-1].request.content) == {
            "id": "c1", "ok": True, "erro": None,
        }
        assert len(repo.execucoes) == 1


def test_pausar_sem_carga_confirma_com_motivo(cenario):
    repo, daily, relogio = cenario
    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(
            return_value=httpx.Response(200, json={"comandos": [_comando("p1", "pausar")]})
        )
        ack = router.post(ACK).mock(return_value=httpx.Response(204))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)

        ex.ciclo()

        assert json.loads(ack.calls[-1].request.content) == {
            "id": "p1",
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
        router.post(COMANDOS).mock(return_value=httpx.Response(500))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()
        assert not repo.execucoes

    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(side_effect=httpx.ConnectError("sem rede"))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()
        assert not repo.execucoes


def test_ack_falhando_nao_impede_o_ciclo(cenario):
    repo, daily, relogio = cenario
    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(
            return_value=httpx.Response(200, json={"comandos": [_comando("c9", "daily")]})
        )
        router.post(ACK).mock(return_value=httpx.Response(500))
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
        router.post(COMANDOS).mock(return_value=httpx.Response(200, json={"comandos": []}))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()


# -- heartbeat ------------------------------------------------------------------------
def test_heartbeat_manda_ocupado_com_historico(cenario):
    repo, daily, relogio = cenario
    repo.criar_historico(_carga("em_andamento"))
    with respx.mock(assert_all_called=False) as router:
        hb = router.post(f"{URL}/api/executor/heartbeat").mock(return_value=httpx.Response(204))
        ex = _executor(repo, daily, HistoricoFalso(ocupado=True), relogio)
        ex.bater_heartbeat()
        body = json.loads(hb.calls[-1].request.content)
    assert body["host"] == "maquina-teste"
    assert body["ocupado_com"] == "historico"
    assert repo.heartbeats["maquina-teste"]["ocupado_com"] == "historico"


@pytest.mark.parametrize(
    ("status", "esperado"),
    [("em_andamento", "historico"), ("pausando", "historico"),
     ("cancelando", "historico"), ("pausada", None)],
)
def test_ocupado_com_historico_so_com_carga_ativa(cenario, status, esperado):
    repo, daily, relogio = cenario
    repo.criar_historico(_carga(status))
    ex = _executor(repo, daily, HistoricoFalso(ocupado=True), relogio)
    assert ex.ocupado_com() == esperado


def test_pausada_com_daily_rodando_reporta_daily(cenario, monkeypatch):
    repo, daily, relogio = cenario
    repo.criar_historico(_carga("pausada"))
    liberar = threading.Event()

    def trava(ctx, p):
        liberar.wait(5)
        return ResultadoEtapa("concluida", itens=0)

    monkeypatch.setattr(E, "ETAPAS", (EtapaDef("precos", 1, "p", "c", trava),))
    ex = _executor(repo, daily, HistoricoFalso(ocupado=True), relogio)
    eid = daily.criar("daily")
    daily.iniciar(eid)
    try:
        assert ex.ocupado_com() == "daily"
    finally:
        liberar.set()
        _esperar(daily)


def test_ocupado_com_nao_levanta_quando_o_banco_falha(cenario, monkeypatch):
    repo, daily, relogio = cenario

    def quebra():
        raise RuntimeError("banco fora")

    monkeypatch.setattr(repo, "historico_atual", quebra)
    ex = _executor(repo, daily, HistoricoFalso(ocupado=True), relogio)
    assert ex.ocupado_com() is None


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
        router.post(COMANDOS).mock(
            return_value=httpx.Response(200, json={"comandos": [_comando("c1", "daily")]})
        )
        ack = router.post(ACK).mock(return_value=httpx.Response(204))
        ex = _executor(repo, daily, historico, relogio)

        ex.ciclo()
        exe = repo.atual()
        assert exe["status"] == "agendada"
        assert exe["aguardando"] == "historico"
        assert not daily.ocupado
        assert json.loads(ack.calls[-1].request.content) == {
            "id": "c1", "ok": True, "erro": None,
        }

        historico.liberar()
        ex.ciclo()
        _esperar(daily)
    assert repo.execucoes[exe["id"]]["status"] == "concluida"
    assert repo.execucoes[exe["id"]]["aguardando"] is None


def test_cron_dispara_as_1830_uma_vez_e_nao_faz_ack(cenario):
    repo, daily, relogio = cenario
    relogio.t = datetime(2026, 10, 19, 21, 31, tzinfo=UTC)  # 18:31 em Sao Paulo
    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(return_value=httpx.Response(200, json={"comandos": []}))
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
        router.post(COMANDOS).mock(return_value=httpx.Response(200, json={"comandos": []}))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()
    assert not repo.execucoes


# -- cliente HTTP ------------------------------------------------------------------------
def test_cliente_le_comandos_e_manda_bearer():
    with respx.mock(assert_all_called=False) as router:
        rota = router.post(COMANDOS).mock(
            return_value=httpx.Response(
                200, json={"comandos": [_comando("c1", "etapa", etapa="noticias")]}
            )
        )
        cmds = PainelClient(URL, "segredo", client=httpx.Client()).comandos()
    assert cmds == [
        Comando("c1", "etapa", {"etapa": "noticias"}, "2026-10-19T18:00:00Z"),
    ]
    assert rota.calls[-1].request.headers["Authorization"] == "Bearer segredo"
    assert json.loads(rota.calls[-1].request.content) == {}


def test_cliente_comandos_com_500_devolve_none():
    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(return_value=httpx.Response(500))
        assert PainelClient(URL, "tok", client=httpx.Client()).comandos() is None


@pytest.mark.parametrize(
    ("status", "esperado"), [(204, True), (409, True), (404, True), (500, False)]
)
def test_cliente_ack_manda_id_no_corpo_e_trata_404_409_como_confirmado(status, esperado):
    with respx.mock(assert_all_called=False) as router:
        rota = router.post(ACK).mock(return_value=httpx.Response(status))
        cliente = PainelClient(URL, "tok", client=httpx.Client())
        assert cliente.ack("a", False, "motivo") is esperado
    assert json.loads(rota.calls[-1].request.content) == {
        "id": "a", "ok": False, "erro": "motivo",
    }


# -- carga historica real dentro do executor ------------------------------------------
class _ColetorVazio:
    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def run(self, *, observador, **_kwargs) -> dict:
        observador.plano(0)
        return {"interrompido": 0, "itens": 0}


class _ColetorTrava:
    """Segura a carga em andamento ate a pausa chegar; devolve interrompido."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def run(self, *, observador, **_kwargs) -> dict:
        observador.plano(1)
        while not observador.deve_parar():
            time.sleep(0.005)
        return {"interrompido": 1, "itens": 0}


def _resolver_vazio(alvo: str):
    nome = alvo.split(":")[1]
    if nome in ("score_pending", "julgar_pendentes", "build_entrega"):
        return lambda observador=None, **_kwargs: {"interrompido": 0}
    return lambda **_kwargs: _ColetorVazio()


def _resolver_trava(alvo: str):
    if alvo.split(":")[1] == "YahooPriceCollector":
        return lambda **_kwargs: _ColetorTrava()
    return _resolver_vazio(alvo)


def _settings_historico() -> Settings:
    return Settings(
        gemini_api_key="",
        painel_api_url=URL,
        executor_token="tok",
        executor_poll_s=0,
        executor_heartbeat_s=3600,
        executor_host="maquina-teste",
        # Fixo: os relogios dos testes (15:30, 18:31, 18:45) supoem o cron as 18:30.
        daily_hora="18:30",
        data_fim_coleta=date(2026, 10, 19),
    )


def _esperar_status_historico(repo, status: str, limite: float = 5.0) -> None:
    fim = time.monotonic() + limite
    while (atual := repo.historico_atual()) is None or atual["status"] != status:
        if time.monotonic() > fim:
            raise AssertionError(f"carga histórica não chegou a {status}")
        time.sleep(0.005)


def test_comando_historico_cria_a_carga_real(cenario, monkeypatch):
    repo, daily, relogio = cenario
    monkeypatch.setattr(H, "_resolver", _resolver_vazio)
    historico = Historico(repo, _settings_historico(), agora=relogio)
    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(
            return_value=httpx.Response(
                200,
                json={"comandos": [_comando("h1", "historico", anos=[2026], modo="pendentes")]},
            )
        )
        ack = router.post(ACK).mock(return_value=httpx.Response(204))
        ex = _executor(repo, daily, historico, relogio)

        ex.ciclo()
        historico.join(5)

    assert json.loads(ack.calls[-1].request.content) == {"id": "h1", "ok": True, "erro": None}
    assert repo.historico_atual()["status"] == "concluida"


def test_daily_com_carga_em_andamento_nasce_agendada_e_inicia_apos_pausar(cenario, monkeypatch):
    repo, daily, relogio = cenario
    monkeypatch.setattr(H, "_resolver", _resolver_trava)
    historico = Historico(repo, _settings_historico(), agora=relogio)
    historico.disparar([2026])
    try:
        with respx.mock(assert_all_called=False) as router:
            router.post(COMANDOS).mock(
                return_value=httpx.Response(200, json={"comandos": [_comando("c1", "daily")]})
            )
            router.post(ACK).mock(return_value=httpx.Response(204))
            ex = _executor(repo, daily, historico, relogio)

            ex.ciclo()
            agendada = repo.atual()
            assert agendada["status"] == "agendada"
            assert agendada["aguardando"] == "historico"
            assert not daily.ocupado

            assert historico.controlar("pausar") == "pausando"
            _esperar_status_historico(repo, "pausada")
            ex.ciclo()  # carga pausada nao bloqueia mais: o daily inicia
            _esperar(daily)
            assert repo.obter(agendada["id"])["status"] == "concluida"
    finally:
        historico.controlar("cancelar")
        historico.join(5)


def test_cron_que_falha_nao_e_registrado_e_avisa_uma_vez_por_dia(cenario, caplog):
    repo, daily, relogio = cenario
    relogio.t = datetime(2026, 10, 19, 21, 45, tzinfo=UTC)  # 18:45 em Sao Paulo
    historico = HistoricoFalso(bloqueia=True)
    # Execucao manual ainda agendada: o cron nao consegue criar a dele enquanto ela existir.
    eid = daily.criar("daily")
    ex = _executor(repo, daily, historico, relogio)

    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(return_value=httpx.Response(200, json={"comandos": []}))
        with caplog.at_level(logging.WARNING, logger="tcc_scraper.executor.loop"):
            ex.ciclo()
            ex.ciclo()
        avisos = [r for r in caplog.records if "nao disparou" in r.getMessage()]
        assert len(avisos) == 1
        assert repo.comando_aplicado("cron-2026-10-19") is False

        # A manual termina e a carga libera: o cron dispara no ciclo seguinte.
        repo.atualizar_execucao(eid, status="concluida")
        historico.liberar()
        ex.ciclo()
        _esperar(daily)

    assert repo.comando_aplicado("cron-2026-10-19") is True
    assert repo.existe_execucao_cron(date(2026, 10, 19)) is True


# -- comando coletar ------------------------------------------------------------------
_STATS_OK = {"total": 1, "ok": 1, "erro": 0, "itens": 5, "saturadas": 0, "interrompido": 0}
CONTRATO = {
    "itens": [{"coletor": "cvm_itr_dfp", "anos": [2024, 2025], "refazer": True}],
    "montar_entrega": ["balancos"],
    "origem_verificacoes": ["balancos_contas_novas"],
}


@pytest.fixture
def coletor_falso(monkeypatch):
    """Troca o alvo de todo coletor por um falso que so registra as chamadas.

    `trava` comeca aberta; quem quiser segurar o run limpa o evento e o reabre depois.
    """
    registro: list[tuple[str, dict]] = []
    trava = threading.Event()
    trava.set()

    class _Falso:
        def __init__(self, **kwargs):
            registro.append(("init", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

        def run(self, *, observador, **kwargs) -> dict:
            trava.wait(5)
            registro.append(("run", kwargs))
            return dict(_STATS_OK)

    monkeypatch.setattr(coletores, "resolver", lambda alvo: _Falso)
    monkeypatch.setattr("tcc_scraper.entrega.builder.build_entrega", lambda **kw: {"balancos": 3})
    return registro, trava


def test_coletar_contrato_roda_os_itens_e_monta_a_entrega(cenario, coletor_falso):
    repo, daily, _ = cenario
    registro, _ = coletor_falso
    aplicador = Aplicador(repo, daily, HistoricoFalso(), _settings_historico())

    assert aplicador.aplicar(Comando("k1", "coletar", CONTRATO, "")) == (True, None)
    _esperar(daily)

    (eid,) = repo.execucoes
    exe = repo.obter(eid)
    assert exe["tipo"] == "coletar"
    assert exe["status"] == "concluida"
    assert [e["etapa_id"] for e in exe["etapas"]] == ["cvm_itr_dfp", "build_entrega"]
    assert ("init", {"anos": [2024, 2025]}) in registro
    assert ("run", {"resume": False}) in registro
    assert any("balancos_contas_novas" in log["mensagem"] for log in repo.logs)


def test_coletar_invalido_e_recusado_sem_criar_execucao(cenario):
    repo, daily, _ = cenario
    aplicador = Aplicador(repo, daily, HistoricoFalso(), _settings_historico())

    ok, erro = aplicador.aplicar(Comando("k2", "coletar", {"itens": [{"coletor": "xpto"}]}, ""))

    assert ok is False
    assert erro.startswith("coletor desconhecido")
    assert not repo.execucoes
    assert repo.comandos["k2"]["ok"] is False


def test_coletar_invalido_e_validado_antes_da_ocupacao(cenario):
    repo, daily, _ = cenario
    daily.criar("daily")  # execucao agendada: se a ocupacao viesse antes, a msg seria outra
    aplicador = Aplicador(repo, daily, HistoricoFalso(), _settings_historico())

    ok, erro = aplicador.aplicar(Comando("k8", "coletar", {"itens": [{"coletor": "xpto"}]}, ""))

    assert ok is False
    assert erro.startswith("coletor desconhecido")


def test_coletar_com_carga_ativa_nasce_agendada_e_roda_apos_liberar(cenario, coletor_falso):
    repo, daily, relogio = cenario
    historico = HistoricoFalso(bloqueia=True)
    aplicador = Aplicador(repo, daily, historico, _settings_historico())

    assert aplicador.aplicar(Comando("k4", "coletar", CONTRATO, "")) == (True, None)
    (eid,) = repo.execucoes
    assert repo.obter(eid)["status"] == "agendada"
    assert repo.obter(eid)["aguardando"] == "historico"
    assert not daily.ocupado

    historico.liberar()
    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(return_value=httpx.Response(200, json={"comandos": []}))
        ex = _executor(repo, daily, historico, relogio)
        ex.ciclo()
        _esperar(daily)
    assert repo.obter(eid)["status"] == "concluida"


def test_coletar_recusado_com_daily_agendado(cenario):
    repo, daily, _ = cenario
    daily.criar("daily")
    aplicador = Aplicador(repo, daily, HistoricoFalso(), _settings_historico())

    assert aplicador.aplicar(Comando("k5", "coletar", CONTRATO, "")) == (
        False,
        "ja existe uma execucao em andamento ou agendada",
    )
    assert len(repo.execucoes) == 1


def test_ocupado_com_coletar_enquanto_o_coletor_roda(cenario, coletor_falso):
    repo, daily, relogio = cenario
    _, trava = coletor_falso
    trava.clear()
    aplicador = Aplicador(repo, daily, HistoricoIndisponivel(), _settings_historico())
    ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
    try:
        assert aplicador.aplicar(Comando("k6", "coletar", CONTRATO, "")) == (True, None)
        assert ex.ocupado_com() == "coletar"
    finally:
        trava.set()
        _esperar(daily)
    assert ex.ocupado_com() is None


def test_coletar_invalido_chega_ao_painel_com_a_mensagem(cenario):
    repo, daily, relogio = cenario
    with respx.mock(assert_all_called=False) as router:
        router.post(COMANDOS).mock(
            return_value=httpx.Response(
                200, json={"comandos": [_comando("k7", "coletar", itens=[])]}
            )
        )
        ack = router.post(ACK).mock(return_value=httpx.Response(204))
        ex = _executor(repo, daily, HistoricoIndisponivel(), relogio)
        ex.ciclo()
    assert json.loads(ack.calls[-1].request.content) == {
        "id": "k7", "ok": False, "erro": "itens vazios: informe ao menos um coletor",
    }
    assert not repo.execucoes
