"""build_entrega(tabelas=["indices"]) contra PostgreSQL e MySQL reais (skip se nao estiver no ar).

Padrao de tests/test_orquestrador_banco.py: o session_scope aponta para o engine de teste
(TEST_PG_URL / TEST_MYSQL_URL). Os dados de teste usam datas em 2099 e sao removidos.
Os dois dialetos devem gravar exatamente o mesmo conteudo para as mesmas linhas de origem.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import cache

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from tcc_scraper.config import get_settings
from tcc_scraper.db import session as session_mod
from tcc_scraper.db.models import Article, ArticleSentiment, ArticleTicker, Macro, Price, url_hash
from tcc_scraper.db.models_entrega import Indice, Noticia
from tcc_scraper.db.models_fontes import StagingNoticiaJulgamento
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.entrega import builder as entrega_builder
from tcc_scraper.entrega.builder import build_entrega, id_noticia, modelos_finbert

PG_URL = os.environ.get(
    "TEST_PG_URL", "postgresql+psycopg://tcc:tcc_local_dev@localhost:5433/tcc_scraper"
)
MYSQL_URL = os.environ.get(
    "TEST_MYSQL_URL", "mysql+pymysql://root@127.0.0.1:3307/tcc_scraper"
)
_CONNECT_TIMEOUT = 2
INICIO = dt.date(2099, 1, 1)
ESPERADO = [
    (dt.date(2099, 1, 1), "IPCA", 0.26),
    (dt.date(2099, 1, 2), "CDI", 0.0551),
    (dt.date(2099, 1, 2), "IDIV", 5123.5),
]


@cache
def _tentar_engine(url: str):
    args = {"connect_timeout": _CONNECT_TIMEOUT} if url.startswith(("mysql", "postgresql")) else {}
    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args=args)
        with engine.connect():
            pass
        return engine, None
    except Exception as exc:
        return None, str(exc)[:80]


@pytest.fixture(params=["postgresql", "mysql"])
def engine(request):
    url, label = (PG_URL, "PostgreSQL") if request.param == "postgresql" else (MYSQL_URL, "MySQL")
    eng, motivo = _tentar_engine(url)
    if eng is None:
        pytest.skip(f"{label} indisponivel: {motivo}")
    return eng


def _limpar(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(Macro).where(Macro.series.in_(["cdi", "ipca"]), Macro.date >= INICIO))
        s.execute(delete(Price).where(Price.ticker == "IDIV", Price.date >= INICIO))
        s.execute(delete(Indice).where(Indice.data >= INICIO))
        s.commit()


@pytest.fixture
def dados(engine, monkeypatch):
    sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda: sm)
    _limpar(engine)
    with Session(engine) as s:
        s.add_all([
            Macro(series="cdi", date=dt.date(2099, 1, 2), value=0.0551),
            Macro(series="ipca", date=dt.date(2099, 1, 1), value=0.26),
            Price(ticker="IDIV", date=dt.date(2099, 1, 2), open=None, high=None, low=None,
                  close=5123.5, adj_close=None, volume=None, source="yahoo"),
        ])
        s.commit()
    yield engine
    _limpar(engine)


def _indices_de_teste(engine) -> list[tuple]:
    with Session(engine) as s:
        linhas = s.execute(
            select(Indice.data, Indice.indice, Indice.valor)
            .where(Indice.data >= INICIO)
            .order_by(Indice.data, Indice.indice)
        ).all()
    return [(d, i, float(v)) for d, i, v in linhas]


def test_indices_nao_duplica_e_grava_o_mesmo_conteudo(dados) -> None:
    engine = dados
    primeira = build_entrega(tabelas=["indices"])
    assert primeira["indices"] >= len(ESPERADO)
    assert _indices_de_teste(engine) == sorted(ESPERADO)

    segunda = build_entrega(tabelas=["indices"])
    assert segunda["indices"] == primeira["indices"]
    assert _indices_de_teste(engine) == sorted(ESPERADO)  # nada duplicado


# --------------------------------------------------------------------------------------
# noticias: ENTREGA_FILTRO_JUIZA decide o que entra na entrega
# --------------------------------------------------------------------------------------
PREFIXO_NOTICIAS = "https://t27.entrega.invalid/"
URLS_NOTICIAS = {
    "prot": PREFIXO_NOTICIAS + "protagonista",
    "ipe": PREFIXO_NOTICIAS + "ipe",
    "pan": PREFIXO_NOTICIAS + "panorama",
}
ORIGENS_NOTICIAS = {"prot": "google_news", "ipe": "cvm_ipe", "pan": "google_news"}


def _limpar_noticias(engine) -> None:
    """Remove so as linhas de teste (pelo prefixo da URL)."""
    with Session(engine) as s:
        ids = s.execute(
            select(Article.id).where(Article.url.like(f"{PREFIXO_NOTICIAS}%"))
        ).scalars().all()
        if ids:
            s.execute(Noticia.__table__.delete().where(
                Noticia.id.in_([id_noticia(i) for i in ids])))
            s.execute(StagingNoticiaJulgamento.__table__.delete().where(
                StagingNoticiaJulgamento.article_id.in_(ids)))
            s.execute(ArticleSentiment.__table__.delete().where(
                ArticleSentiment.article_id.in_(ids)))
            s.execute(ArticleTicker.__table__.delete().where(ArticleTicker.article_id.in_(ids)))
            s.execute(Article.__table__.delete().where(Article.id.in_(ids)))
        s.commit()


def _semear_noticias(engine) -> dict[str, int]:
    """Protagonista (com ticker adicional VALE3), IPE e panorama, todos com vinculo PETR4."""
    agora = dt.datetime.now(dt.UTC)
    linhas = [
        {
            "url_hash": url_hash(URLS_NOTICIAS[k]), "url": URLS_NOTICIAS[k],
            "title": f"T27 {k}", "source_name": "Teste T27", "source_domain": "teste27.invalid",
            "published_at": dt.datetime(2099, 1, 5, 12, 0, tzinfo=dt.UTC), "lang": "pt",
            "collected_at": agora, "origem": ORIGENS_NOTICIAS[k],
        }
        for k in URLS_NOTICIAS
    ]
    with Session(engine) as s:
        bulk_upsert(s, Article.__table__, linhas, index_elements=["url_hash"], update_columns=None)
        s.flush()
        por_url = dict(
            s.execute(
                select(Article.url, Article.id).where(
                    Article.url.in_(list(URLS_NOTICIAS.values()))
                )
            ).all()
        )
        ids = {k: por_url[URLS_NOTICIAS[k]] for k in URLS_NOTICIAS}
        bulk_upsert(s, ArticleTicker.__table__,
                    [{"article_id": ids[k], "ticker": "PETR4"} for k in URLS_NOTICIAS],
                    index_elements=["article_id", "ticker"], update_columns=None)
        bulk_upsert(s, ArticleSentiment.__table__,
                    [{"article_id": ids[k], "model": modelos_finbert()["pt"],
                      "prob_pos": 0.7, "prob_neu": 0.2, "prob_neg": 0.1,
                      "text_source": "title", "scored_at": agora} for k in URLS_NOTICIAS],
                    index_elements=["article_id", "model"], update_columns=None)
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
def noticias_semeadas(engine, monkeypatch):
    sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda: sm)
    _limpar_noticias(engine)
    try:
        yield _semear_noticias(engine)
    finally:
        _limpar_noticias(engine)


@pytest.fixture
def gravadas(monkeypatch) -> list[dict]:
    """Captura o que build_entrega gravaria em `noticias`, sem gravar.

    O banco de teste e o de desenvolvimento, com dados reais: deixar a entrega reescrever
    `noticias` levaria as linhas do panorama (filtro desligado) para a tabela real.
    """
    capturadas: list[dict] = []

    def _falso_gravar(tabela: str, linhas: list[dict], artigos_janela=None) -> None:
        if tabela == "noticias":
            capturadas.extend(linhas)

    monkeypatch.setattr(entrega_builder, "_gravar", _falso_gravar)
    return capturadas


def _noticias_de_teste(gravadas: list[dict], ids: dict[str, int]) -> list[dict]:
    nossas = {id_noticia(i) for i in ids.values()}
    return [r for r in gravadas if r["id"] in nossas]


def test_filtro_da_juiza_exclui_panorama_e_mantem_protagonista_e_ipe(
    noticias_semeadas, gravadas, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "entrega_filtro_juiza", True)
    assert "noticias" in build_entrega(tabelas=["noticias"])

    pares = {(r["id"], r["ticker"]) for r in _noticias_de_teste(gravadas, noticias_semeadas)}
    assert pares == {
        (id_noticia(noticias_semeadas["prot"]), "PETR4"),
        (id_noticia(noticias_semeadas["prot"]), "VALE3"),  # ticker adicional da juiza
        (id_noticia(noticias_semeadas["ipe"]), "PETR4"),
    }


def test_sem_filtro_da_juiza_o_panorama_entra_com_o_julgamento(
    noticias_semeadas, gravadas, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "entrega_filtro_juiza", False)
    assert "noticias" in build_entrega(tabelas=["noticias"])

    pan = [
        r for r in _noticias_de_teste(gravadas, noticias_semeadas)
        if r["id"] == id_noticia(noticias_semeadas["pan"])
    ]
    assert len(pan) == 1
    assert pan[0]["papel"] == "mencao"
    assert pan[0]["tipo_conteudo"] == "resumo_mercado"


def _pares_no_banco(engine, ids: dict[str, int]) -> set[tuple[str, str]]:
    codigos = [id_noticia(i) for i in ids.values()]
    with Session(engine) as s:
        linhas = s.execute(
            select(Noticia.id, Noticia.ticker).where(Noticia.id.in_(codigos))
        ).all()
    return {(i, t) for i, t in linhas}


def test_panorama_some_do_banco_quando_o_filtro_e_religado(
    engine, noticias_semeadas, monkeypatch
) -> None:
    """Grava com o filtro desligado (panorama entra), religa o filtro e grava de novo."""
    ids = noticias_semeadas
    prot, ipe, pan = id_noticia(ids["prot"]), id_noticia(ids["ipe"]), id_noticia(ids["pan"])
    alvo = set(ids.values())

    monkeypatch.setattr(get_settings(), "entrega_filtro_juiza", False)
    assert "noticias" in build_entrega(tabelas=["noticias"], somente_ids=alvo)
    assert (pan, "PETR4") in _pares_no_banco(engine, ids)

    monkeypatch.setattr(get_settings(), "entrega_filtro_juiza", True)
    assert "noticias" in build_entrega(tabelas=["noticias"], somente_ids=alvo)
    assert _pares_no_banco(engine, ids) == {
        (prot, "PETR4"),
        (prot, "VALE3"),
        (ipe, "PETR4"),
    }
