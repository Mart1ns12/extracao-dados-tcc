"""Carga historica: coleta ano a ano (do mais recente ao mais antigo), com pausa e cancelamento.

Roda em thread propria, ao lado do executor. Pausa e cancelamento sao checados ENTRE
unidades de trabalho (`deve_parar()` do observador): nunca interrompem uma requisicao no
meio. Os coletores sao retomaveis por checkpoint (`raw_collection_runs`), entao retomar
recomeca a unidade interrompida e segue do mesmo ponto.

O estado da carga (status e posicao) vive em `v1-painel_historico`; as flags de controle
vivem na memoria do processo. `recuperar_apos_reinicio` reconcilia os dois.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from tcc_scraper.config import Settings, get_settings
from tcc_scraper.orquestrador.coletores import CATALOGO, args_fixos

# _resolver e global deste modulo: os testes trocam H._resolver por um duble.
from tcc_scraper.orquestrador.coletores import resolver as _resolver
from tcc_scraper.orquestrador.progresso import FLUSH_SEGUNDOS, FLUSH_UNIDADES

if TYPE_CHECKING:
    from tcc_scraper.orquestrador.repositorio import Repositorio

logger = logging.getLogger(__name__)

MODOS = ("pendentes", "reprocessar_erros")
# Carga ainda nao terminou: bloqueia um disparo novo.
STATUS_ATIVOS = ("em_andamento", "pausando", "pausada", "cancelando")
# Janela da media movel de itens por minuto.
JANELA_RITMO = timedelta(minutes=10)

class HistoricoEmAndamento(Exception):
    """Ja existe uma carga historica ativa (rodando, pausada ou sendo cancelada)."""


class TransicaoInvalida(Exception):
    """A acao nao se aplica ao status atual da carga."""


@dataclass(frozen=True)
class Segmento:
    ano: int
    inicio: date
    fim: date


def segmentos(settings: Settings, anos: list[int] | None = None) -> list[Segmento]:
    """Anos civis dentro de [inicio_noticias, fim_coleta + 1 dia], do mais recente ao mais antigo.

    `fim` e o limite exclusivo do segmento: 1 de janeiro do ano seguinte, ou o fim da janela.
    """
    janela_ini = settings.inicio_noticias
    janela_fim = settings.fim_coleta + timedelta(days=1)
    filtro = set(anos) if anos is not None else None
    saida: list[Segmento] = []
    for ano in range(janela_fim.year, janela_ini.year - 1, -1):
        if filtro is not None and ano not in filtro:
            continue
        inicio = max(date(ano, 1, 1), janela_ini)
        fim = min(date(ano + 1, 1, 1), janela_fim)
        if inicio < fim:
            saida.append(Segmento(ano, inicio, fim))
    return saida


@dataclass(frozen=True)
class Passo:
    """Uma unidade de trabalho da carga: um coletor do catalogo para um ano."""

    ano: int
    coletor: str
    args: dict = field(default_factory=dict)

    def executar(self, progresso: ProgressoHistorico, modo: str) -> Any:
        return CATALOGO[self.coletor].executar(
            progresso,
            args=self.args,
            apenas_erros=modo == "reprocessar_erros",
            resolver_alvo=_resolver,
        )


class _Ritmo:
    """Media movel de itens por minuto, compartilhada pelos passos de uma carga."""

    def __init__(self) -> None:
        self._amostras: deque[tuple[datetime, int]] = deque()

    def registrar(self, agora: datetime, itens: int) -> None:
        self._amostras.append((agora, itens))

    def media(self, agora: datetime) -> float:
        corte = agora - JANELA_RITMO
        while self._amostras and self._amostras[0][0] < corte:
            self._amostras.popleft()
        if not self._amostras:
            return 0.0
        minutos = max((agora - self._amostras[0][0]).total_seconds() / 60, 1.0)
        return sum(itens for _, itens in self._amostras) / minutos


class ProgressoHistorico:
    """Observador de um passo da carga (duck-type de `ObservadorColeta`, tambem de score_pending).

    Grava o passo em `v1-painel_historico_progresso` e a posicao/ritmo em `v1-painel_historico`.
    Um UPDATE por flush; o flush acontece a cada 25 unidades ou a cada 2 s.
    Um mesmo objeto acompanha o passo inteiro, inclusive as retomadas: os contadores
    nao zeram quando a carga e pausada e retomada.
    """

    def __init__(
        self,
        repo: Repositorio,
        hist_id: str,
        passo: Passo,
        *,
        pausar: threading.Event,
        cancelar: threading.Event,
        ritmo: _Ritmo,
        agora: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.hist_id = hist_id
        self.ano = passo.ano
        self.coletor = passo.coletor
        self._pausar = pausar
        self._cancelar = cancelar
        self._ritmo = ritmo
        self._agora = agora or (lambda: datetime.now(UTC))
        self.total = 0
        self.feitas = 0
        self.erro = 0
        self.itens = 0
        self.unidade_atual: str | None = None
        self._pendentes = 0
        self._ultimo_flush = self._agora()

    # -- ObservadorColeta ---------------------------------------------------------
    def plano(self, total: int) -> None:
        # `total` e o que falta nesta rodada; somamos o que ja foi feito antes da pausa.
        self.total = self.feitas + total
        self.flush()

    def unidade(
        self, key: str, *, ok: bool, itens: int, saturada: bool, erro: str | None
    ) -> None:
        self.feitas += 1
        self.itens += itens
        if not ok:
            self.erro += 1
        self.unidade_atual = key[:128]
        agora = self._agora()
        self._ritmo.registrar(agora, itens)
        self._pendentes += 1
        if self._pendentes >= FLUSH_UNIDADES or (
            agora - self._ultimo_flush >= timedelta(seconds=FLUSH_SEGUNDOS)
        ):
            self.flush()

    def deve_parar(self) -> bool:
        return self._pausar.is_set() or self._cancelar.is_set()

    # -- uso do orquestrador ------------------------------------------------------
    def log(self, nivel: str, mensagem: str) -> None:
        self.repo.log("historico", nivel, self.coletor, mensagem, self.hist_id)

    def flush(self) -> None:
        agora = self._agora()
        self.repo.atualizar_progresso_historico(
            self.ano,
            self.coletor,
            unidades_total=self.total,
            unidades_feitas=self.feitas,
            unidades_erro=self.erro,
            itens=self.itens,
            atualizado_em=agora,
        )
        self.repo.atualizar_historico(
            self.hist_id,
            ano_atual=self.ano,
            coletor_atual=self.coletor,
            unidade_atual=self.unidade_atual,
            itens_por_min=self._ritmo.media(agora),
            atualizada_em=agora,
        )
        self._pendentes = 0
        self._ultimo_flush = agora

    def finalizar(self) -> None:
        self.flush()


def _passos(settings: Settings, anos: list[int]) -> list[Passo]:
    """Ordem da carga: globais do primeiro (mais recente) ano, depois ano a ano."""
    ordem = sorted(set(anos), reverse=True)
    primeiro = ordem[0]
    passos = [
        Passo(primeiro, "yahoo"),
        Passo(primeiro, "bcb"),
        Passo(primeiro, "indices_bcb", {"anos": ordem}),
        Passo(primeiro, "cvm_cadastro"),
        Passo(primeiro, "b3_cadastro"),
        Passo(primeiro, "b3_proventos"),
        Passo(primeiro, "cvm_itr_dfp", {"anos": ordem}),
    ]
    for seg in segmentos(settings, ordem):
        passos += [
            Passo(seg.ano, "google_news", {"desde": seg.inicio, "ate": seg.fim}),
            Passo(seg.ano, "cvm_ipe", {"anos": [seg.ano]}),
            Passo(seg.ano, "enriquecimento"),
            Passo(seg.ano, "finbert"),
            Passo(seg.ano, "juiza", args_fixos("juiza", settings)),
            Passo(seg.ano, "build_entrega"),
        ]
    return passos


def _transicao(acao: str, esperado: str, atual: str) -> TransicaoInvalida:
    return TransicaoInvalida(f"{acao} exige carga {esperado} (atual: {atual})")


class Historico:
    """Implementa `HistoricoControle`: dispara, pausa, retoma e cancela a carga em uma thread."""

    def __init__(
        self,
        repo: Repositorio,
        settings: Settings | None = None,
        agora: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings or get_settings()
        self._tz = ZoneInfo(self._settings.daily_tz)
        self._agora = agora or (lambda: datetime.now(UTC))
        # Um lock so para a sequencia "le status, decide, grava": nada de estado paralelo.
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pausar = threading.Event()
        self._cancelar = threading.Event()
        self._retomar = threading.Event()

    @property
    def ocupado(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def bloqueia(self) -> bool:
        """True enquanto a carga esta em_andamento ou pausando: o daily espera."""
        atual = self._repo.historico_atual()
        return atual is not None and atual["status"] in ("em_andamento", "pausando")

    def disparar(self, anos: list[int], modo: str = "pendentes") -> str:
        if modo not in MODOS:
            raise ValueError(f"modo inválido: {modo!r}")
        if not anos:
            raise ValueError("informe ao menos um ano")
        anos_ord = sorted({int(a) for a in anos}, reverse=True)
        with self._lock:
            atual = self._repo.historico_atual()
            if self.ocupado or (atual is not None and atual["status"] in STATUS_ATIVOS):
                status = atual["status"] if atual is not None else "em_andamento"
                ident = atual["id"] if atual is not None else "?"
                raise HistoricoEmAndamento(f"carga histórica {ident} ainda ativa ({status})")
            if not segmentos(self._settings, anos_ord):
                raise ValueError("nenhum dos anos está na janela de coleta")

            agora = self._agora()
            # Com segundos: dois disparos no mesmo minuto colidiam no mesmo id.
            hist_id = f"hist-{agora.astimezone(self._tz):%Y%m%d-%H%M%S}"
            self._repo.criar_historico(
                {
                    "id": hist_id,
                    "status": "em_andamento",
                    "modo": modo,
                    "anos": anos_ord,
                    "ano_atual": None,
                    "coletor_atual": None,
                    "unidade_atual": None,
                    "iniciada_em": agora,
                    "finalizada_em": None,
                    "itens_por_min": 0.0,
                    "erro": None,
                    "atualizada_em": agora,
                }
            )
            # Linhas "pendente" de todos os passos: o painel mostra o plano antes de rodar.
            self._repo.criar_progresso_historico(
                [
                    {
                        "ano": p.ano,
                        "coletor": p.coletor,
                        "unidades_total": 0,
                        "unidades_feitas": 0,
                        "unidades_erro": 0,
                        "itens": 0,
                        "status_segmento": "pendente",
                        "atualizado_em": agora,
                    }
                    for p in _passos(self._settings, anos_ord)
                ]
            )
            self._iniciar_thread(hist_id, anos_ord, modo)
        mensagem = f"carga {hist_id} iniciada ({modo}, anos {anos_ord})"
        self._repo.log("historico", "info", None, mensagem, hist_id)
        return hist_id

    def controlar(self, acao: str) -> str:
        with self._lock:
            atual = self._repo.historico_atual()
            if atual is None:
                raise TransicaoInvalida(f"{acao} exige uma carga histórica (nenhuma existe)")
            hist_id, status, agora = atual["id"], atual["status"], self._agora()

            if acao == "pausar":
                if status != "em_andamento":
                    raise _transicao(acao, "em_andamento", status)
                self._pausar.set()
                self._repo.atualizar_historico(hist_id, status="pausando", atualizada_em=agora)
                return "pausando"

            if acao == "retomar":
                if status != "pausada":
                    raise _transicao(acao, "pausada", status)
                self._pausar.clear()
                self._repo.atualizar_historico(
                    hist_id, status="em_andamento", atualizada_em=agora
                )
                if self.ocupado:
                    # A thread esta esperando em _aguardar: acorda e segue do mesmo ponto.
                    self._retomar.set()
                else:
                    # Processo reiniciado: a nova thread continua pela posicao gravada.
                    self._iniciar_thread(hist_id, list(atual["anos"]), atual["modo"])
                return "em_andamento"

            if acao == "cancelar":
                if status not in ("em_andamento", "pausando", "pausada"):
                    raise _transicao(acao, "em_andamento, pausando ou pausada", status)
                self._cancelar.set()
                self._retomar.set()
                if self.ocupado:
                    self._repo.atualizar_historico(
                        hist_id, status="cancelando", atualizada_em=agora
                    )
                    return "cancelando"
                # Sem thread (pausada apos reinicio): nada a esperar, encerra aqui.
                self._encerrar(hist_id, "cancelada")
                return "cancelada"

            raise TransicaoInvalida(f"ação desconhecida: {acao!r}")

    def recuperar_apos_reinicio(self) -> None:
        """Ao subir o executor: em_andamento/pausando retoma sozinha; cancelando vira cancelada."""
        with self._lock:
            atual = self._repo.historico_atual()
            if atual is None:
                return
            hist_id, status = atual["id"], atual["status"]
            if status in ("em_andamento", "pausando"):
                self._repo.atualizar_historico(
                    hist_id, status="em_andamento", atualizada_em=self._agora()
                )
                self._repo.log(
                    "historico", "aviso", None, f"carga {hist_id} retomada após reinício", hist_id
                )
                self._iniciar_thread(hist_id, list(atual["anos"]), atual["modo"])
            elif status == "cancelando":
                self._encerrar(hist_id, "cancelada")

    def join(self, timeout: float | None = None) -> None:
        """Espera a thread da carga (uso bloqueante pelo CLI)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- execucao ------------------------------------------------------------------
    def _iniciar_thread(self, hist_id: str, anos: list[int], modo: str) -> None:
        # Flags novas por thread: uma carga cancelada nao contamina a seguinte.
        self._pausar = threading.Event()
        self._cancelar = threading.Event()
        self._retomar = threading.Event()
        self._thread = threading.Thread(
            target=self._rodar, args=(hist_id, anos, modo), name=f"historico-{hist_id}", daemon=True
        )
        self._thread.start()

    def _rodar(self, hist_id: str, anos: list[int], modo: str) -> None:
        passos = _passos(self._settings, anos)
        ritmo = _Ritmo()
        try:
            for passo in passos[self._indice_inicial(hist_id, passos) :]:
                if not self._executar_passo(hist_id, passo, modo, ritmo):
                    self._encerrar(hist_id, "cancelada")
                    return
            self._encerrar(hist_id, "concluida")
        except Exception as exc:
            # Excecao fora de unidade (ex.: banco fora): a carga para aqui e o erro vai ao painel.
            logger.exception("carga histórica %s falhou", hist_id)
            erro = f"{type(exc).__name__}: {exc}"[:4000]
            agora = self._agora()
            self._repo.atualizar_historico(
                hist_id, status="falhou", erro=erro, finalizada_em=agora, atualizada_em=agora
            )
            self._repo.log("historico", "erro", None, f"carga {hist_id} falhou: {erro}", hist_id)

    def _indice_inicial(self, hist_id: str, passos: list[Passo]) -> int:
        """Posicao gravada (ano_atual, coletor_atual): a retomada nao refaz os passos antes dela."""
        atual = self._repo.historico_atual()
        if atual is None or atual["id"] != hist_id or atual["ano_atual"] is None:
            return 0
        for indice, passo in enumerate(passos):
            if (passo.ano, passo.coletor) == (atual["ano_atual"], atual["coletor_atual"]):
                return indice
        return 0

    def _executar_passo(self, hist_id: str, passo: Passo, modo: str, ritmo: _Ritmo) -> bool:
        """Roda um passo ate o fim. Pausa no meio: espera retomar e roda o passo de novo.

        Devolve False se a carga foi cancelada (quem chama encerra), True se o passo concluiu.
        """
        progresso = ProgressoHistorico(
            self._repo,
            hist_id,
            passo,
            pausar=self._pausar,
            cancelar=self._cancelar,
            ritmo=ritmo,
            agora=self._agora,
        )
        while True:
            if (self._pausar.is_set() or self._cancelar.is_set()) and not self._aguardar(hist_id):
                return False
            self._repo.atualizar_historico(
                hist_id,
                ano_atual=passo.ano,
                coletor_atual=passo.coletor,
                unidade_atual=None,
                atualizada_em=self._agora(),
            )
            if not self._executar(passo, progresso, modo):
                return True

    def _executar(self, passo: Passo, progresso: ProgressoHistorico, modo: str) -> bool:
        """Roda o passo uma vez. Devolve True se o passo foi interrompido por pausa/cancelamento."""
        self._repo.atualizar_progresso_historico(
            passo.ano, passo.coletor, status_segmento="em_andamento", atualizado_em=self._agora()
        )
        try:
            resultado = passo.executar(progresso, modo)
        except Exception:
            self._repo.atualizar_progresso_historico(
                passo.ano, passo.coletor, status_segmento="falhou", atualizado_em=self._agora()
            )
            raise
        progresso.finalizar()
        interrompido = isinstance(resultado, dict) and bool(resultado.get("interrompido"))
        self._repo.atualizar_progresso_historico(
            passo.ano,
            passo.coletor,
            status_segmento="pausado" if interrompido else "concluido",
            atualizado_em=self._agora(),
        )
        return interrompido

    def _aguardar(self, hist_id: str) -> bool:
        """Grava `pausada` e espera retomar (ou cancelar). Devolve False se foi cancelada."""
        with self._lock:
            if self._cancelar.is_set():
                return False
            # Limpar ANTES de gravar `pausada`: um retomar so pode chegar depois disso,
            # entao o wakeup nunca se perde.
            self._retomar.clear()
            self._repo.atualizar_historico(hist_id, status="pausada", atualizada_em=self._agora())
        self._repo.log("historico", "info", None, f"carga {hist_id} pausada", hist_id)
        self._retomar.wait()
        return not self._cancelar.is_set()

    def _encerrar(self, hist_id: str, status: str) -> None:
        agora = self._agora()
        self._repo.atualizar_historico(
            hist_id, status=status, finalizada_em=agora, atualizada_em=agora
        )
        self._repo.log("historico", "info", None, f"carga {hist_id} {status}", hist_id)
