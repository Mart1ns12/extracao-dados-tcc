"""Progresso de uma etapa: recebe o observador do coletor e grava no painel em lotes.

Um UPDATE por flush, e o flush acontece a cada 25 unidades ou a cada 2 s. O painel
precisa de contadores vivos, mas gravar a cada unidade sobrecarregaria o banco.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tcc_scraper.orquestrador.etapas import Contexto
    from tcc_scraper.orquestrador.repositorio import Repositorio

FLUSH_UNIDADES = 25
FLUSH_SEGUNDOS = 2


class Progresso:
    """Implementa `ObservadorColeta` (e o duck-type que `score_pending` usa)."""

    def __init__(
        self,
        repo: Repositorio,
        execucao_id: str,
        etapa_id: str,
        ctx: Contexto,
        origem: str = "daily",
        *,
        agora: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.execucao_id = execucao_id
        self.etapa_id = etapa_id
        self.ctx = ctx
        self.origem = origem
        # Relogio injetavel: os testes controlam o intervalo de 2 s sem dormir.
        self._agora = agora or (lambda: datetime.now(UTC))
        self.total: int | None = None
        self.feitas = 0
        self.erro = 0
        self.saturadas = 0
        self.itens = 0
        self.ultimo_erro_unidade: str | None = None
        self.ultimo_erro_mensagem: str | None = None
        self._pendentes = 0
        self._ultimo_flush = self._agora()

    # -- ObservadorColeta ---------------------------------------------------------
    def plano(self, total: int) -> None:
        self.total = (self.total or 0) + total
        self.flush()

    def unidade(
        self, key: str, *, ok: bool, itens: int, saturada: bool, erro: str | None
    ) -> None:
        self.feitas += 1
        self.itens += itens
        if saturada:
            self.saturadas += 1
        if not ok:
            self.erro += 1
            self.ultimo_erro_unidade = key[:128]
            self.ultimo_erro_mensagem = erro
        self._pendentes += 1
        agora = self._agora()
        if self._pendentes >= FLUSH_UNIDADES or (
            agora - self._ultimo_flush >= timedelta(seconds=FLUSH_SEGUNDOS)
        ):
            self.flush()

    def deve_parar(self) -> bool:
        return self.ctx.cancelar.is_set() or self._agora() >= self.ctx.deadline

    # -- uso do orquestrador ------------------------------------------------------
    def log(self, nivel: str, mensagem: str) -> None:
        self.repo.log(self.origem, nivel, self.etapa_id, mensagem, self.execucao_id)

    def flush(self) -> None:
        agora = self._agora()
        self.repo.atualizar_etapa(
            self.execucao_id,
            self.etapa_id,
            unidades_total=self.total,
            unidades_feitas=self.feitas,
            unidades_erro=self.erro,
            unidades_saturadas=self.saturadas,
            itens=self.itens,
            ultimo_erro_unidade=self.ultimo_erro_unidade,
            ultimo_erro_mensagem=self.ultimo_erro_mensagem,
            atualizada_em=agora,
        )
        self._pendentes = 0
        self._ultimo_flush = agora

    def finalizar(self) -> None:
        self.flush()
