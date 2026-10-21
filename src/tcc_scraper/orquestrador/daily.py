"""Orquestra uma execucao: roda as etapas em ordem, sob orcamento e cancelamento.

Nao tem HTTP nem loop de polling -- quem chama (o executor, o CLI) decide quando criar
e iniciar. Uma execucao por vez: `criar`/`iniciar` recusam se ja houver uma ativa.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tcc_scraper.config import Settings, get_settings
from tcc_scraper.orquestrador import etapas as _etapas
from tcc_scraper.orquestrador.etapas import Contexto, EtapaDef, ResultadoEtapa
from tcc_scraper.orquestrador.progresso import Progresso
from tcc_scraper.orquestrador.repositorio import Repositorio

logger = logging.getLogger(__name__)

# Etapas que rodam mesmo com o orcamento estourado: publicar o dia e a analise
# nao podem ficar de fora por causa de uma coleta que demorou.
ETAPAS_SEM_CORTE = ("entrega", "analise")

# Execucao do comando `coletar`: sem orcamento (itens historicos levam horas).
SEM_PRAZO = datetime.max.replace(tzinfo=UTC)
MOTIVO_SEM_PLANO = "plano do coletar perdido no reinício do executor; reenvie o comando"

# Estado da execucao corrente, lido pelo handler de logging. E global ao processo (nao
# threading.local) porque o ArticleBodyCollector loga a partir de um ThreadPoolExecutor:
# logs de worker cairiam fora de uma variavel por thread. Uma execucao por vez torna isso seguro.
_ESTADO_LOG: dict = {"repo": None, "execucao_id": None, "etapa": None}
_ESTADO_LOCK = threading.Lock()
_HANDLER_INSTALADO = False


class ExecucaoEmAndamento(Exception):
    """Ja existe uma execucao ativa (rodando ou agendada)."""


class _EncaminhaParaPainel(logging.Handler):
    """Manda WARNING+ de `tcc_scraper` para a tabela de logs do painel."""

    def emit(self, record: logging.LogRecord) -> None:
        repo = _ESTADO_LOG["repo"]
        if repo is None:
            return
        try:
            nivel = "erro" if record.levelno >= logging.ERROR else "aviso"
            repo.log(
                "daily",
                nivel,
                _ESTADO_LOG["etapa"],
                record.getMessage(),
                _ESTADO_LOG["execucao_id"],
            )
        except Exception:
            # Falha ao gravar log nao pode derrubar a coleta nem gerar loop de logging.
            pass


def _instalar_handler() -> None:
    global _HANDLER_INSTALADO
    with _ESTADO_LOCK:
        if _HANDLER_INSTALADO:
            return
        handler = _EncaminhaParaPainel(level=logging.WARNING)
        logging.getLogger("tcc_scraper").addHandler(handler)
        _HANDLER_INSTALADO = True


def _agora_utc() -> datetime:
    return datetime.now(UTC)


class Daily:
    def __init__(
        self,
        repo: Repositorio,
        settings: Settings | None = None,
        agora: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings or get_settings()
        self._agora = agora or _agora_utc
        self._thread: threading.Thread | None = None
        self._cancelar = threading.Event()
        self._lock = threading.Lock()
        # Plano do `coletar` por execucao. So em memoria: nao ha coluna de parametros no painel.
        self._planos: dict[str, tuple[EtapaDef, ...]] = {}
        self._tipo_rodando: str | None = None
        _instalar_handler()

    @property
    def ocupado(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def ocupado_com(self) -> str | None:
        """'coletar' ou 'daily' (daily e etapa) enquanto a thread roda; None parado."""
        return (self._tipo_rodando or "daily") if self.ocupado else None

    def _catalogo(self) -> dict[str, EtapaDef]:
        # Lido na hora (nao importado): os testes trocam `etapas.ETAPAS`.
        return {e.id: e for e in _etapas.ETAPAS}

    def criar(
        self,
        tipo: str,
        etapa: str | None = None,
        origem: str = "manual",
        aguardando: str | None = None,
    ) -> str:
        defs = self._catalogo()
        if tipo not in ("daily", "etapa"):
            raise ValueError(f"tipo invalido: {tipo!r}")
        if tipo == "etapa":
            if etapa not in defs:
                raise ValueError(f"etapa invalida: {etapa!r}")
            alvo = (defs[etapa],)
        else:
            if etapa is not None:
                raise ValueError("etapa so se aplica ao tipo 'etapa'")
            alvo = tuple(_etapas.ETAPAS)
        return self._registrar(tipo, etapa, alvo, origem, aguardando)

    def criar_coletar(
        self,
        etapas: tuple[EtapaDef, ...],
        origem: str = "manual",
        aguardando: str | None = None,
    ) -> str:
        """Execucao avulsa do comando `coletar`: as etapas vem do plano, nao do catalogo."""
        if not etapas:
            raise ValueError("coletar sem etapas")
        return self._registrar("coletar", None, tuple(etapas), origem, aguardando)

    def _registrar(
        self,
        tipo: str,
        etapa: str | None,
        alvo: tuple[EtapaDef, ...],
        origem: str,
        aguardando: str | None,
    ) -> str:
        with self._lock:
            atual = self.repo.atual()
            ativa = atual is not None and atual["status"] in ("em_andamento", "agendada")
            if self.ocupado or ativa:
                raise ExecucaoEmAndamento("ja existe uma execucao em andamento ou agendada")

            agora = self._agora()
            tz = ZoneInfo(self.settings.daily_tz)
            agora_sp = agora.astimezone(tz)
            hoje_sp = agora_sp.date()
            execucao_id = f"{hoje_sp:%Y-%m-%d}-{tipo}-{agora_sp:%H%M%S}"
            prazo_em = datetime.combine(
                hoje_sp, time(self.settings.daily_prazo_hora), tzinfo=tz
            ).astimezone(UTC)

            execucao = {
                "id": execucao_id,
                "tipo": tipo,
                "etapa_alvo": etapa,
                "origem": origem,
                "status": "agendada",
                "aguardando": aguardando,
                "iniciada_em": agora,
                "finalizada_em": None,
                "prazo_em": prazo_em,
                "erro": None,
                "atualizada_em": agora,
            }
            etapas_linhas = [
                {
                    "execucao_id": execucao_id,
                    "etapa_id": e.id,
                    "ordem": e.ordem,
                    "status": "pendente",
                    "unidades_total": None,
                    "unidades_feitas": 0,
                    "unidades_erro": 0,
                    "unidades_saturadas": 0,
                    "itens": 0,
                    "iniciada_em": None,
                    "finalizada_em": None,
                    "ultimo_erro_unidade": None,
                    "ultimo_erro_mensagem": None,
                    "atualizada_em": agora,
                }
                for e in alvo
            ]
            self.repo.criar_execucao(execucao, etapas_linhas)
            if tipo == "coletar":
                self._planos[execucao_id] = alvo
            return execucao_id

    def iniciar(self, execucao_id: str) -> None:
        with self._lock:
            if self.ocupado:
                raise ExecucaoEmAndamento("ja existe uma execucao rodando neste processo")
            agora = self._agora()
            self.repo.atualizar_execucao(
                execucao_id,
                status="em_andamento",
                iniciada_em=agora,
                aguardando=None,
                atualizada_em=agora,
            )
            self._tipo_rodando = "coletar" if execucao_id in self._planos else "daily"
            self._cancelar = threading.Event()
            self._thread = threading.Thread(
                target=self._rodar, args=(execucao_id,), name=f"daily-{execucao_id}", daemon=True
            )
            self._thread.start()

    def executar(self, tipo: str, etapa: str | None = None, origem: str = "manual") -> str:
        execucao_id = self.criar(tipo, etapa, origem)
        self.iniciar(execucao_id)
        self._thread.join()
        return execucao_id

    def cancelar(self) -> bool:
        if not self.ocupado:
            return False
        self._cancelar.set()
        return True

    def recuperar_apos_reinicio(self) -> int:
        n = self.repo.marcar_interrompidas("interrompida por reinício do executor")
        atual = self.repo.atual()
        if (
            atual is not None
            and atual.get("tipo") == "coletar"
            and atual["status"] == "agendada"
            and atual["id"] not in self._planos
        ):
            self._falhar_sem_plano(atual)
        return n

    # -- execucao ------------------------------------------------------------------
    def _rodar(self, execucao_id: str) -> None:
        exe = self.repo.obter(execucao_id)
        cancelar = self._cancelar
        if exe.get("tipo") == "coletar":
            plano = self._planos.pop(execucao_id, None)
            if plano is None:
                self._falhar_sem_plano(exe)
                return
            defs = {e.id: e for e in plano}
            deadline = SEM_PRAZO
        else:
            defs = self._catalogo()
            deadline = exe["iniciada_em"] + timedelta(minutes=self.settings.daily_orcamento_min)
        ctx = Contexto(
            execucao_id=execucao_id,
            hoje=self.settings.fim_coleta,
            settings=self.settings,
            deadline=deadline,
            cancelar=cancelar,
            origem=exe["origem"],
        )
        estados: list[str] = []
        _ESTADO_LOG.update(repo=self.repo, execucao_id=execucao_id, etapa=None)
        try:
            self.repo.log("daily", "info", None, f"execução {execucao_id} iniciada", execucao_id)
            for reg in exe["etapas"]:
                ed = defs.get(reg["etapa_id"])
                if ed is None:
                    self._pular(execucao_id, reg["etapa_id"], "etapa desconhecida")
                    estados.append("pulada")
                    continue
                estados.append(self._executar_etapa(ctx, ed, deadline))
        finally:
            _ESTADO_LOG.update(repo=None, execucao_id=None, etapa=None)

        fim = self._agora()
        cancelada = cancelar.is_set()
        if cancelada or "concluida" not in estados:
            final = "falhou"
        elif "falhou" in estados:
            final = "parcial"
        else:
            final = "concluida"
        campos = {"status": final, "finalizada_em": fim, "atualizada_em": fim}
        if cancelada:
            campos["erro"] = "cancelada"
        self.repo.atualizar_execucao(execucao_id, **campos)
        self.repo.log("daily", "info", None, f"execução {execucao_id} {final}", execucao_id)

    def _executar_etapa(self, ctx: Contexto, ed: EtapaDef, deadline: datetime) -> str:
        eid = ctx.execucao_id
        if ctx.cancelar.is_set():
            self._pular(eid, ed.id, "cancelada")
            return "pulada"
        if self._agora() >= deadline and ed.id not in ETAPAS_SEM_CORTE:
            self._pular(eid, ed.id, "orçamento esgotado")
            return "pulada"

        inicio = self._agora()
        self.repo.atualizar_etapa(
            eid, ed.id, status="em_andamento", iniciada_em=inicio, atualizada_em=inicio
        )
        self.repo.log("daily", "info", ed.id, f"etapa {ed.id} iniciada", eid)

        progresso = Progresso(self.repo, eid, ed.id, ctx, origem="daily", agora=self._agora)
        _ESTADO_LOG["etapa"] = ed.id
        try:
            resultado = ed.run(ctx, progresso)
        except Exception as exc:
            resultado = ResultadoEtapa("falhou", erro=f"{type(exc).__name__}: {exc}"[:4000])
        finally:
            _ESTADO_LOG["etapa"] = None
        progresso.finalizar()

        fim = self._agora()
        campos: dict = {"status": resultado.status, "finalizada_em": fim, "atualizada_em": fim}
        if resultado.itens:
            campos["itens"] = resultado.itens
        # Sem erro por unidade, a mensagem da etapa e a unica pista para o painel.
        sem_erro_de_unidade = progresso.ultimo_erro_mensagem is None
        if resultado.erro and resultado.status != "concluida" and sem_erro_de_unidade:
            campos["ultimo_erro_mensagem"] = resultado.erro
        self.repo.atualizar_etapa(eid, ed.id, **campos)

        if resultado.status == "concluida":
            mensagem = f"etapa {ed.id} concluída: {resultado.itens} itens"
            self.repo.log("daily", "info", ed.id, mensagem, eid)
        elif resultado.status == "falhou":
            self.repo.log("daily", "erro", ed.id, f"etapa {ed.id} falhou: {resultado.erro}", eid)
        else:
            self.repo.log("daily", "info", ed.id, f"etapa {ed.id} pulada: {resultado.erro}", eid)
        return resultado.status

    def _falhar_sem_plano(self, exe: dict) -> None:
        eid = exe["id"]
        for reg in exe["etapas"]:
            if reg["status"] == "pendente":
                self._pular(eid, reg["etapa_id"], MOTIVO_SEM_PLANO)
        agora = self._agora()
        self.repo.atualizar_execucao(
            eid, status="falhou", erro=MOTIVO_SEM_PLANO, finalizada_em=agora, atualizada_em=agora
        )
        self.repo.log("daily", "erro", None, f"execução {eid}: {MOTIVO_SEM_PLANO}", eid)

    def _pular(self, execucao_id: str, etapa_id: str, motivo: str) -> None:
        agora = self._agora()
        self.repo.atualizar_etapa(
            execucao_id,
            etapa_id,
            status="pulada",
            finalizada_em=agora,
            ultimo_erro_mensagem=motivo,
            atualizada_em=agora,
        )
        self.repo.log("daily", "info", etapa_id, f"etapa {etapa_id} pulada: {motivo}", execucao_id)
