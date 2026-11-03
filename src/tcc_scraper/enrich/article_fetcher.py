"""Extracao do corpo dos artigos (estrategia hibrida).

Duas vias, nesta ordem de preferencia:

1. RSS nativo do veiculo -- InfoMoney e Money Times publicam content:encoded com o
   texto completo. E o caminho barato: uma requisicao traz varias materias inteiras.
2. Resolver o link do Google News e baixar a pagina, extraindo com trafilatura.

Restricao deliberada: so veiculos da ARTICLE_ALLOWLIST, cujo robots.txt foi
verificado. Todo o resto contribui apenas com a manchete -- que e o suficiente,
ja que manchete e o padrao da literatura de sentimento financeiro.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import zip_longest

from sqlalchemy import and_, bindparam, func, or_, select

from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.collectors.google_news import data_de_meia_noite_pacifica, inicio_do_dia_brasil
from tcc_scraper.config import ARTICLE_ALLOWLIST
from tcc_scraper.db.models import Article, ArticleBody
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.enrich.data_publicacao import extrair_data_publicacao, refinar_publicacao
from tcc_scraper.enrich.link_resolver import resolve

logger = logging.getLogger(__name__)

# Abaixo disso o "corpo" e quase certamente um teaser de paywall ou aviso de
# cookies -- pior que a manchete, porque parece texto e nao e.
MIN_BODY_CHARS = 200

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ExtractedBody:
    article_id: int
    text: str
    method: str
    real_url: str | None = None


def is_allowed(domain: str | None) -> bool:
    """True se o dominio esta na allowlist (aceita subdominios)."""
    if not domain:
        return False
    d = domain.lower().removeprefix("www.")
    return any(d == allowed or d.endswith(f".{allowed}") for allowed in ARTICLE_ALLOWLIST)


def clean_body(text: str | None) -> str | None:
    """Normaliza espacos e descarta textos curtos demais para serem materia."""
    if not text:
        return None
    normalized = _WHITESPACE_RE.sub(" ", text).strip()
    return normalized if len(normalized) >= MIN_BODY_CHARS else None


def extract_with_trafilatura(html: str) -> str | None:
    """Extrai o texto principal da pagina, sem menu, rodape e comentarios."""
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        logger.error("trafilatura ausente: pip install -e .")
        return None
    return clean_body(
        trafilatura.extract(
            html, include_comments=False, include_tables=False, favor_precision=True
        )
    )


def intercalar_por_dominio(
    pendentes: list[tuple[int, str, str | None]],
) -> list[tuple[int, str, str | None]]:
    """Reordena a fila alternando entre veiculos (round-robin).

    Sem isto a paralelizacao nao rende: os artigos chegam agrupados por veiculo, e
    medindo com dados reais um lote de 24 trazia 17 do mesmo dominio. Como o
    throttle e por dominio, os 6 workers ficavam todos na mesma fila -- o paralelo
    saiu MAIS LENTO que o sequencial (38 s contra 32 s), pagando so o custo das
    threads.

    Alternando, os primeiros N artigos cobrem N veiculos distintos e os workers
    baixam de verdade em paralelo. O piso passa a ser o veiculo mais frequente:
    nenhuma reordenacao faz o mesmo host aceitar mais que 1 requisicao por
    intervalo.
    """
    por_dominio: dict[str | None, list] = defaultdict(list)
    for artigo in pendentes:
        por_dominio[artigo[2]].append(artigo)

    return [
        artigo
        for rodada in zip_longest(*por_dominio.values())
        for artigo in rodada
        if artigo is not None
    ]


# Meia-noite em Los Angeles cai em 07:00 UTC (PDT) ou 08:00 UTC (PST). Serve so para
# pre-filtrar no SQL; a conferencia exata e feita em Python, com o fuso.
# Hora inteira, nao a string '07:00:00': no MySQL, TIME(x) IN ('07:00:00') compara como
# numero e nao casava nenhuma linha (131.690 artigos ficaram de fora ate 2026-11-03).
_HORAS_UTC_MEIA_NOITE_PACIFICA = (7, 8)
# Paginas por lote gravado na fase (b) do fix-news-times.
LOTE_HORAS_PAGINA = 200


@dataclass
class PlanoHora:
    """O que o fix-news-times vai fazer, decidido antes de qualquer gravacao.

    remarcar: (id, published_at novo) -- datetime em meia-noite pacifica vira 'date'.
    paginas: (id, resolved_url, published_at de base) -- 'date' com pagina para baixar.
    """

    remarcar: list[tuple[int, dt.datetime]]
    paginas: list[tuple[int, str, dt.datetime]]
    sem_url: int = 0
    fora_da_allowlist: int = 0


def buscar_candidatos_hora() -> list[tuple[int, dt.datetime, str, str | None, str | None]]:
    """Artigos do Google News que podem ter hora falsa: so data, ou meia-noite pacifica.

    Devolve (id, published_at, published_precision, resolved_url, source_domain).
    """
    with session_scope() as session:
        rows = session.execute(
            select(
                Article.id,
                Article.published_at,
                Article.published_precision,
                Article.resolved_url,
                Article.source_domain,
            ).where(
                Article.origem == "google_news",
                or_(
                    Article.published_precision == "date",
                    and_(
                        Article.published_precision == "datetime",
                        func.hour(Article.published_at).in_(_HORAS_UTC_MEIA_NOITE_PACIFICA),
                        func.minute(Article.published_at) == 0,
                        func.second(Article.published_at) == 0,
                    ),
                ),
            )
        ).all()
    return [tuple(r) for r in rows]


def planejar_correcao_hora(
    candidatos: Iterable[tuple[int, dt.datetime, str, str | None, str | None]],
) -> PlanoHora:
    """Decide, sem tocar no banco nem na rede, o que remarcar e o que baixar.

    Um 'datetime' so entra na remarcacao se o horario for exatamente meia-noite pacifica
    (o SQL so pre-filtra). O novo published_at e 00:00 de Sao Paulo do mesmo dia.
    """
    plano = PlanoHora(remarcar=[], paginas=[])
    for article_id, publicado_em, precisao, resolved_url, dominio in candidatos:
        base = publicado_em
        if precisao == "datetime":
            data_pacifica = data_de_meia_noite_pacifica(publicado_em)
            if data_pacifica is None:
                continue
            base = inicio_do_dia_brasil(data_pacifica)
            plano.remarcar.append((article_id, base))
        if not resolved_url:
            plano.sem_url += 1
        elif not is_allowed(dominio):
            plano.fora_da_allowlist += 1
        else:
            plano.paginas.append((article_id, resolved_url, base))
    return plano


def _gravar_horas(linhas: list[dict]) -> None:
    """UPDATE em lote de published_at, precisao e origem da hora, por id.

    Uma ida ao banco para o lote inteiro (executemany), como manda a convencao do projeto.
    """
    if not linhas:
        return
    with session_scope() as session:
        session.execute(
            Article.__table__.update()
            .where(Article.__table__.c.id == bindparam("_id"))
            .values(
                published_at=bindparam("_hora"),
                published_precision=bindparam("_precisao"),
                published_time_source=bindparam("_origem"),
            ),
            linhas,
        )


def corrigir_horas_publicacao(limite: int | None = None, dry_run: bool = False) -> dict[str, int]:
    """Fix-news-times: corrige a hora falsa das noticias do Google News.

    Fase (a): artigos em meia-noite pacifica viram 'date', com published_at em 00:00 de SP.
    Fase (b): os 'date' com resolved_url de veiculo da allowlist baixam a pagina; se ela
    trouxer hora dentro da regra de refinamento, vira 'datetime' com origem 'pagina'.

    Em dry_run nada e gravado, mas a fase (b) ainda baixa as paginas para medir o efeito
    (leitura, com throttle). O limite vale so para essas paginas.
    """
    plano = planejar_correcao_hora(buscar_candidatos_hora())
    alvos = plano.paginas[:limite] if limite is not None else plano.paginas
    corrigidas: list[tuple[int, dt.datetime]] = []
    sem_hora = fora_da_janela = falhas = gravadas_pagina = 0

    def gravar_corrigidas() -> None:
        # Em lotes, durante o laco: a fase (b) leva horas, e uma queda no fim perderia tudo.
        nonlocal gravadas_pagina
        if dry_run:
            return
        novas = corrigidas[gravadas_pagina:]
        _gravar_horas(
            [{"_id": i, "_hora": h, "_precisao": "datetime", "_origem": "pagina"} for i, h in novas]
        )
        gravadas_pagina = len(corrigidas)

    if not dry_run:
        _gravar_horas(
            [
                {"_id": i, "_hora": h, "_precisao": "date", "_origem": None}
                for i, h in plano.remarcar
            ]
        )

    with ArticleBodyCollector() as coletor:
        for n, (article_id, url, publicado_em) in enumerate(alvos, start=1):
            if n % LOTE_HORAS_PAGINA == 0:
                gravar_corrigidas()
                logger.info(
                    "fix-news-times: %d de %d paginas, %d horas corrigidas",
                    n, len(alvos), len(corrigidas),
                )
            try:
                html = coletor.fetch(url).text
            except Exception as exc:
                logger.debug("pagina do artigo %s nao baixada: %s", article_id, exc)
                falhas += 1
                continue
            extraida = extrair_data_publicacao(html)
            if extraida is None:
                sem_hora += 1
                continue
            nova = refinar_publicacao(publicado_em, extraida)
            if nova is None:
                fora_da_janela += 1
                continue
            corrigidas.append((article_id, nova))
    gravar_corrigidas()

    return {
        "remarcados_para_date": len(plano.remarcar),
        "paginas_candidatas": len(plano.paginas),
        "paginas_baixadas": len(alvos) - falhas,
        "falhas_download": falhas,
        "hora_corrigida_pela_pagina": len(corrigidas),
        "pagina_sem_hora": sem_hora,
        "hora_fora_da_janela": fora_da_janela,
        "sem_resolved_url": plano.sem_url,
        "fora_da_allowlist": plano.fora_da_allowlist,
        "gravados": 0 if dry_run else len(plano.remarcar) + len(corrigidas),
    }


class ArticleBodyCollector(Collector):
    """Baixa e extrai o corpo dos artigos elegiveis."""

    name = "enriquecimento"

    def __init__(
        self, batch_size: int = 200, workers: int | None = None, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.batch_size = batch_size
        self.workers = workers if workers is not None else self.settings.enrich_workers

        # Os throttles sao configurados AQUI, depois do super(). Atribuir
        # `self.default_delay` nao adiantava: o DomainThrottle ja havia sido criado
        # com o valor de classe, e o throttle_article_fetch do .env era ignorado.
        self.throttle.set_default(self.settings.throttle_article_fetch)
        self.throttle.configure("news.google.com", self.settings.throttle_link_resolve)

        # Artigos do lote que so tem a data (id -> published_at) e as horas recuperadas
        # das paginas, gravadas depois do lote por _persist_horas.
        self._publicados_em: dict[int, dt.datetime] = {}
        self._horas_da_pagina: list[tuple[int, dt.datetime]] = []

    def _pending(self) -> list[tuple[int, str, str | None]]:
        """Artigos da allowlist que ainda nao tem corpo.

        O anti-join com article_bodies e o que garante que nenhuma URL e baixada
        duas vezes, mesmo entre execucoes diferentes.
        """
        with session_scope() as session:
            rows = session.execute(
                select(
                    Article.id,
                    Article.url,
                    Article.source_domain,
                    Article.published_at,
                    Article.published_precision,
                )
                .outerjoin(ArticleBody, ArticleBody.article_id == Article.id)
                .where(ArticleBody.article_id.is_(None))
            ).all()
        self._publicados_em = {r[0]: r[3] for r in rows if r[4] == "date"}
        return [(r[0], r[1], r[2]) for r in rows if is_allowed(r[2])]

    def work_units(self) -> Iterable[WorkUnit]:
        pendentes = intercalar_por_dominio(self._pending())
        logger.info("%d artigos elegiveis sem corpo", len(pendentes))
        for start in range(0, len(pendentes), self.batch_size):
            lote = pendentes[start : start + self.batch_size]
            yield WorkUnit(key=f"lote|{lote[0][0]}|{lote[-1][0]}", payload={"artigos": lote})

    def _extrair_um(self, artigo: tuple[int, str, str | None]) -> ExtractedBody | None:
        """Resolve o link, baixa a pagina e extrai o texto de UM artigo.

        Devolve None em qualquer falha: com 200 artigos por lote, um veiculo fora
        do ar nao pode custar os outros 199.
        """
        article_id, google_url, domain = artigo

        # O try abrange TODA a funcao de proposito. `pool.map` propaga a excecao do
        # primeiro item que falhar e aborta a iteracao -- um erro inesperado em um
        # artigo levaria junto os outros 199 do lote, depois de ja ter gasto o
        # tempo de rede deles.
        try:
            real_url = resolve(self._client, google_url, throttle=self.throttle)
            if not real_url:
                logger.debug("link nao resolvido para o artigo %s (%s)", article_id, domain)
                return None

            html = self.fetch(real_url).text
            self._tentar_hora_da_pagina(article_id, html)

            texto = extract_with_trafilatura(html)
            if not texto:
                return None
            return ExtractedBody(article_id, texto, "trafilatura", real_url)
        except Exception as exc:
            logger.debug("artigo %s (%s) falhou: %s", article_id, domain, exc)
            return None

    def _tentar_hora_da_pagina(self, article_id: int, html: str) -> None:
        """Se o artigo so tem a data, procura a hora real no HTML da materia.

        Roda nas threads dos workers, entao so guarda a correcao; quem grava e
        _persist_horas, ao fim do lote.
        """
        publicado_em = self._publicados_em.get(article_id)
        if publicado_em is None:
            return
        extraida = extrair_data_publicacao(html)
        if extraida is None:
            return
        nova = refinar_publicacao(publicado_em, extraida)
        if nova is not None:
            self._horas_da_pagina.append((article_id, nova))

    def _persist_horas(self) -> None:
        """Grava as horas recuperadas das paginas deste lote."""
        if not self._horas_da_pagina:
            return
        linhas = [
            {"_id": article_id, "_hora": hora, "_precisao": "datetime", "_origem": "pagina"}
            for article_id, hora in self._horas_da_pagina
        ]
        self._horas_da_pagina = []
        _gravar_horas(linhas)

    def process(self, unit: WorkUnit) -> UnitResult:
        """Processa o lote em paralelo.

        A paralelizacao e segura porque o DomainThrottle e por dominio e tem lock:
        os workers se serializam sozinhos no news.google.com (por onde TODO artigo
        passa) e correm em paralelo nos veiculos, que sao varios. O ganho vem de
        sobrepor o download de um veiculo com o resolve do proximo artigo.

        httpx.Client e thread-safe, entao um unico cliente atende todos os workers
        e o pool de conexoes e reaproveitado.
        """
        artigos = unit.payload["artigos"]
        extraidos: list[ExtractedBody] = []

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for resultado in pool.map(self._extrair_um, artigos):
                if resultado is not None:
                    extraidos.append(resultado)

        self._persist(extraidos)
        self._persist_horas()
        return UnitResult(items_found=len(extraidos))

    def _persist(self, bodies: list[ExtractedBody]) -> None:
        if not bodies:
            return
        now = dt.datetime.now(dt.UTC)
        with session_scope() as session:
            bulk_upsert(
                session,
                ArticleBody.__table__,
                [
                    {
                        "article_id": b.article_id,
                        "body_text": b.text,
                        "extraction_method": b.method,
                        "fetched_at": now,
                    }
                    for b in bodies
                ],
                index_elements=["article_id"],
                update_columns=None,  # corpo ja extraido nao e re-extraido
            )

            # Guarda a URL real em coluna propria. Sobrescrever `url` deixaria o
            # url_hash (calculado sobre a URL do Google) sem corresponder ao campo,
            # quebrando a deduplicacao na proxima coleta.
            # Um UPDATE executemany para o lote inteiro, nao um execute por artigo.
            resolvidos = [
                {"_id": b.article_id, "_url": b.real_url} for b in bodies if b.real_url
            ]
            if resolvidos:
                # Core, e nao update(Article) do ORM: com lista de parametros o ORM
                # tenta o "bulk update por PK" e exige a chave primaria no dict.
                session.execute(
                    Article.__table__.update()
                    .where(Article.__table__.c.id == bindparam("_id"))
                    .values(resolved_url=bindparam("_url")),
                    resolvidos,
                )
