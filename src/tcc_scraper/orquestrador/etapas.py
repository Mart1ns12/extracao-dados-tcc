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
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import threading

    from tcc_scraper.collectors.base import Collector
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


def resultado_finbert(st: dict) -> ResultadoEtapa:
    """Traduz o dict de `score_pending` em resultado de etapa (usado pelo `coletar`)."""
    itens = st.get("gravados", 0)
    if st.get("interrompido"):
        return ResultadoEtapa(
            "falhou",
            itens=itens,
            erro="interrompida: orçamento ou cancelamento",
            detalhes=dict(st),
        )
    return ResultadoEtapa("concluida", itens=itens, detalhes=dict(st))


def resultado_corrigir_horas(st: dict) -> ResultadoEtapa:
    """Traduz o dict de `corrigir_horas_publicacao` em resultado de etapa (usado pelo `coletar`)."""
    return ResultadoEtapa("concluida", itens=st.get("gravados", 0), detalhes=dict(st))


def resultado_concordancia(r: Any) -> ResultadoEtapa:
    """Traduz o `ResultadoConcordancia` de `medir_concordancia` (usado pelo `coletar`)."""
    detalhes = {
        "decisao": r.decisao,
        "limiar_recomendado": r.metricas.get("limiar_recomendado"),
        "amostra": r.amostra,
        "custo_usd": r.custo_usd,
    }
    return ResultadoEtapa("concluida", itens=r.amostra, detalhes=detalhes)


