"""O dataset v1 continua congelado mesmo com IPE, julgamentos da juiza e tickers extras na base.

`features/build.carregar_entradas_v1()` so pode enxergar artigos `origem == "google_news"` e
so os vinculos de `staging_article_tickers`. Os julgamentos da juiza e os tickers adicionais
que ela sugere NAO entram no v1: ele e um artefato congelado do TCC.

Roda contra o MySQL do .env (opt-in: TCC_TESTES_COM_BANCO=1). Os dados de teste usam URLs
com prefixo reconhecivel e sao removidos antes e depois de cada teste.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tcc_scraper.db import session as session_mod
from tcc_scraper.db.models import Article, ArticleSentiment, ArticleTicker, url_hash
from tcc_scraper.db.models_fontes import StagingNoticiaJulgamento
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.entrega.builder import modelos_finbert
from tcc_scraper.features.build import carregar_entradas_v1

PREFIXO = "https://t27.regr.invalid/"
URLS = {
    "prot": PREFIXO + "protagonista",
    "ipe": PREFIXO + "ipe",
    "pan": PREFIXO + "panorama",
}
ORIGENS = {"prot": "google_news", "ipe": "cvm_ipe", "pan": "google_news"}
DATA_PUB = dt.datetime(2099, 1, 5, 12, 0, tzinfo=dt.UTC)


def _limpar(engine) -> None:
    """Remove so as linhas de teste (pelo prefixo da URL), preservando o resto."""
    with Session(engine) as s:
        ids = s.execute(
            select(Article.id).where(Article.url.like(f"{PREFIXO}%"))
        ).scalars().all()
        if ids:
            s.execute(StagingNoticiaJulgamento.__table__.delete().where(
                StagingNoticiaJulgamento.article_id.in_(ids)))
            s.execute(ArticleSentiment.__table__.delete().where(
                ArticleSentiment.article_id.in_(ids)))
            s.execute(ArticleTicker.__table__.delete().where(ArticleTicker.article_id.in_(ids)))
            s.execute(Article.__table__.delete().where(Article.id.in_(ids)))
        s.commit()


def _semear(engine) -> dict[str, int]:
    """Um artigo de cada tipo, todos com vinculo PETR4 e sentimento, para que so a origem
    (e nao a falta de vinculo ou de sentimento) explique o que sai do v1."""
    agora = dt.datetime.now(dt.UTC)
    linhas = [
        {
            "url_hash": url_hash(URLS[k]), "url": URLS[k], "title": f"T27 {k}",
            "source_name": "Teste T27", "source_domain": "teste27.invalid",
            "published_at": DATA_PUB, "lang": "pt", "collected_at": agora,
            "origem": ORIGENS[k],
        }
        for k in URLS
    ]
    with Session(engine) as s:
        bulk_upsert(s, Article.__table__, linhas, index_elements=["url_hash"], update_columns=None)
        s.flush()
        por_url = dict(
            s.execute(
                select(Article.url, Article.id).where(Article.url.in_(list(URLS.values())))
            ).all()
        )
        ids = {k: por_url[URLS[k]] for k in URLS}

        bulk_upsert(s, ArticleTicker.__table__,
                    [{"article_id": ids[k], "ticker": "PETR4"} for k in URLS],
                    index_elements=["article_id", "ticker"], update_columns=None)
        bulk_upsert(s, ArticleSentiment.__table__,
                    [{"article_id": ids[k], "model": modelos_finbert()["pt"],
                      "prob_pos": 0.7, "prob_neu": 0.2, "prob_neg": 0.1,
                      "text_source": "title", "scored_at": agora} for k in URLS],
                    index_elements=["article_id", "model"], update_columns=None)
        # Julgamentos da juiza: o v1 tem de ignorar tudo isto.
        bulk_upsert(s, StagingNoticiaJulgamento.__table__, [
            {"article_id": ids["prot"], "ticker": "PETR4", "prompt_versao": 1, "run_id": None,
             "modelo": "teste-t27", "papel": "protagonista", "tipo_conteudo": "reportagem",
             "evento": "resultado", "tickers_adicionais": ["VALE3"],
             "qualidade": 0.9, "confianca": 0.9, "julgado_em": agora},
            {"article_id": ids["pan"], "ticker": "PETR4", "prompt_versao": 1, "run_id": None,
             "modelo": "teste-t27", "papel": "mencao", "tipo_conteudo": "resumo_mercado",
             "evento": "outro", "tickers_adicionais": [],
             "qualidade": 0.3, "confianca": 0.9, "julgado_em": agora},
        ], index_elements=["article_id", "ticker", "prompt_versao"], update_columns=None)
        s.commit()
    return ids


@pytest.fixture
def semeado(engine, monkeypatch):
    sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda: sm)
    _limpar(engine)
    try:
        yield _semear(engine)
    finally:
        _limpar(engine)


def test_dataset_v1_so_ve_google_news_e_o_vinculo_original(semeado) -> None:
    """IPE sai pela origem; VALE3 (ticker sugerido pela juiza) e os julgamentos nao entram."""
    _precos, artigos = carregar_entradas_v1()
    do_teste = artigos[artigos["article_id"].isin(list(semeado.values()))]

    # O panorama entra: o v1 nao usa a juiza, e isso e o que o congela.
    assert set(do_teste["article_id"]) == {semeado["prot"], semeado["pan"]}
    assert set(do_teste["ticker"]) == {"PETR4"}
    assert semeado["ipe"] not in set(do_teste["article_id"])
