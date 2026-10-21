"""Loop do executor: busca comandos no painel, aplica, confirma, roda o cron e a agenda.

O heartbeat roda em thread propria. Falha de rede ou de banco nunca derruba o loop: cada
parte do ciclo e protegida e registra o erro no log.
"""

from __future__ import annotations

import importlib.metadata
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from tcc_scraper.config import Settings
from tcc_scraper.executor.agenda import deve_disparar_cron
from tcc_scraper.executor.comandos import Aplicador, HistoricoControle
from tcc_scraper.executor.painel_client import Comando, PainelClient
from tcc_scraper.orquestrador.daily import Daily
from tcc_scraper.orquestrador.historico import Historico
from tcc_scraper.orquestrador.repositorio import Repositorio

logger = logging.getLogger(__name__)

# Carga historica que ocupa a maquina. Pausada nao ocupa: o daily pode rodar.
_STATUS_HISTORICO_ATIVO = ("em_andamento", "pausando", "cancelando")


def _versao() -> str:
    try:
        return importlib.metadata.version("tcc-scraper")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def _seguro(nome: str, fn: Callable[..., Any], *args: Any) -> None:
    """Executa fn; se levantar, registra no log e segue. O loop nao pode morrer por uma etapa."""
    try:
        fn(*args)
    except Exception:
        logger.exception("executor: falha em %s", nome)


class Executor:
    def __init__(
        self,
        repo: Repositorio,
        daily: Daily,
        historico: HistoricoControle | None,
        client: PainelClient,
        settings: Settings,
        agora: Callable[[], datetime] | None = None,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repo = repo
        self._daily = daily
        self._historico = historico if historico is not None else Historico(repo, settings)
        self._client = client
        self._settings = settings
        self._agora = agora or (lambda: datetime.now(UTC))
        self._dormir = dormir
        self._aplicador = Aplicador(repo, daily, self._historico, settings)
        self._parar = threading.Event()
        self._thread_heartbeat: threading.Thread | None = None
        # Dia em que o aviso de cron falho ja saiu: o log nao repete a cada ciclo.
        self._aviso_cron: date | None = None

    # -- ciclo de vida -------------------------------------------------------------
    def iniciar(self) -> None:
        self._daily.recuperar_apos_reinicio()
        self._historico.recuperar_apos_reinicio()
        self._repo.podar_logs(self._settings.painel_logs_dias)
        self._thread_heartbeat = threading.Thread(
            target=self._loop_heartbeat, name="executor-heartbeat", daemon=True
        )
        self._thread_heartbeat.start()

    def run_forever(self) -> None:
        self.iniciar()
        try:
            while not self._parar.is_set():
                self.ciclo()
                self._dormir(self._settings.executor_poll_s)
        except KeyboardInterrupt:
            logger.info("executor interrompido (Ctrl+C)")
        finally:
            self.parar()

    def parar(self) -> None:
        self._parar.set()

    # -- um ciclo ------------------------------------------------------------------
    def ciclo(self) -> None:
        comandos = self._client.comandos()
        for cmd in comandos or []:
            _seguro(f"comando {cmd.id}", self._aplicar_e_confirmar, cmd)
        _seguro("agenda do cron", self._agendar_cron)
        _seguro("inicio da execucao agendada", self._iniciar_agendada)

    def ocupado_com(self) -> str | None:
        if self._historico.ocupado and self._carga_historica_ativa():
            return "historico"
        return self._daily.ocupado_com

    def _carga_historica_ativa(self) -> bool:
        try:
            atual = self._repo.historico_atual()
        except Exception:
            # O heartbeat roda em thread propria: uma falha de banco nao pode derrubá-lo.
            logger.exception("executor: nao consegui ler o status da carga historica")
            return False
        return atual is not None and atual["status"] in _STATUS_HISTORICO_ATIVO

    def bater_heartbeat(self) -> None:
        host = self._settings.host_executor
        versao = _versao()
        ocupado = self.ocupado_com()
        _seguro("heartbeat na API", self._client.heartbeat, host, versao, ocupado)
        _seguro("heartbeat no banco", self._repo.heartbeat, host, versao, ocupado)

    # -- internos ------------------------------------------------------------------
    def _loop_heartbeat(self) -> None:
        intervalo = self._settings.executor_heartbeat_s
        while not self._parar.wait(intervalo):
            self.bater_heartbeat()

    def _aplicar_e_confirmar(self, cmd: Comando) -> None:
        ok, erro = self._aplicador.aplicar(cmd)
        self._client.ack(cmd.id, ok, erro)

    def _agendar_cron(self) -> None:
        agora = self._agora()
        agora_sp = agora.astimezone(ZoneInfo(self._settings.daily_tz))
        hoje = agora_sp.date()
        if not deve_disparar_cron(
            agora_sp, self._settings.daily_hora, self._repo.existe_execucao_cron(hoje)
        ):
            return
        cmd = Comando(
            id=f"cron-{hoje:%Y-%m-%d}",
            tipo="daily",
            parametros={},
            criado_em=agora.isoformat(),
        )
        # Sem ack: o comando nao veio do painel. Se falhar (ex.: daily manual rodando),
        # nada e registrado e a tentativa se repete no proximo ciclo.
        ok, erro = self._aplicador.aplicar(cmd, origem="cron")
        if not ok and self._aviso_cron != hoje:
            logger.warning("cron de %s nao disparou (nova tentativa a cada ciclo): %s", hoje, erro)
            self._aviso_cron = hoje

    def _iniciar_agendada(self) -> None:
        ex = self._repo.atual()
        if ex is None or ex["status"] != "agendada":
            return
        if self._historico.bloqueia() or self._daily.ocupado:
            return
        self._daily.iniciar(ex["id"])