def etapa_precos(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.collectors.b3_carteira_ibov import B3CarteiraIbovCollector
    from tcc_scraper.collectors.b3_indices import B3IndicesCollector
    from tcc_scraper.collectors.prices_yahoo import YahooPriceCollector

    # IDIV, SMLL e IFIX vêm da B3 (o Yahoo só tem 1 dia deles); só o ano corrente é refeito.
    # A carteira do IBOV e so um retrato do dia: grava mudancas em v1-composicao_ibov.
    return _executa_partes(
        p,
        [
            ("yahoo", lambda: _roda_coletor(YahooPriceCollector(), p, resume=False)),
            (
                "b3_indices",
                lambda: _roda_coletor(
                    B3IndicesCollector(anos=[ctx.hoje.year]), p, resume=False
                ),
            ),
            (
                "b3_carteira_ibov",
                lambda: _roda_coletor(B3CarteiraIbovCollector(), p, resume=False),
            ),
        ],
    )


def _roda_coletor(coletor: Collector, p: Progresso, **kw: Any) -> dict:
    with coletor as c:
        return c.run(observador=p, **kw)


def _executa_partes(p: Progresso, partes: list[tuple[str, Callable[[], dict]]]) -> ResultadoEtapa:
    """Roda os coletores de uma etapa em sequência; um que levanta não derruba os outros.

    Falhou se algum devolveu `interrompido` (orçamento ou cancelamento) ou se todos falharam
    (levantaram ou tiveram erro total). Caso contrário, concluída, com os itens somados.
    """
    sts: dict[str, dict] = {}
    falhas = 0
    for nome, executa in partes:
        try:
            st = executa()
        except Exception as exc:
            logger.exception("%s levantou", nome)
            p.log("erro", f"{nome} levantou: {exc}"[:4000])
            sts[nome] = {"excecao": str(exc)[:4000]}
            falhas += 1
            continue
        sts[nome] = dict(st)
        if de_stats(st).status == "falhou":
            falhas += 1
    itens = sum(st.get("itens", 0) for st in sts.values())
    if any(st.get("interrompido") for st in sts.values()):
        return ResultadoEtapa(
            "falhou", itens=itens, erro="interrompida: orçamento ou cancelamento", detalhes=sts
        )
    if falhas == len(partes):
        return ResultadoEtapa("falhou", itens=itens, erro="todas as partes falharam", detalhes=sts)
    return ResultadoEtapa("concluida", itens=itens, detalhes=sts)


def etapa_macro(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.collectors.indices import IndicesBcbCollector
    from tcc_scraper.collectors.macro_bcb import BcbCollector

    return _executa_partes(
        p,
        [
            ("bcb", lambda: _roda_coletor(BcbCollector(), p, resume=False)),
            (
                "indices_bcb",
                lambda: _roda_coletor(IndicesBcbCollector(anos=[ctx.hoje.year]), p, resume=False),
            ),
        ],
    )


def etapa_ipe(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.collectors.cvm_ipe import CvmIpeCollector

    with CvmIpeCollector(anos=[ctx.hoje.year]) as c:
        st = c.run(resume=True, observador=p)
    return de_stats(st)


def etapa_cadastro(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.collectors.b3_cadastro import B3CadastroCollector
    from tcc_scraper.collectors.b3_proventos import B3ProventosCollector
    from tcc_scraper.collectors.cvm_cadastro import CvmCadastroCollector
    from tcc_scraper.collectors.cvm_demonstracoes import CvmDemonstracoesCollector

    return _executa_partes(
        p,
        [
            ("cvm_cadastro", lambda: _roda_coletor(CvmCadastroCollector(), p, resume=False)),
            ("b3_cadastro", lambda: _roda_coletor(B3CadastroCollector(), p, resume=False)),
            ("b3_proventos", lambda: _roda_coletor(B3ProventosCollector(), p, resume=False)),
            (
                "cvm_itr_dfp",
                lambda: _roda_coletor(
                    CvmDemonstracoesCollector(anos=[ctx.hoje.year]), p, resume=True
                ),
            ),
        ],
    )


def etapa_noticias(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.collectors.google_news import GoogleNewsCollector
    from tcc_scraper.collectors.wordpress import WordPressCollector

    desde = ctx.hoje - timedelta(days=2)
    ate = ctx.hoje + timedelta(days=1)
    partes: list[tuple[str, Callable[[], dict]]] = [
        (
            "google_news",
            lambda: _roda_coletor(GoogleNewsCollector(desde=desde, ate=ate), p, resume=False),
        ),
    ]
    # WordPress e o processamento so entram com a fonte ligada; desligada, o daily e o de antes.
    if ctx.settings.wp_enabled:
        partes += [
            (
                "wordpress",
                lambda: _roda_coletor(WordPressCollector(desde=desde, ate=ate), p, resume=False),
            ),
            (
                "processar_wp",
                lambda: _roda_processar_wp(
                    p, desde=ctx.hoje - timedelta(days=3), ate=ate
                ),
            ),
        ]
    return _executa_partes(p, partes)


def _roda_processar_wp(p: Progresso, *, desde: date, ate: date) -> dict:
    from tcc_scraper.enrich.processar_wp import processar_wp

    return processar_wp(desde=desde, ate=ate, observador=p)


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
    if not ctx.settings.analise_enabled:
        logger.info("ANALISE_ENABLED=false; análise diária pulada")
        return ResultadoEtapa("pulada", erro="análise diária desativada (ANALISE_ENABLED=false)")
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


def montar_dataset(p: Progresso) -> str | None:
    """Monta o features_dataset_v1 (a mesma funcao da CLI build-dataset). Devolve o erro, se houver.

    Nao levanta: quem chama segue com a entrega e reporta o erro junto com a dela.
    """
    from tcc_scraper.features.build import build_dataset_v1

    try:
        build_dataset_v1()
    except Exception as exc:
        logger.exception("build_dataset levantou")
        p.log("erro", f"build_dataset levantou: {exc}"[:4000])
        return f"Dataset 1.0 falhou: {exc}"[:4000]
    return None


def _junta_erros(*erros: str | None) -> str | None:
    presentes = [e for e in erros if e]
    return "; ".join(presentes) or None


def montar_entrega(p: Progresso, tabelas: list[str] | None = None) -> ResultadoEtapa:
    """Monta o Dataset 1.0 e as tabelas pedidas (todas se None). Usado pelo daily e pelo `coletar`.

    O dataset roda antes. Se ele falhar, as tabelas saem do mesmo jeito e a etapa falha com as
    duas mensagens.
    """
    from tcc_scraper.db.models_entrega import COLUNAS
    from tcc_scraper.entrega.builder import build_entrega

    erro_dataset = montar_dataset(p)
    # O builder nao levanta por tabela: a que falha some do dict e o observador recebe ok=False.
    # Sem tabelas, a chamada fica identica a de antes (ha teste que confere os kwargs).
    if tabelas:
        gravadas = build_entrega(tabelas=list(tabelas), observador=p)
    else:
        gravadas = build_entrega(observador=p)
    if not gravadas:
        return ResultadoEtapa(
            "falhou",
            erro=_junta_erros(erro_dataset, "todas as tabelas falharam"),
            detalhes={"tabelas": {}},
        )
    itens = sum(gravadas.values())
    alvo = list(tabelas) if tabelas else list(COLUNAS)
    faltam = [t for t in alvo if t not in gravadas]
    erro_tabelas = f"tabelas com falha: {', '.join(faltam)}" if faltam else None
    erro = _junta_erros(erro_dataset, erro_tabelas)
    if erro:
        return ResultadoEtapa("falhou", itens=itens, erro=erro, detalhes={"tabelas": gravadas})
    return ResultadoEtapa("concluida", itens=itens, detalhes={"tabelas": gravadas})


def etapa_entrega(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    return montar_entrega(p)


def etapa_juiza(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    from tcc_scraper.llm.juiza import julgar_pendentes

    r = julgar_pendentes(
        limite_itens=ctx.settings.juiza_max_itens_dia, observador=p, modo="diario"
    )
    # Os detalhes da etapa nao chegam ao painel: a linha de log e o que fica visivel.
    avisos = f", avisos={', '.join(r.avisos)}" if r.avisos else ""
    logger.info(
        "juiza: %d lidos, %d julgados, US$ %.4f, parada=%s%s",
        r.pendentes, r.regra + r.jev + r.llm, r.custo_usd, r.parada or "nenhuma", avisos,
    )
    return resultado_juiza(r)


# Paradas em que a juiza nao julga por falta de configuracao ou de credito: a etapa falha,
# em vez de parecer concluida com zero itens.
PARADAS_FALHA = {"jev_sem_chave", "erro_jev", "sem_credito"}


def resultado_juiza(r: Any) -> ResultadoEtapa:
    itens = r.regra + r.jev + r.llm
    detalhes = {
        "pendentes": r.pendentes,
        "regra": r.regra,
        "jev": r.jev,
        "llm": r.llm,
        "provedor": r.provedor,
        "erros": r.erros,
        "invalidos": r.invalidos,
        "runs": r.runs,
        "tokens_entrada": r.tokens_entrada,
        "tokens_saida": r.tokens_saida,
        "tokens_cache": r.tokens_cache,
        "custo_usd": r.custo_usd,
        "parada": r.parada,
        "interrompido": r.interrompido,
        "avisos": r.avisos,
    }
    if r.interrompido:
        return ResultadoEtapa(
            "falhou", itens=itens, erro="interrompida: orçamento ou cancelamento", detalhes=detalhes
        )
    if r.parada in PARADAS_FALHA:
        erro = f"juiza parou: {r.parada}"
        if r.avisos:
            erro += f"; avisos: {', '.join(r.avisos)}"
        return ResultadoEtapa("falhou", itens=itens, erro=erro, detalhes=detalhes)
    return ResultadoEtapa("concluida", itens=itens, detalhes=detalhes)


def etapa_nao_implementada(ctx: Contexto, p: Progresso) -> ResultadoEtapa:
    return ResultadoEtapa("pulada", erro="etapa ainda não implementada")


ETAPAS: tuple[EtapaDef, ...] = (
    EtapaDef("precos", 1, "Preços e índices", "yahoo", etapa_precos),
    EtapaDef("macro", 2, "Macro do BCB", "bcb", etapa_macro),
    EtapaDef("noticias", 3, "Notícias", "google_news", etapa_noticias),
    EtapaDef("ipe", 4, "Fatos relevantes", "cvm_ipe", etapa_ipe),
    EtapaDef("enriquecimento", 5, "Corpo e sentimento", "finbert", etapa_enriquecimento),
    EtapaDef("juiza", 6, "Juíza de qualidade", "juiza", etapa_juiza),
    EtapaDef("cadastro", 7, "Cadastro e eventos", "cvm_b3", etapa_cadastro),
    EtapaDef(
        "entrega", 8, "Dataset 1.0 e as 11 tabelas da entrega", "build_entrega", etapa_entrega
    ),
    EtapaDef("analise", 9, "Análise diária", "gemini", etapa_analise),
)

POR_ID: dict[str, EtapaDef] = {e.id: e for e in ETAPAS}
