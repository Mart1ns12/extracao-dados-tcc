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
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select

from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.config import ARTICLE_ALLOWLIST
from tcc_scraper.db.models import Article, ArticleBody
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
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


class ArticleBodyCollector(Collector):
    """Baixa e extrai o corpo dos artigos elegiveis."""

    name = "article_bodies"

    def __init__(self, batch_size: int = 200, **kwargs) -> None:
        super().__init__(**kwargs)
        self.batch_size = batch_size
        self.default_delay = self.settings.throttle_article_fetch

    def _pending(self) -> list[tuple[int, str, str | None]]:
        """Artigos da allowlist que ainda nao tem corpo.

        O anti-join com article_bodies e o que garante que nenhuma URL e baixada
        duas vezes, mesmo entre execucoes diferentes.
        """
        with session_scope() as session:
            rows = session.execute(
                select(Article.id, Article.url, Article.source_domain)
                .outerjoin(ArticleBody, ArticleBody.article_id == Article.id)
                .where(ArticleBody.article_id.is_(None))
            ).all()
        return [(r[0], r[1], r[2]) for r in rows if is_allowed(r[2])]

    def work_units(self) -> Iterable[WorkUnit]:
        pendentes = self._pending()
        logger.info("%d artigos elegiveis sem corpo", len(pendentes))
        for start in range(0, len(pendentes), self.batch_size):
            lote = pendentes[start : start + self.batch_size]
            yield WorkUnit(key=f"lote|{lote[0][0]}|{lote[-1][0]}", payload={"artigos": lote})

    def process(self, unit: WorkUnit) -> UnitResult:
        extraidos: list[ExtractedBody] = []
        for article_id, google_url, domain in unit.payload["artigos"]:
            real_url = resolve(self._client, google_url)
            if not real_url:
                logger.debug("link nao resolvido para o artigo %s (%s)", article_id, domain)
                continue
            try:
                html = self.fetch(real_url).text
            except Exception as exc:
                logger.debug("falha ao baixar %s: %s", real_url, exc)
                continue

            texto = extract_with_trafilatura(html)
            if texto:
                extraidos.append(
                    ExtractedBody(article_id, texto, "trafilatura", real_url)
                )

        self._persist(extraidos)
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
            resolvidos = [
                {"_id": b.article_id, "resolved_url": b.real_url} for b in bodies if b.real_url
            ]
            for item in resolvidos:
                session.execute(
                    Article.__table__.update()
                    .where(Article.id == item["_id"])
                    .values(resolved_url=item["resolved_url"])
                )
