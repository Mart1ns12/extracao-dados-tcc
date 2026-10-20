"""Aplica um comando do painel: traduz o `tipo` em chamada ao Daily ou a carga historica.

Idempotente: id ja aplicado devolve (True, None) sem reaplicar, porque o painel entrega
"pelo menos uma vez".
"""

from __future__ import annotations

import logging
from typing import Protocol

from tcc_scraper.config import Settings
from tcc_scraper.executor.painel_client import Comando
from tcc_scraper.orquestrador.daily import Daily, ExecucaoEmAndamento
from tcc_scraper.orquestrador.historico import Historico, HistoricoEmAndamento, TransicaoInvalida
from tcc_scraper.orquestrador.repositorio import Repositorio

logger = logging.getLogger(__name__)


class ComandoRecusado(Exception):
    """O comando e valido, mas o executor nao pode atende-lo agora."""


class HistoricoControle(Protocol):
    @property
    def ocupado(self) -> bool: ...

    def bloqueia(self) -> bool:
        """True enquanto a carga historica estiver em_andamento ou pausando."""

    def disparar(self, anos: list[int], modo: str) -> str: ...

    def controlar(self, acao: str) -> str: ...

    def recuperar_apos_reinicio(self) -> None: ...


class HistoricoIndisponivel:
    """Dublê para testes: nunca bloqueia, recusa os comandos. Producao usa `Historico`."""

    @property
    def ocupado(self) -> bool:
        return False

    def bloqueia(self) -> bool:
        return False

    def disparar(self, anos: list[int], modo: str) -> str:
        raise ComandoRecusado("carga histórica ainda não implementada")

    def controlar(self, acao: str) -> str:
        raise ComandoRecusado("carga histórica ainda não implementada")

    def recuperar_apos_reinicio(self) -> None:
        return None


class Aplicador:
    def __init__(
        self,
        repo: Repositorio,
        daily: Daily,
        historico: HistoricoControle | None,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._daily = daily
        self._historico = historico if historico is not None else Historico(repo, settings)
        self._settings = settings

    def aplicar(self, cmd: Comando, origem: str = "manual") -> tuple[bool, str | None]:
        if self._repo.comando_aplicado(cmd.id):
            return True, None
        try:
            self._despachar(cmd, origem)
            ok, erro = True, None
        except (
            ComandoRecusado,
            ExecucaoEmAndamento,
            HistoricoEmAndamento,
            TransicaoInvalida,
            ValueError,
        ) as exc:
            ok, erro = False, str(exc)
        except Exception:
            logger.exception("falha ao aplicar comando %s (%s)", cmd.id, cmd.tipo)
            ok, erro = False, "falha interna ao aplicar o comando"
        # Cron sem sucesso nao vira registro: sem ele, a tentativa se repete no proximo ciclo.
        if ok or origem != "cron":
            self._repo.registrar_comando(cmd.id, ok, erro)
        return ok, erro

    def _despachar(self, cmd: Comando, origem: str) -> None:
        tipo = cmd.tipo
        if tipo in ("daily", "etapa"):
            aguardando = "historico" if self._historico.bloqueia() else None
            etapa = cmd.parametros.get("etapa") if tipo == "etapa" else None
            execucao_id = self._daily.criar(tipo, etapa, origem=origem, aguardando=aguardando)
            if aguardando is None:
                self._daily.iniciar(execucao_id)
        elif tipo == "historico":
            self._historico.disparar(
                cmd.parametros["anos"], cmd.parametros.get("modo", "pendentes")
            )
        elif tipo in ("pausar", "retomar", "cancelar"):
            self._historico.controlar(tipo)
        else:
            raise ComandoRecusado(f"tipo desconhecido: {tipo}")
