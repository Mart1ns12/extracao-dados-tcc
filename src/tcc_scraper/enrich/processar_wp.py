"""Processamento das noticias WordPress, sem rede (plano hibrido, fase F7).

Para cada intervalo de dias, le os posts ja baixados (raw_wp_posts) e as materias do Google
dos mesmos veiculos, e monta as historias (republicacoes entre veiculos). Por historia:
  - representante: o post de date_gmt mais antigo. Vira artigo em raw_articles (origem
    wordpress), com o corpo limpo em raw_article_bodies;
  - vinculos (staging_article_tickers): uniao dos tickers com evidencia valida ou revisavel
    de qualquer membro;
  - julgamento do codigo (v1-staging_noticias_julgamento), so nas camadas com decisao
    (classificar_par). As demais ficam sem julgamento, e a juiza as pega como pendentes;
  - duplicatas do Google apontam para o representante (v1-staging_historias).

Idempotencia: v1-staging_wp_processamento registra cada post por versao de regras e de
prompt. Historia cujos posts do intervalo ja estao registrados nao e refeita. Toda gravacao
e upsert sem atualizar, entao um julgamento ja gravado (de codigo ou de LLM) nunca muda.

planejar_wp e puro. A leitura fica em _ler_janela e a gravacao em gravar_plano.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import and_, select

from tcc_scraper.collectors.wordpress import meses_do_intervalo
from tcc_scraper.config import APELIDOS, DOMINIOS_WP, NOMES_EMPRESA, WP_SITES, universo_coleta
from tcc_scraper.db.models import Article, ArticleBody, ArticleTicker, url_hash
from tcc_scraper.db.models_fontes import (
    RawWpPost,
    StagingHistoria,
    StagingNoticiaJulgamento,
    StagingWpProcessamento,
)
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.enrich.boilerplate import JANELA_BOILER_DIAS, IndiceShingles
from tcc_scraper.enrich.casador import EvidenciaPar, construir_regras, evidencias
from tcc_scraper.enrich.historias import (
    IndiceWP,
    Ref,
    canon,
    casar,
    escolhe_representante,
    grupos_historias,
    norm_titulo,
    ordem_site,
    pares_republicacao,
)
from tcc_scraper.enrich.wp_html import limpar_html, titulo_texto
from tcc_scraper.llm.juiza import _linha_staging
from tcc_scraper.llm.juiza_codigo import REGRAS_VERSAO, classificar_par, rotulo_modelo
from tcc_scraper.llm.juiza_prompt import VERSAO
from tcc_scraper.llm.juiza_regras import nomes_por_ticker

logger = logging.getLogger(__name__)

# Posts depois do fim do intervalo entram na janela: republicacoes de um dia vizinho contam.
MARGEM_POSTS_DEPOIS_DIAS = 1
# Duplicatas do Google: published_at em [desde - 2 d, ate + 2 d).
MARGEM_GOOGLE_DIAS = 2

_SITES = {s.nome: s for s in WP_SITES}
_SITE_POR_DOMINIO = {s.dominio: s.nome for s in WP_SITES}
_NOMES = nomes_por_ticker(NOMES_EMPRESA, APELIDOS)
_CHAVE_JULGAMENTO = ["article_id", "ticker", "prompt_versao"]


# --------------------------------------------------------------------------------------
# entradas e saidas do planejamento
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PostRaw:
    """Post de raw_wp_posts, como o planejamento le (sem o ORM)."""

    site: str
    wp_id: int
    link: str
    date_gmt: dt.datetime  # UTC, aware
    titulo_html: str
    content_html: str


@dataclass(frozen=True)
class GoogleRef:
    """Artigo do Google News de um veiculo WordPress (raw_articles, origem google_news)."""

    article_id: int
    dominio: str  # source_domain
    titulo: str
    url: str | None  # resolved_url
    published_at: dt.datetime  # UTC, aware


@dataclass(frozen=True)
class ParWP:
    """Par (historia, ticker). julgamento None: a juiza decide (camada sem decisao de codigo)."""

    ticker: str
    camada: str
    julgamento: dict | None


@dataclass
class HistoriaWP:
    """Historia com representante e artigo. processar=False: so o vinculo com o Google muda."""

    rep: tuple[str, int]  # (site, wp_id) do representante
    link: str
    date_gmt: dt.datetime
    titulo: str  # titulo_texto do representante
    dominio: str
    fonte: str
    blocos: list[str]
    article_id: int | None  # artigo ja gravado em execucao anterior; None = criar
    processar: bool
    pares: list[ParWP] = field(default_factory=list)
    google: list[tuple[int, str]] = field(default_factory=list)  # (article_id, metodo)

    @property
    def tem_artigo(self) -> bool:
        return self.article_id is not None or (self.processar and bool(self.pares))


@dataclass(frozen=True)
class RegistroWP:
    """Registro de processamento de um post (v1-staging_wp_processamento)."""

    site: str
    wp_id: int
    papel: str  # representante | membro | sem_ticker
    n_pares: int  # pares da historia (0 sem ticker)
    rep: tuple[str, int]


@dataclass
class PlanoWP:
    historias: list[HistoriaWP] = field(default_factory=list)
    registros: list[RegistroWP] = field(default_factory=list)
    posts_alvo: int = 0  # posts com date_gmt em [desde, ate)


@dataclass
class _Analisado:
    post: PostRaw
    ref: Ref
    titulo: str
    blocos: list[str]
    ev: dict[str, EvidenciaPar]


@lru_cache(maxsize=1)
def _regras_padrao() -> dict:
    return construir_regras()


def _utc(dia: dt.date) -> dt.datetime:
    return dt.datetime.combine(dia, dt.time.min, tzinfo=dt.UTC)


def _analisar(posts: Sequence[PostRaw], regras: dict) -> dict[tuple[str, int], _Analisado]:
    """Limpa o HTML e calcula as evidencias de cada post.

    O boilerplate e causal: os posts entram no indice de cada veiculo em ordem de data, e o
    trecho de um post so vê os posts ate a data dele (janela de 30 dias).
    """
    unicos: dict[tuple[str, int], PostRaw] = {}
    for p in posts:
        unicos.setdefault((p.site, p.wp_id), p)
    indices: dict[str, IndiceShingles] = {}
    saida: dict[tuple[str, int], _Analisado] = {}
    for chave, p in sorted(unicos.items(), key=lambda kv: (kv[1].date_gmt, kv[0])):
        idx = indices.setdefault(p.site, IndiceShingles())
        titulo = titulo_texto(p.titulo_html)
        blocos = limpar_html(p.content_html)
        idx.adicionar(chave, p.date_gmt, blocos)
        boiler = idx.trechos(chave, blocos)
        saida[chave] = _Analisado(
            post=p,
            ref=Ref(p.site, p.wp_id, norm_titulo(p.titulo_html), canon(p.link), p.date_gmt),
            titulo=titulo,
            blocos=blocos,
            ev=evidencias(titulo, blocos, regras, boiler),
        )
    return saida


def _chave_decisor(m: _Analisado, ticker: str) -> tuple:
    """Decisor do par: titulo primeiro, depois mais mencoes no corpo, depois o mais antigo."""
    ev = m.ev[ticker]
    return (ev.n_titulo < 1, -ev.n_corpo, m.post.date_gmt, ordem_site(m.post.site), m.post.wp_id)


def _pares_do_grupo(membros: Sequence[_Analisado], rep: _Analisado) -> list[ParWP]:
    """Pares da historia. A camada e decidida pelo membro decisor, com o artigo do representante."""
    tickers = sorted({
        t for m in membros for t, ev in m.ev.items() if ev.vincula or ev.revisavel
    })
    artigo = {
        "title": rep.titulo,
        "source_domain": _SITES[rep.post.site].dominio,
        "origem": "wordpress",
        "url": rep.post.link,
    }
    pares: list[ParWP] = []
    for t in tickers:
        candidatos = [m for m in membros if t in m.ev and (m.ev[t].vincula or m.ev[t].revisavel)]
        decisor = min(candidatos, key=lambda m: _chave_decisor(m, t))
        camada, julgamento = classificar_par(decisor.ev[t], artigo, _NOMES)
        pares.append(ParWP(ticker=t, camada=camada, julgamento=julgamento))
    return pares


def planejar_wp(
    posts: Sequence[PostRaw],
    google_refs: Sequence[GoogleRef],
    ja_processados: Mapping[tuple[str, int], int | None],
    desde: dt.date,
    ate: dt.date,
    *,
    refazer: bool = False,
    regras: dict | None = None,
) -> PlanoWP:
    """Planeja historias e registros de [desde, ate), sem tocar no banco.

    posts: a janela da raw, [desde - 30 d, ate + 1 d). Posts fora de [desde, ate) entram so como
    contexto (boilerplate causal e membros de historia).
    ja_processados: {(site, wp_id): article_id ou None} dos posts ja registrados para a versao
    atual de regras e de prompt. Historia cujos posts de [desde, ate) estao todos ali nao e
    refeita. refazer=True refaz mesmo assim (os julgamentos ja gravados continuam intactos).
    """
    regras = regras if regras is not None else _regras_padrao()
    inicio, fim = _utc(desde), _utc(ate)
    analisados = _analisar(posts, regras)
    refs = [a.ref for a in analisados.values()]
    grupos = grupos_historias(refs, pares_republicacao(refs))

    # Duplicatas do Google: cada artigo casa com no maximo um post do mesmo veiculo.
    idx = IndiceWP(refs)
    google_por_post: dict[tuple[str, int], list[tuple[int, str]]] = defaultdict(list)
    for g in google_refs:
        site = _SITE_POR_DOMINIO.get(g.dominio)
        if site is None:
            continue
        ref_g = Ref(site, g.article_id, norm_titulo(g.titulo, cortar_sufixo=True),
                    canon(g.url), g.published_at)
        metodo, post = casar(ref_g, idx)
        if post is not None:
            google_por_post[post.chave].append((g.article_id, metodo))

    alvo_total = sum(1 for a in analisados.values() if inicio <= a.post.date_gmt < fim)
    plano = PlanoWP(posts_alvo=alvo_total)
    for grupo in grupos:
        membros = [analisados[r.chave] for r in grupo]
        alvo = [m for m in membros if inicio <= m.post.date_gmt < fim]
        if not alvo:
            continue
        rep = analisados[escolhe_representante(grupo).chave]
        article_ja = ja_processados.get(rep.ref.chave)
        processar = refazer or any(m.ref.chave not in ja_processados for m in alvo)
        pares = _pares_do_grupo(membros, rep) if processar else []
        historia = HistoriaWP(
            rep=rep.ref.chave,
            link=rep.post.link,
            date_gmt=rep.post.date_gmt,
            titulo=rep.titulo,
            dominio=_SITES[rep.post.site].dominio,
            fonte=_SITES[rep.post.site].fonte,
            blocos=rep.blocos,
            article_id=article_ja,
            processar=processar,
            pares=pares,
        )
        if not historia.tem_artigo:
            # Sem ticker: nenhum artigo e nenhuma duplicata do Google. So o registro.
            if processar:
                plano.registros += [
                    RegistroWP(m.post.site, m.post.wp_id, "sem_ticker", 0, rep.ref.chave)
                    for m in membros
                ]
            continue
        historia.google = sorted(
            g for m in membros for g in google_por_post.get(m.ref.chave, [])
        )
        plano.historias.append(historia)
        if processar:
            plano.registros += [
                RegistroWP(
                    m.post.site, m.post.wp_id,
                    "representante" if m is rep else "membro",
                    len(pares), rep.ref.chave,
                )
                for m in membros
            ]
    return plano


# --------------------------------------------------------------------------------------
# gravacao (banco)
# --------------------------------------------------------------------------------------
def _linha_artigo(h: HistoriaWP, agora: dt.datetime) -> dict:
    return {
        "url_hash": url_hash(h.link),
        "url": h.link,
        "google_id": None,
        "resolved_url": h.link,
        "title": h.titulo,
        "source_name": h.fonte,
        "source_domain": h.dominio,
        "published_at": h.date_gmt,
        "published_precision": "datetime",
        "published_time_source": "wp",
        "lang": "pt",
        "collected_at": agora,
        "origem": "wordpress",
    }


def gravar_plano(plano: PlanoWP) -> dict[str, int]:
    """Grava o plano em lote, numa transacao. Idempotente: nada que ja existe e atualizado,
    exceto os campos de historia e de processamento, que sao derivados e nao julgamento.
    """
    agora = dt.datetime.now(dt.UTC)
    universo = set(universo_coleta())
    novas = [h for h in plano.historias if h.article_id is None and h.processar and h.pares]
    contagem = {"artigos": 0, "vinculos": 0, "julgamentos": 0, "historias": 0, "registros": 0}

    with session_scope() as s:
        # 1. artigos novos (representantes), e os ids relidos pelo url_hash (sem RETURNING)
        ids_novos: dict[tuple[str, int], int] = {}
        if novas:
            bulk_upsert(
                s, Article.__table__, [_linha_artigo(h, agora) for h in novas],
                index_elements=["url_hash"], update_columns=None,
            )
            s.flush()
            hash_por_rep = {h.rep: url_hash(h.link) for h in novas}
            por_hash = {
                hsh: aid for aid, hsh in s.execute(
                    select(Article.id, Article.url_hash).where(
                        Article.url_hash.in_(list(set(hash_por_rep.values())))
                    )
                ).all()
            }
            ids_novos = {rep: por_hash[hsh] for rep, hsh in hash_por_rep.items()}
            contagem["artigos"] = len(novas)

        aid = {
            h.rep: h.article_id if h.article_id is not None else ids_novos[h.rep]
            for h in plano.historias if h.tem_artigo
        }
        contagem["historias"] = len(plano.historias)

        # 2. corpo e vinculos (so historias processadas agora; reprocessar nao sobrescreve)
        corpos = [
            {"article_id": aid[h.rep], "body_text": "\n\n".join(h.blocos),
             "extraction_method": "wp_rest", "fetched_at": agora}
            for h in plano.historias if h.processar and h.blocos
        ]
        bulk_upsert(s, ArticleBody.__table__, corpos, index_elements=["article_id"],
                    update_columns=None)
        vinculos = [
            {"article_id": aid[h.rep], "ticker": p.ticker}
            for h in plano.historias if h.processar for p in h.pares
        ]
        bulk_upsert(s, ArticleTicker.__table__, vinculos,
                    index_elements=["article_id", "ticker"], update_columns=None)
        contagem["vinculos"] = len(vinculos)

        # 3. julgamento do codigo: so os pares com decisao. Upsert sem atualizar, entao um
        # julgamento de LLM ou de codigo ja gravado para o mesmo (artigo, ticker, versao) fica.
        julgamentos = [
            _linha_staging(
                {"id": aid[h.rep], "ticker": p.ticker}, p.julgamento,
                modelo=rotulo_modelo(p.camada), run_id=None, universo=universo, agora=agora,
            )
            for h in plano.historias if h.processar
            for p in h.pares if p.julgamento is not None
        ]
        bulk_upsert(s, StagingNoticiaJulgamento.__table__, julgamentos,
                    index_elements=_CHAVE_JULGAMENTO, update_columns=None)
        contagem["julgamentos"] = len(julgamentos)

        # 4. historias: o representante e a propria linha; duplicatas do Google apontam para ele
        historias = []
        for h in plano.historias:
            rep_id = aid[h.rep]
            historias.append({
                "article_id": rep_id, "historia_article_id": rep_id,
                "metodo": "representante", "regras_versao": REGRAS_VERSAO,
                "atualizado_em": agora,
            })
            for gid, metodo in h.google:
                historias.append({
                    "article_id": gid, "historia_article_id": rep_id,
                    "metodo": metodo, "regras_versao": REGRAS_VERSAO,
                    "atualizado_em": agora,
                })
        bulk_upsert(
            s, StagingHistoria.__table__, historias, index_elements=["article_id"],
            update_columns=["historia_article_id", "metodo", "regras_versao", "atualizado_em"],
        )

        # 5. registro de processamento por post
        registros = [
            {
                "site": r.site, "wp_id": r.wp_id,
                "regras_versao": REGRAS_VERSAO, "prompt_versao": VERSAO,
                "article_id": aid.get(r.rep) if r.papel == "representante" else None,
                "papel_historia": r.papel, "n_pares": r.n_pares, "processado_em": agora,
            }
            for r in plano.registros
        ]
        bulk_upsert(
            s, StagingWpProcessamento.__table__, registros, index_elements=["site", "wp_id"],
            update_columns=["regras_versao", "prompt_versao", "article_id", "papel_historia",
                            "n_pares", "processado_em"],
        )
        contagem["registros"] = len(registros)
    return contagem


# --------------------------------------------------------------------------------------
# leitura e funcao publica
# --------------------------------------------------------------------------------------
def _ler_janela(
    ini: dt.date, fim: dt.date
) -> tuple[list[PostRaw], list[GoogleRef], dict[tuple[str, int], int | None]]:
    """Posts da janela [ini - 30 d, fim + 1 d), o ja processado e as duplicatas do Google."""
    ini_w = _utc(ini - dt.timedelta(days=JANELA_BOILER_DIAS))
    fim_w = _utc(fim + dt.timedelta(days=MARGEM_POSTS_DEPOIS_DIAS))
    g_ini = _utc(ini - dt.timedelta(days=MARGEM_GOOGLE_DIAS))
    g_fim = _utc(fim + dt.timedelta(days=MARGEM_GOOGLE_DIAS))
    with session_scope() as s:
        posts = [
            PostRaw(r.site, r.wp_id, r.link, r.date_gmt, r.titulo_html, r.content_html)
            for r in s.execute(
                select(RawWpPost).where(RawWpPost.date_gmt >= ini_w, RawWpPost.date_gmt < fim_w)
            ).scalars()
        ]
        ja = {
            (site, wp_id): aid
            for site, wp_id, aid in s.execute(
                select(StagingWpProcessamento.site, StagingWpProcessamento.wp_id,
                       StagingWpProcessamento.article_id)
                .select_from(StagingWpProcessamento)
                .join(RawWpPost, and_(
                    RawWpPost.site == StagingWpProcessamento.site,
                    RawWpPost.wp_id == StagingWpProcessamento.wp_id,
                ))
                .where(
                    RawWpPost.date_gmt >= ini_w, RawWpPost.date_gmt < fim_w,
                    StagingWpProcessamento.regras_versao == REGRAS_VERSAO,
                    StagingWpProcessamento.prompt_versao == VERSAO,
                )
            ).all()
        }
        google = [
            GoogleRef(aid, dominio, titulo or "", url, pub)
            for aid, dominio, titulo, url, pub in s.execute(
                select(Article.id, Article.source_domain, Article.title,
                       Article.resolved_url, Article.published_at).where(
                    Article.origem == "google_news",
                    Article.source_domain.in_(sorted(DOMINIOS_WP)),
                    Article.published_at >= g_ini,
                    Article.published_at < g_fim,
                )
            ).all()
        ]
    return posts, google, ja


def processar_wp(
    desde: dt.date,
    ate: dt.date,
    observador: Any = None,
    refazer: bool = False,
) -> dict[str, int]:
    """Processa os posts de [desde, ate), um mes por vez. ate e exclusivo.

    Segue o contrato de Collector.run: devolve total, ok, erro, itens, saturadas e interrompido.
    Cada mes e uma unidade; uma falha em um mes nao para os demais.
    """
    unidades = list(meses_do_intervalo(desde, ate))
    stats = {"total": len(unidades), "ok": 0, "erro": 0, "itens": 0, "saturadas": 0,
             "interrompido": 0}
    if observador is not None:
        observador.plano(len(unidades))
    for ini, fim in unidades:
        chave = f"processar_wp|{ini:%Y-%m-%d}|{fim:%Y-%m-%d}"
        if observador is not None and observador.deve_parar():
            stats["interrompido"] = 1
            restantes = len(unidades) - stats["ok"] - stats["erro"]
            logger.warning("processar_wp: parada solicitada; %d meses nao executados", restantes)
            break
        try:
            posts, google, ja = _ler_janela(ini, fim)
            plano = planejar_wp(posts, google, ja, ini, fim, refazer=refazer)
            gravado = gravar_plano(plano)
        except Exception as exc:
            logger.error("processar_wp: unidade %s falhou: %s", chave, exc)
            erro = str(exc)[:500]
            stats["erro"] += 1
            if observador is not None:
                observador.unidade(chave, ok=False, itens=0, saturada=False, erro=erro)
            continue
        logger.info("processar_wp %s: %d posts no intervalo, gravado %s",
                    chave, plano.posts_alvo, gravado)
        stats["ok"] += 1
        stats["itens"] += plano.posts_alvo
        if observador is not None:
            observador.unidade(chave, ok=True, itens=plano.posts_alvo, saturada=False, erro=None)
    return stats
