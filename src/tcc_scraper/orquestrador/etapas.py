"""Catalogo das 9 etapas do daily e a implementacao de cada uma.

Ids, ordem, nome e coletor sao o que o painel espera: nao renomear sem combinar com o painel.
Os imports das fases pesadas (Yahoo, FinBERT, Gemini) sao tardios, dentro das funcoes, para
importar este modulo nao carregar torch nem o cliente da LLM.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import threading

    from tcc_scraper.config import Settings
    from tcc_scraper.orquestrador.progresso import Progresso

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Contexto:
    execucao_id: str
    hoje: date
    settings: Settings
    deadline: datetime
    cancelar: threading.Event
    origem: str


@dataclass
class ResultadoEtapa:
    status: Literal["concluida", "falhou", "pulada"]
    itens: int = 0
    erro: str | None = None
    detalhes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EtapaDef:
    id: str
    ordem: int
    nome: str
    coletor: str
    run: Callable[[Contexto, Progresso], ResultadoEtapa]


def de_stats(st: dict) -> ResultadoEtapa:
    """Traduz as estatisticas de `Collector.run` em resultado de etapa."""
    if st.get("interrompido"):
        return ResultadoEtapa(
            "falhou",
            itens=st.get("itens", 0),
            erro="interrompida: orçamento ou cancelamento",
            detalhes=dict(st),
        )
    if st["total"] > 0 and st["ok"] == 0 and st["erro"] > 0:
        return ResultadoEtapa(
            "falhou",
            itens=st.get("itens", 0),
            erro="todas as unidades falharam",
            detalhes=dict(st),
        )
    return ResultadoEtapa("concluida", itens=st["itens"], detalhes=dict(st))


def etapa_precos(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.collectors.prices_yahoo import YahooPriceCollector

    with YahooPriceCollector() as c:
        st = c.run(resume=False, observador=p)
    return de_stats(st)


def etapa_macro(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.collectors.macro_bcb import BcbCollector

    with BcbCollector() as c:
        st = c.run(resume=False, observador=p)
    return de_stats(st)


def etapa_noticias(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.collectors.google_news import GoogleNewsCollector

    with GoogleNewsCollector(
        desde=ctx.hoje - timedelta(days=2), ate=ctx.hoje + timedelta(days=1)
    ) as c:
        st = c.run(resume=False, observador=p)
    return de_stats(st)


def etapa_enriquecimento(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.enrich.article_fetcher import ArticleBodyCollector

    with ArticleBodyCollector() as c:
        corpo = c.run(resume=False, observador=p)
    try:
        from tcc_scraper.sentiment.scoring import score_pending

        sentimento = score_pending(observador=p)
    except (RuntimeError, ImportError) as exc:
        return ResultadoEtapa(
            "falhou",
            itens=corpo["itens"],
            erro=f"FinBERT indisponível: {exc}"[:4000],
            detalhes={"corpo": corpo},
        )
    base = de_stats(corpo)
    if sentimento.get("interrompido") and base.status != "falhou":
        base = ResultadoEtapa("falhou", erro="interrompida: orçamento ou cancelamento")
    return ResultadoEtapa(
        base.status,
        itens=sentimento.get("gravados", 0),
        erro=base.erro,
        detalhes={"corpo": corpo, "sentimento": sentimento},
    )


def etapa_analise(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    if not ctx.settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY ausente; análise diária pulada")
        return ResultadoEtapa("pulada", erro="GEMINI_API_KEY ausente")

    from tcc_scraper.llm.job import run_daily_analysis

    r = run_daily_analysis(ctx.hoje)
    if r.status == "ok":
        return ResultadoEtapa(
            "concluida",
            itens=sum(r.linhas.values()),
            detalhes={"status": r.status, "linhas": dict(r.linhas)},
        )
    return ResultadoEtapa("falhou", erro=(r.erro or r.status)[:4000])


def etapa_nao_implementada(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    return ResultadoEtapa("pulada", erro="etapa ainda não implementada")


ETAPAS: tuple[EtapaDef, ...] = (
    EtapaDef("precos", 1, "Preços e índices", "yahoo", etapa_precos),
    EtapaDef("macro", 2, "Macro do BCB", "bcb", etapa_macro),
    EtapaDef("noticias", 3, "Notícias", "google_news", etapa_noticias),
    EtapaDef("ipe", 4, "Fatos relevantes", "cvm_ipe", etapa_nao_implementada),
    EtapaDef("enriquecimento", 5, "Corpo e sentimento", "finbert", etapa_enriquecimento),
    EtapaDef("juiza", 6, "Juíza de qualidade", "juiza", etapa_nao_implementada),
    EtapaDef("cadastro", 7, "Cadastro e eventos", "cvm_b3", etapa_nao_implementada),
    EtapaDef("entrega", 8, "Montagem das 8 tabelas", "build_entrega", etapa_nao_implementada),
    EtapaDef("analise", 9, "Análise diária", "gemini", etapa_analise),
)

POR_ID: dict[str, EtapaDef] = {e.id: e for e in ETAPAS}
