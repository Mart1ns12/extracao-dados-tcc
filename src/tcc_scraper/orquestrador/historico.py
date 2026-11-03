"""Carga historica: coleta ano a ano (do mais recente ao mais antigo), com pausa e cancelamento.

Roda em thread propria (a coordenadora), ao lado do executor. A coordenadora sobe duas
trilhas em threads: a Google (google_news e corpo dos artigos) e a principal (o resto). A
principal so pontua e julga as noticias de um ano depois que a Google concluiu esse ano.
Pausa e cancelamento sao checados ENTRE unidades de trabalho (`deve_parar()` do observador):
nunca interrompem uma requisicao no meio. Os coletores sao retomaveis por checkpoint
(`raw_collection_runs`), entao retomar recomeca a unidade interrompida e segue do mesmo ponto.

O estado da carga (status e posicao) vive em `v1-painel_historico` e o progresso de cada
passo em `v1-painel_historico_progresso`; as flags de controle vivem na memoria do processo.
`recuperar_apos_reinicio` reconcilia os dois: os passos ja `concluido` nao se refazem.
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
TRILHA_GOOGLE = "google"
TRILHA_PRINCIPAL = "principal"
# Origens que nao dependem da trilha Google: a juiza as julga enquanto o Google coleta o ano.
ORIGENS_SEM_GOOGLE = ("wordpress", "cvm_ipe")

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
    # Trilha em que roda (ver Historico._rodar). A Google so tem google_news e enriquecimento.
    trilha: str = TRILHA_PRINCIPAL
    # True: so comeca depois que a trilha Google concluir este ano (finbert, juiza, entrega).
    depende_google: bool = False
    # Id no painel (`coletor` de v1-painel_historico_progresso); None = o proprio coletor.
    painel: str | None = None

    @property
    def id_painel(self) -> str:
        return self.painel or self.coletor

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
        # As duas trilhas registram no mesmo ritmo: a media precisa de uma fila so.
        self._lock = threading.Lock()

    def registrar(self, agora: datetime, itens: int) -> None:
        with self._lock:
            self._amostras.append((agora, itens))

    def media(self, agora: datetime) -> float:
        with self._lock:
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
        abortar: threading.Event | None = None,
        agora: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.hist_id = hist_id
        self.ano = passo.ano
        self.coletor = passo.id_painel
        self._pausar = pausar
        self._cancelar = cancelar
        self._abortar = abortar
        self._ritmo = ritmo
        self._agora = agora or (lambda: datetime.now(UTC))
        self.total = 0
        self.feitas = 0
        self.erro = 0
        self.itens = 0
        self.unidade_atual: str | None = None
        self.ultimo_erro_unidade: str | None = None
        self.ultimo_erro_mensagem: str | None = None
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
            self.ultimo_erro_unidade = key[:128]
            self.ultimo_erro_mensagem = (erro or "")[:4000]
        self.unidade_atual = key[:128]
        agora = self._agora()
        self._ritmo.registrar(agora, itens)
        self._pendentes += 1
        if not ok or self._pendentes >= FLUSH_UNIDADES or (
            agora - self._ultimo_flush >= timedelta(seconds=FLUSH_SEGUNDOS)
        ):
            self.flush()

    def inicio_unidade(self, key: str) -> None:
        """Chamado por Collector.run ANTES de cada unidade: o painel mostra a unidade em curso,
        nao a ultima concluida (uma unidade lenta parecia travada)."""
        self.unidade_atual = key[:128]
        self.flush()

    def deve_parar(self) -> bool:
        return (
            self._pausar.is_set()
            or self._cancelar.is_set()
            or (self._abortar is not None and self._abortar.is_set())
        )

    # -- uso do orquestrador ------------------------------------------------------
    def log(self, nivel: str, mensagem: str) -> None:
        self.repo.log("historico", nivel, self.coletor, mensagem, self.hist_id)

    def flush(self) -> None:
        agora = self._agora()
        campos_erro = {}
        # Objeto novo apos reinicio nao tem erro em memoria: nao apaga o ultimo erro gravado.
        if self.ultimo_erro_mensagem is not None:
            campos_erro = {
                "ultimo_erro_unidade": self.ultimo_erro_unidade,
                "ultimo_erro_mensagem": self.ultimo_erro_mensagem,
            }
        self.repo.atualizar_progresso_historico(
            self.ano,
            self.coletor,
            unidades_total=self.total,
            unidades_feitas=self.feitas,
            unidades_erro=self.erro,
            itens=self.itens,
            unidade_atual=self.unidade_atual,
            **campos_erro,
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
    """Ordem da carga: globais do primeiro (mais recente) ano, depois ano a ano, em duas trilhas.

    A trilha Google so coleta noticias do Google News e o corpo dos artigos. A principal faz o
    resto. Na principal, o FinBERT e a juiza de um ano (e a entrega e o dataset) esperam o portao
    do Google desse ano (`depende_google`); a juiza de WordPress e IPE nao espera, porque essas
    origens
    nao dependem do corpo do Google. O FinBERT e a juiza recebem `anos`: pontuam e julgam so o
    ano que chegou, e nao o backlog inteiro, que pontuaria pela manchete o que o enriquecimento
    da outra trilha ainda nao processou.
    """
    ordem = sorted(set(anos), reverse=True)
    primeiro = ordem[0]
    juiza = args_fixos("juiza", settings)
    passos = [
        Passo(primeiro, "yahoo"),
        Passo(primeiro, "b3_indices", {"anos": ordem}),
        Passo(primeiro, "bcb"),
        Passo(primeiro, "indices_bcb", {"anos": ordem}),
        Passo(primeiro, "cvm_cadastro"),
        Passo(primeiro, "b3_cadastro"),
        Passo(primeiro, "b3_proventos"),
        Passo(primeiro, "cvm_itr_dfp", {"anos": ordem}),
    ]
    for seg in segmentos(settings, ordem):
        passos += [
            Passo(seg.ano, "google_news", {"desde": seg.inicio, "ate": seg.fim},
                  trilha=TRILHA_GOOGLE),
            Passo(seg.ano, "enriquecimento", trilha=TRILHA_GOOGLE),
        ]
        # seg.fim e exclusivo (ver segmentos): vale para o WordPress e para o processamento.
        # Os 31 d antes do segmento aquecem o boilerplate causal (30 d) do processar_wp.
        if settings.wp_enabled:
            passos += [
                Passo(seg.ano, "wordpress", {
                    "desde": seg.inicio - timedelta(days=31), "ate": seg.fim,
                }),
                Passo(seg.ano, "processar_wp", {"desde": seg.inicio, "ate": seg.fim}),
            ]
        passos += [
            Passo(seg.ano, "cvm_ipe", {"anos": [seg.ano]}),
            Passo(seg.ano, "juiza",
                  {"anos": [seg.ano], "origens": list(ORIGENS_SEM_GOOGLE), **juiza},
                  painel="juiza_wp"),
            Passo(seg.ano, "finbert", {"anos": [seg.ano]}, depende_google=True),
            Passo(seg.ano, "juiza", {"anos": [seg.ano], **juiza}, depende_google=True),
            Passo(seg.ano, "build_entrega", depende_google=True),
            Passo(seg.ano, "build_dataset", depende_google=True),
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
        # Um lock para a sequencia "le status, decide, grava" e para o estado das trilhas.
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pausar = threading.Event()
        self._cancelar = threading.Event()
        # Espera de pausa: Condition sobre o MESMO lock; `_geracao` sobe a cada retomar,
        # cancelar ou abortar, entao nenhuma trilha perde (nem repete) o acordar.
        self._cond = threading.Condition(self._lock)
        self._geracao = 0
        self._abortar = threading.Event()
        self._vivas = 0  # trilhas ainda rodando
        self._paradas = 0  # trilhas estacionadas em _aguardar
        self._falha: str | None = None
        # Falha da trilha Google: so ela para (a principal segue); a carga termina `falhou`.
        self._falha_google: str | None = None
        # A trilha Google encerrou (falha, cancelamento ou fim): portao que nao abriu nao abre.
        self._google_encerrada = False

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
                        "coletor": p.id_painel,
                        "unidades_total": 0,
                        "unidades_feitas": 0,
                        "unidades_erro": 0,
                        "itens": 0,
                        "status_segmento": "pendente",
                        "unidade_atual": None,
                        "ultimo_erro_unidade": None,
                        "ultimo_erro_mensagem": None,
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
                    # As trilhas estao esperando em _aguardar: acorda e seguem do mesmo ponto.
                    self._acordar()
                else:
                    # Processo reiniciado: a nova thread continua pela posicao gravada.
                    self._iniciar_thread(hist_id, list(atual["anos"]), atual["modo"])
                return "em_andamento"

            if acao == "cancelar":
                if status not in ("em_andamento", "pausando", "pausada"):
                    raise _transicao(acao, "em_andamento, pausando ou pausada", status)
                self._cancelar.set()
                self._acordar()
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
        """Chamar com self._lock em maos. Flags novas por thread: uma carga cancelada nao
        contamina a seguinte."""
        self._pausar = threading.Event()
        self._cancelar = threading.Event()
        self._abortar = threading.Event()
        self._vivas, self._paradas, self._falha = 0, 0, None
        self._falha_google, self._google_encerrada = None, False
        self._thread = threading.Thread(
            target=self._rodar, args=(hist_id, anos, modo), name=f"historico-{hist_id}", daemon=True
        )
        self._thread.start()

    def _acordar(self) -> None:
        """Chamar com self._lock em maos: acorda todas as trilhas estacionadas em _aguardar."""
        self._geracao += 1
        self._cond.notify_all()

    def _rodar(self, hist_id: str, anos: list[int], modo: str) -> None:
        """Coordenador: roda as duas trilhas em threads e grava o status final."""
        try:
            passos = _passos(self._settings, anos)
            feitos = self._concluidos(passos)
            ritmo = _Ritmo()
            portoes = {p.ano: threading.Event() for p in passos}
            trilhas = {
                t: [p for p in passos if p.trilha == t]
                for t in (TRILHA_GOOGLE, TRILHA_PRINCIPAL)
            }
            # Ano sem passo Google nao tem portao a liberar: a principal nao espera por ele.
            anos_google = {p.ano for p in trilhas[TRILHA_GOOGLE]}
            for ano, portao in portoes.items():
                if ano not in anos_google:
                    portao.set()
            with self._lock:
                self._vivas, self._paradas = len(trilhas), 0
            threads = [
                threading.Thread(
                    target=self._rodar_trilha,
                    args=(hist_id, nome, lista, modo, ritmo, feitos, portoes),
                    name=f"historico-{hist_id}-{nome}",
                    daemon=True,
                )
                for nome, lista in trilhas.items()
            ]
            for t in threads:
                t.start()
            for t in threads:
                while t.is_alive():
                    t.join(timeout=1.0)
        except Exception as exc:
            # Excecao fora de unidade (ex.: banco fora): a carga para aqui e o erro vai ao painel.
            logger.exception("carga histórica %s falhou", hist_id)
            with self._lock:
                self._falha = self._falha or f"{type(exc).__name__}: {exc}"[:4000]
        # Falha da Google (so a trilha) e falha geral: a carga fica incompleta e termina `falhou`.
        falhas = [f for f in (self._falha, self._falha_google) if f is not None]
        if falhas:
            erro = "; ".join(falhas)[:4000]
            agora = self._agora()
            self._repo.atualizar_historico(
                hist_id, status="falhou", erro=erro, finalizada_em=agora, atualizada_em=agora
            )
            mensagem = f"carga {hist_id} falhou: {erro}"
            self._repo.log("historico", "erro", None, mensagem, hist_id)
        elif self._cancelar.is_set():
            self._encerrar(hist_id, "cancelada")
        else:
            self._encerrar(hist_id, "concluida")

    def _concluidos(self, passos: list[Passo]) -> set[tuple[int, str]]:
        """(ano, id no painel) ja `concluido` nesta carga: a retomada nao os refaz.

        O disparo grava todas as linhas como `pendente`; o que esta `concluido` foi concluido
        por esta carga, antes de um reinicio do processo.
        """
        feitos: set[tuple[int, str]] = set()
        for ano in sorted({p.ano for p in passos}):
            for linha in self._repo.progresso_historico(ano):
                if linha["status_segmento"] == "concluido":
                    feitos.add((ano, linha["coletor"]))
        return feitos

    def _rodar_trilha(self, hist_id, trilha, passos, modo, ritmo, feitos, portoes) -> None:
        """Passos de uma trilha, em ordem. A Google libera o ano (portao) ao terminar o ultimo
        passo dele; a principal espera o portao antes dos passos `depende_google`.

        Falha da Google para so a propria trilha: a principal segue e os passos com portao
        daquele ano ficam pendentes (nao rodam). Falha da principal aborta as duas.
        """
        ultimo_do_ano = {p.ano: p for p in passos}
        # Passo em curso: se ele falhar, o log do painel diz qual foi.
        em_curso: Passo | None = None
        try:
            for passo in passos:
                if passo.depende_google:
                    if not self._esperar_portao(hist_id, portoes[passo.ano]):
                        return
                    if not portoes[passo.ano].is_set():
                        # A Google nao concluiu o ano: o passo nao roda e fica como esta.
                        if (passo.ano, passo.id_painel) not in feitos:
                            self._repo.log(
                                "historico", "aviso", passo.id_painel,
                                f"{passo.id_painel} de {passo.ano} não rodou: a trilha google "
                                "não concluiu o ano",
                                hist_id,
                            )
                        continue
                if (passo.ano, passo.id_painel) in feitos:
                    self._log_passo(
                        hist_id, trilha, passo, "pulado: já constava como concluído nesta carga"
                    )
                else:
                    em_curso = passo
                    self._log_passo(hist_id, trilha, passo, "iniciado")
                    progresso = self._executar_passo(hist_id, passo, modo, ritmo)
                    if progresso is None:
                        return
                    em_curso = None
                    resumo = (
                        f": {progresso.feitas} de {progresso.total} unidades"
                        if progresso.total
                        else ""
                    )
                    self._log_passo(hist_id, trilha, passo, f"concluído{resumo}")
                if trilha == TRILHA_GOOGLE and ultimo_do_ano[passo.ano] is passo:
                    portoes[passo.ano].set()
        except Exception as exc:
            if trilha == TRILHA_GOOGLE:
                # So a Google para: sem _abortar, a principal segue ate o fim.
                logger.exception("carga histórica %s: trilha google falhou", hist_id)
                with self._lock:
                    self._falha_google = f"trilha google: {type(exc).__name__}: {exc}"[:4000]
            else:
                logger.exception("carga histórica %s: trilha %s falhou", hist_id, trilha)
                with self._lock:
                    if self._falha is None:
                        self._falha = f"{type(exc).__name__}: {exc}"[:4000]
                    self._abortar.set()
                    self._acordar()
            # Depois dos flags: falha ao gravar o log nao pode impedir a propagacao da falha.
            if em_curso is not None:
                self._log_passo(
                    hist_id, trilha, em_curso, f"falhou: {type(exc).__name__}: {exc}"[:4000],
                    nivel="erro",
                )
        finally:
            with self._lock:
                if trilha == TRILHA_GOOGLE:
                    self._google_encerrada = True
                self._vivas -= 1
                if not (self._cancelar.is_set() or self._abortar.is_set()):
                    self._marcar_pausada_se_todas(hist_id)

    def _log_passo(
        self, hist_id: str, trilha: str, passo: Passo, situacao: str, nivel: str = "info"
    ) -> None:
        """Uma linha no log do painel por passo (nunca por unidade). hist_id vai em execucao_id."""
        self._repo.log(
            "historico",
            nivel,
            passo.id_painel,
            f"passo {passo.id_painel} de {passo.ano} (trilha {trilha}) {situacao}",
            hist_id,
        )

    def _esperar_portao(self, hist_id: str, portao: threading.Event) -> bool:
        """Espera a trilha Google liberar o ano. Pausa no meio: estaciona como as demais.
        Devolve False se a carga foi cancelada ou abortada. Se a Google encerrar sem liberar o
        ano (falha), devolve True com o portao fechado: quem chama confere `portao.is_set()`.
        Sem acordar por `_acordar` aqui: acordar uma trilha pausada faria ela rodar pausada."""
        while not portao.is_set():
            if self._abortar.is_set() or self._cancelar.is_set():
                return False
            if self._google_encerrada:
                return True
            if self._pausar.is_set() and not self._aguardar(hist_id):
                return False
            portao.wait(timeout=0.5)
        return True

    def _executar_passo(
        self, hist_id: str, passo: Passo, modo: str, ritmo: _Ritmo
    ) -> ProgressoHistorico | None:
        """Roda um passo ate o fim. Pausa no meio: espera retomar e roda o passo de novo.

        Devolve None se a carga foi cancelada ou abortada (quem chama encerra), e o progresso do
        passo (contadores para o log) se o passo concluiu.
        """
        progresso = ProgressoHistorico(
            self._repo,
            hist_id,
            passo,
            pausar=self._pausar,
            cancelar=self._cancelar,
            ritmo=ritmo,
            abortar=self._abortar,
            agora=self._agora,
        )
        while True:
            if self._abortar.is_set():
                return None
            if (self._pausar.is_set() or self._cancelar.is_set()) and not self._aguardar(hist_id):
                return None
            self._repo.atualizar_historico(
                hist_id,
                ano_atual=passo.ano,
                coletor_atual=passo.id_painel,
                unidade_atual=None,
                atualizada_em=self._agora(),
            )
            if not self._executar(passo, progresso, modo):
                return progresso

    def _executar(self, passo: Passo, progresso: ProgressoHistorico, modo: str) -> bool:
        """Roda o passo uma vez. Devolve True se o passo foi interrompido por pausa/cancelamento."""
        self._repo.atualizar_progresso_historico(
            passo.ano, passo.id_painel, status_segmento="em_andamento", atualizado_em=self._agora()
        )
        try:
            resultado = passo.executar(progresso, modo)
        except Exception as exc:
            self._repo.atualizar_progresso_historico(
                passo.ano,
                passo.id_painel,
                status_segmento="falhou",
                ultimo_erro_mensagem=f"{type(exc).__name__}: {exc}"[:4000],
                atualizado_em=self._agora(),
            )
            raise
        progresso.finalizar()
        interrompido = isinstance(resultado, dict) and bool(resultado.get("interrompido"))
        # Reprocessar so refaz as unidades com erro: total 0 nao quer dizer que o ano foi coletado.
        if (
            modo == "reprocessar_erros"
            and isinstance(resultado, dict)
            and resultado.get("total") == 0
        ):
            progresso.log(
                "aviso",
                f"carga {progresso.hist_id}: {passo.ano} {passo.id_painel}: nenhuma unidade com "
                "erro; nada reprocessado (não significa que o ano foi coletado; use o modo "
                "pendentes)",
            )
        # Concluido nao tem unidade em curso: o painel nao deve mostrar a ultima como "rodando".
        extra = {} if interrompido else {"unidade_atual": None}
        self._repo.atualizar_progresso_historico(
            passo.ano,
            passo.id_painel,
            status_segmento="pausado" if interrompido else "concluido",
            atualizado_em=self._agora(),
            **extra,
        )
        return interrompido

    def _aguardar(self, hist_id: str) -> bool:
        """Estaciona a trilha ate retomar, cancelar ou abortar. `pausada` so e gravada quando
        todas as trilhas vivas estao estacionadas. Devolve False se cancelada ou abortada."""
        with self._lock:
            if self._cancelar.is_set() or self._abortar.is_set():
                return False
            geracao = self._geracao
            self._paradas += 1
            self._marcar_pausada_se_todas(hist_id)
            # Espera com timeout: reavalia a condicao mesmo que um acordar se perca.
            while self._geracao == geracao:
                self._cond.wait(timeout=1.0)
            self._paradas -= 1
            return not (self._cancelar.is_set() or self._abortar.is_set())

    def _marcar_pausada_se_todas(self, hist_id: str) -> None:
        """Chamar com self._lock. Grava `pausada` quando todas as trilhas vivas estao paradas."""
        if self._pausar.is_set() and self._vivas > 0 and self._paradas >= self._vivas:
            self._repo.atualizar_historico(hist_id, status="pausada", atualizada_em=self._agora())
            self._repo.log("historico", "info", None, f"carga {hist_id} pausada", hist_id)

    def _encerrar(self, hist_id: str, status: str) -> None:
        agora = self._agora()
        self._repo.atualizar_historico(
            hist_id, status=status, finalizada_em=agora, atualizada_em=agora
        )
        self._repo.log("historico", "info", None, f"carga {hist_id} {status}", hist_id)
