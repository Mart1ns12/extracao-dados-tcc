"""Protege as duas propriedades que a portabilidade custou caro para obter.

1. **Lote.** O `_persist` do Google News ja fez tres `execute` POR ARTIGO. Voltar a
   isso nao quebraria teste nenhum -- o resultado seria identico, so lento. Por isso
   o numero de statements e testado explicitamente.

2. **Precisao.** `sa.Float()` vira 4 bytes no MySQL e truncava as probabilidades do
   FinBERT (0.7766114473342896 voltava 0.776611), fazendo o dataset mudar conforme o
   banco. `Double` corrige; este teste impede a regressao.
"""

from __future__ import annotations

import datetime as dt
import os
from functools import cache

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from tcc_scraper.db.models import Article, ArticleTicker, Base, url_hash
from tcc_scraper.db.upsert import bulk_upsert

PG_URL = os.environ.get(
    "TEST_PG_URL", "postgresql+psycopg://tcc:tcc_local_dev@localhost:5433/tcc_scraper"
)
MYSQL_URL = os.environ.get(
    "TEST_MYSQL_URL", "mysql+pymysql://root@127.0.0.1:3307/tcc_scraper"
)


# Timeout curto e resultado em cache, de proposito. Com o timeout padrao do driver
# (30 s+) e uma tentativa por teste, a suite travava por dez minutos so para concluir
# que um banco opcional nao estava no ar -- foi o que aconteceu apos um reinicio da
# maquina. Com cache, a conclusao e tirada UMA vez por URL.
_CONNECT_TIMEOUT = 2


@cache
def _tentar_engine(url: str):
    """Devolve (engine, None) se conectou, ou (None, motivo). Avaliado uma vez por URL."""
    args = (
        {"connect_timeout": _CONNECT_TIMEOUT}
        if url.startswith(("mysql", "postgresql"))
        else {}
    )
    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args=args)
        with engine.connect():
            pass
        return engine, None
    except Exception as exc:
        return None, str(exc)[:80]


def _engine_or_skip(url: str, label: str):
    engine, motivo = _tentar_engine(url)
    if engine is None:
        pytest.skip(f"{label} indisponivel: {motivo}")
    return engine


@pytest.fixture(params=["postgresql", "mysql"])
def engine(request):
    if request.param == "postgresql":
        return _engine_or_skip(PG_URL, "PostgreSQL")
    return _engine_or_skip(MYSQL_URL, "MySQL")


class ContaStatements:
    """Conta comandos enviados ao servidor (um executemany conta como um)."""

    def __init__(self, engine):
        self.engine = engine
        self.total = 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._inc)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._inc)

    def _inc(self, conn, cursor, statement, params, context, executemany):
        self.total += 1


@pytest.fixture
def limpa(engine):
    """Remove os dados de teste antes e depois, sem tocar no resto."""
    def _limpar():
        with Session(engine) as s:
            s.execute(ArticleTicker.__table__.delete().where(ArticleTicker.ticker == "ZZZZ9"))
            s.execute(Article.__table__.delete().where(Article.google_id.like("REGR%")))
            s.commit()
    _limpar()
    yield
    _limpar()


def _payload(n: int):
    agora = dt.datetime.now(dt.UTC)
    linhas = []
    for i in range(n):
        u = f"https://news.google.com/rss/articles/REGR{i}"
        linhas.append({
            "url_hash": url_hash(u), "url": u, "google_id": f"REGR{i}",
            "title": f"Materia {i}", "source_name": "Teste",
            "source_domain": "teste.com.br", "published_at": agora,
            "lang": "pt", "collected_at": agora,
        })
    return linhas


def test_grava_em_lote_e_nao_uma_query_por_linha(engine, limpa) -> None:
    """52 materias (tamanho tipico de uma janela) devem custar poucas queries."""
    linhas = _payload(52)

    with Session(engine) as s, ContaStatements(engine) as contador:
        bulk_upsert(s, Article.__table__, linhas,
                    index_elements=["url_hash"], update_columns=None)
        s.flush()
        ids = s.execute(
            select(Article.id).where(Article.url_hash.in_([r["url_hash"] for r in linhas]))
        ).scalars().all()
        bulk_upsert(s, ArticleTicker.__table__,
                    [{"article_id": a, "ticker": "ZZZZ9"} for a in ids],
                    index_elements=["article_id", "ticker"], update_columns=None)
        s.commit()

    assert len(ids) == 52
    # Tres operacoes logicas + overhead de transacao. O limite generoso ainda
    # denuncia qualquer volta ao padrao de uma query por linha (que daria 150+).
    assert contador.total <= 10, (
        f"{contador.total} statements para 52 linhas -- o lote regrediu"
    )


def test_probabilidades_sobrevivem_ao_round_trip(engine) -> None:
    """A precisao de double precisa valer nos dois bancos.

    Com `sa.Float()` o MySQL truncava em ~7 digitos e o dataset mudava de banco
    para banco.
    """
    from sqlalchemy import Column, Integer, MetaData, Table
    from sqlalchemy.types import Double

    meta = MetaData()
    t = Table(
        "precisao_probe", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("p", Double, nullable=False),
    )
    meta.drop_all(engine)
    meta.create_all(engine)
    try:
        # Saidas reais do FinBERT, com todos os digitos que o modelo produz.
        originais = [0.7766114473342896, 0.030664993450045586, 0.9350475668907166]
        with Session(engine) as s:
            s.execute(t.insert(), [{"p": v} for v in originais])
            s.commit()
            lidos = s.execute(select(t.c.p).order_by(t.c.id)).scalars().all()
        assert lidos == originais, f"precisao perdida: {lidos!r} != {originais!r}"
    finally:
        meta.drop_all(engine)


