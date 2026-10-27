"""Pontuacao de sentimento (FinBERT) em blocos, com gravacao incremental.

Com 88 mil artigos a inferencia leva ~30 min. Pontuar tudo e so entao gravar
significa que uma queda aos 25 min perde o trabalho inteiro -- e, como a query de
pendentes filtra por quem ainda nao tem sentimento, gravar em blocos torna a etapa
retomavel de graca.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select

from tcc_scraper.db.models import Article, ArticleBody, ArticleSentiment
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

if TYPE_CHECKING:
    from tcc_scraper.sentiment.router import SentimentRouter

# Artigos pontuados e gravados por vez. Grande o bastante para o upsert render,
# pequeno o bastante para uma queda custar pouco trabalho refeito.
SCORE_CHUNK = 2_000


def blocos(itens: list, tamanho: int) -> Iterator[list]:
    """Fatia a lista em blocos de `tamanho` (o ultimo pode ser menor)."""
    for inicio in range(0, len(itens), tamanho):
        yield itens[inicio : inicio + tamanho]


def linhas_sentimento(rows: list, resultados: list, quando: dt.datetime) -> list[dict]:
    """Monta as linhas de staging_article_sentiment a partir da saida do router.

    `rows` sao tuplas (id, titulo, lang, corpo); `resultados`, pares (score, modelo)
    na MESMA ordem -- o router garante isso mesmo agrupando por idioma.
    """
    return [
        {
            "article_id": row[0],
            "model": modelo,
            "prob_pos": score.prob_pos,
            "prob_neu": score.prob_neu,
            "prob_neg": score.prob_neg,
            # Registra se o score saiu do corpo ou so da manchete: sem isso nao da
            # para avaliar depois se o enriquecimento melhorou o sinal.
            "text_source": "body" if row[3] else "title",
            "scored_at": quando,
        }
        for row, (score, modelo) in zip(rows, resultados, strict=True)
    ]


def pendentes_de_score(
    limit: int | None = None, anos: Sequence[int] | None = None
) -> list[tuple[int, str, str, str | None]]:
    """Artigos sem sentimento: (id, titulo, lang, corpo).

    Prefere o corpo quando existe; senao usa a manchete. text_source registra
    qual foi, para a analise de qualidade. `anos` filtra pelo ano de publicacao em UTC
    (mesma janela da juiza).
    """
    consulta = (
        select(Article.id, Article.title, Article.lang, ArticleBody.body_text)
        .outerjoin(ArticleBody, ArticleBody.article_id == Article.id)
        .outerjoin(ArticleSentiment, ArticleSentiment.article_id == Article.id)
        .where(ArticleSentiment.article_id.is_(None))
    )
    if anos:
        janelas = [and_(
            Article.published_at >= dt.datetime(ano, 1, 1, tzinfo=dt.UTC),
            Article.published_at < dt.datetime(ano + 1, 1, 1, tzinfo=dt.UTC),
        ) for ano in sorted(set(anos))]
        consulta = consulta.where(or_(*janelas))
    with session_scope() as session:
        rows = session.execute(consulta).all()

    if limit:
        rows = rows[:limit]
    return [tuple(r) for r in rows]


def score_pending(
    *,
    batch_size: int = 32,
    limit: int | None = None,
    anos: Sequence[int] | None = None,
    router: SentimentRouter | None = None,
    observador: Any | None = None,
) -> dict[str, int]:
    """Pontua os artigos pendentes e grava cada bloco assim que e pontuado.

    `anos` restringe ao ano de publicacao (ver pendentes_de_score): a carga historica
    passa o ano, para nao pontuar so pela manchete artigos que o enriquecimento da outra
    trilha ainda nao processou.
    `observador` (opcional, duck-typed) recebe `plano(n_blocos)` antes do laco,
    `unidade(nome, ok=, itens=, saturada=, erro=)` por bloco e `deve_parar()` antes
    de cada bloco; se devolver True, para e devolve o que ja foi gravado.
    """
    rows = pendentes_de_score(limit, anos=anos)
    if not rows:
        return {"artigos": 0, "gravados": 0, "com_corpo": 0, "so_manchete": 0,
                "interrompido": 0}

    if router is None:
        from tcc_scraper.sentiment.router import SentimentRouter

        router = SentimentRouter()
    router.scorers = {
        k: type(v)(v.model_name, v.lang, batch_size) for k, v in router.scorers.items()
    }

    total = len(rows)
    com_corpo = sum(1 for r in rows if r[3])
    lotes = list(blocos(rows, SCORE_CHUNK))
    if observador is not None:
        observador.plano(len(lotes))

    gravados = 0
    interrompido = 0
    # Grava a cada bloco em vez de so no fim. Como a consulta de pendentes exclui
    # quem ja tem sentimento, gravar em blocos torna a etapa retomavel de graca.
    for i, bloco in enumerate(lotes):
        if observador is not None and observador.deve_parar():
            interrompido = 1
            break
        resultados = router.score_texts(
            [(r[3] or r[1]) for r in bloco], langs=[r[2] for r in bloco]
        )
        linhas = linhas_sentimento(bloco, resultados, dt.datetime.now(dt.UTC))
        with session_scope() as session:
            bulk_upsert(
                session,
                ArticleSentiment.__table__,
                linhas,
                index_elements=["article_id", "model"],
                update_columns=["prob_pos", "prob_neu", "prob_neg", "text_source", "scored_at"],
            )
        gravados += len(linhas)
        if observador is not None:
            observador.unidade(f"bloco|{i}", ok=True, itens=len(bloco), saturada=False,
                               erro=None)

    return {
        "artigos": total,
        "gravados": gravados,
        "com_corpo": com_corpo,
        "so_manchete": total - com_corpo,
        "interrompido": interrompido,
    }