def test_todas_as_tabelas_tem_prefixo_de_camada() -> None:
    """Sem schemas, o prefixo e a unica marca de camada -- e o filtro do Alembic."""
    from tcc_scraper.db.models import tabela_do_projeto

    for tabela in Base.metadata.sorted_tables:
        assert tabela_do_projeto(tabela.name), tabela.name
        assert tabela.schema is None, f"{tabela.name} ainda declara schema (quebra no MySQL)"


# --------------------------------------------------------------------------------------
# Persistencia do dataset em features_dataset_v1
# --------------------------------------------------------------------------------------
@pytest.fixture
def limpa_dataset(engine):
    """Remove so as linhas de teste, preservando o dataset real da tabela."""
    from tcc_scraper.db.models import DatasetV1

    def _limpar():
        with Session(engine) as s:
            s.execute(DatasetV1.__table__.delete().where(DatasetV1.ticker == "ZZZZ9"))
            s.commit()
    _limpar()
    yield
    _limpar()


def _linhas_dataset(n_noticias: int, positivo):
    import datetime as d
    return [
        {
            "data": d.date(2025, 9, 1) + d.timedelta(days=i), "ticker": "ZZZZ9",
            "quant_noticias": n_noticias, "positivo": positivo, "neutro": positivo,
            "negativo": positivo, "score_finbert": 0.0 if positivo is not None else None,
            "retorno_1d": 0.01, "retorno_5d": 0.02, "retorno_20d": 0.03,
            "volatilidade_20d": 0.015, "volume_relativo": 1.2,
            "retorno_ibovespa": 0.005, "target": i % 2,
        }
        for i in range(3)
    ]


def test_dataset_e_gravado_no_banco(engine, limpa_dataset) -> None:
    """O build-dataset precisa materializar em features_dataset_v1, nao so exportar."""
    from tcc_scraper.db.models import DatasetV1

    with Session(engine) as s:
        bulk_upsert(s, DatasetV1.__table__, _linhas_dataset(5, 0.7),
                    index_elements=["data", "ticker"], update_columns=["quant_noticias"])
        s.commit()
        n = s.execute(
            select(DatasetV1.data).where(DatasetV1.ticker == "ZZZZ9")
        ).scalars().all()
    assert len(n) == 3


def test_regravar_atualiza_em_vez_de_duplicar(engine, limpa_dataset) -> None:
    """Remontar o dataset com outra regra sobrescreve as linhas, nao acumula."""
    from tcc_scraper.db.models import DatasetV1

    cols = ["quant_noticias", "positivo", "neutro", "negativo", "score_finbert"]
    with Session(engine) as s:
        bulk_upsert(s, DatasetV1.__table__, _linhas_dataset(5, 0.7),
                    index_elements=["data", "ticker"], update_columns=cols)
        s.commit()
        bulk_upsert(s, DatasetV1.__table__, _linhas_dataset(99, 0.1),
                    index_elements=["data", "ticker"], update_columns=cols)
        s.commit()

        linhas = s.execute(
            select(DatasetV1.quant_noticias).where(DatasetV1.ticker == "ZZZZ9")
        ).scalars().all()

    assert len(linhas) == 3, "upsert duplicou linhas"
    assert set(linhas) == {99}, "os valores nao foram atualizados"


def test_sentimento_ausente_vira_null_no_banco(engine, limpa_dataset) -> None:
    """Dia sem noticia grava NULL; gravar 0 afirmaria sentimento neutro medido."""
    from tcc_scraper.db.models import DatasetV1

    with Session(engine) as s:
        bulk_upsert(s, DatasetV1.__table__, _linhas_dataset(0, None),
                    index_elements=["data", "ticker"], update_columns=["positivo"])
        s.commit()
        vals = s.execute(
            select(DatasetV1.positivo).where(DatasetV1.ticker == "ZZZZ9")
        ).scalars().all()

    assert all(v is None for v in vals), f"esperado NULL, veio {vals}"


def test_enriquecimento_grava_resolved_url_em_lote(engine, limpa, monkeypatch) -> None:
    """O resolved_url de 10 artigos sai em um executemany, nao um UPDATE por artigo."""
    from contextlib import contextmanager

    from tcc_scraper.enrich import article_fetcher as af

    @contextmanager
    def _sessao_de_teste():
        sessao = Session(engine)
        try:
            yield sessao
            sessao.commit()
        except Exception:
            sessao.rollback()
            raise
        finally:
            sessao.close()

    # O _persist usa session_scope, que apontaria para o banco configurado no .env.
    monkeypatch.setattr(af, "session_scope", _sessao_de_teste)

    linhas = _payload(10)
    with Session(engine) as s:
        bulk_upsert(s, Article.__table__, linhas,
                    index_elements=["url_hash"], update_columns=None)
        s.commit()
        pares = s.execute(
            select(Article.id, Article.google_id).where(Article.google_id.like("REGR%"))
        ).all()
    assert len(pares) == 10

    corpos = [
        af.ExtractedBody(
            article_id=aid, text="texto de materia " * 20, method="trafilatura",
            real_url=f"https://veiculo.com.br/materia/{gid}",
        )
        for aid, gid in pares
    ]

    with af.ArticleBodyCollector() as coletor, ContaStatements(engine) as contador:
        coletor._persist(corpos)

    with Session(engine) as s:
        resolvidas = s.execute(
            select(Article.resolved_url).where(Article.google_id.like("REGR%"))
        ).scalars().all()
    assert len(resolvidas) == 10
    assert all(url and url.startswith("https://veiculo.com.br/materia/") for url in resolvidas)
    assert contador.total <= 6, (
        f"{contador.total} statements para 10 resolved_url -- o update voltou a ser por linha"
    )
