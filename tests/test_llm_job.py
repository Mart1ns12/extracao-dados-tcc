"""Testes do orquestrador da analise diaria (llm/job.py) contra banco real.

Mesmo padrao de tests/test_upsert.py: PostgreSQL e MySQL reais, pulados se o banco
estiver fora do ar. O Gemini e substituido por um cliente falso -- nenhum teste faz rede.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from functools import cache
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from tcc_scraper import config
from tcc_scraper.config import BENCHMARK_TICKER
from tcc_scraper.db import session as db_session
from tcc_scraper.db.models import (
    Base,
    LlmAcaoSinal,
    LlmMercado,
    LlmNoticiaEmpresa,
    LlmNoticiaMacro,
    LlmPrompt,
    LlmRun,
    Price,
)
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.llm.gemini_client import RespostaGemini
from tcc_scraper.llm.job import run_daily_analysis

PG_URL = os.environ.get(
    "TEST_PG_URL", "postgresql+psycopg://tcc:tcc_local_dev@localhost:5433/tcc_scraper"
)
MYSQL_URL = os.environ.get(
    "TEST_MYSQL_URL", "mysql+pymysql://root@127.0.0.1:3307/tcc_scraper"
)

# Timeout curto e resultado em cache: a conclusao de "banco fora do ar" e tirada uma
# vez por URL, sem esperar o timeout padrao do driver em cada teste.
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
    """Roda cada teste nos dois dialetos."""
    if request.param == "postgresql":
        return _engine_or_skip(PG_URL, "PostgreSQL")
    return _engine_or_skip(MYSQL_URL, "MySQL")


DATA = dt.date(2099, 1, 5)              # data de teste: nunca colide com dado real
PREGAO_SONDA = dt.date(2099, 1, 2)      # ultimo pregao <= DATA no preco-sonda
FIXTURE = Path(__file__).parent / "fixtures" / "gemini_analise_diaria.json"
TABELAS_LLM = [
    Price.__table__,
    LlmPrompt.__table__,
    LlmRun.__table__,
    LlmNoticiaEmpresa.__table__,
    LlmNoticiaMacro.__table__,
    LlmAcaoSinal.__table__,
    LlmMercado.__table__,
]


def _limpar(engine) -> None:
    """Apaga a data de teste (o cascade leva as staging) e o preco-sonda."""
    with engine.begin() as conn:
        conn.execute(LlmRun.__table__.delete().where(LlmRun.__table__.c.data_referencia == DATA))
        conn.execute(
            Price.__table__.delete().where(
                Price.__table__.c.ticker == BENCHMARK_TICKER,
                Price.__table__.c.date == PREGAO_SONDA,
            )
        )


@pytest.fixture
def banco(engine, monkeypatch):
    """Aponta o job para o engine de teste, com preco-sonda e limpeza antes e depois."""
    Base.metadata.create_all(engine, tables=TABELAS_LLM)
    sessao = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_session, "get_sessionmaker", lambda: sessao)
    monkeypatch.setattr(db_session, "get_engine", lambda: engine)

    _limpar(engine)
    with sessao() as s:
        bulk_upsert(
            s, Price.__table__,
            [{"ticker": BENCHMARK_TICKER, "date": PREGAO_SONDA, "close": 100.0,
              "adj_close": 100.0, "source": "teste"}],
            index_elements=["ticker", "date"],
        )
        s.commit()
    yield engine
    _limpar(engine)


class ClienteFalso:
    """Substitui o GeminiClient: devolve sempre o mesmo texto."""

    def __init__(self, texto: str) -> None:
        self.texto = texto

    def analisar(self, prompt: str, schema: dict) -> RespostaGemini:
        return RespostaGemini(
            texto=self.texto, modelo="fake", latencia_ms=5,
            tokens_entrada=10, tokens_saida=20,
            buscas=["petrobras hoje"], citacoes=[{"url": "https://x", "title": "X"}],
        )


def _sem_rede(url: str) -> None:
    """Resolvedor de redirect que nao faz rede: devolve None (URL fica como veio)."""
    return None


class ClienteQuebrado:
    """Simula falha da API (ex.: 503 depois das tentativas do retry)."""

    def analisar(self, prompt: str, schema: dict) -> RespostaGemini:
        raise RuntimeError("503 boom")


def _texto_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _contar(engine, tabela) -> int:
    with Session(engine) as s:
        return s.execute(
            select(func.count()).select_from(tabela).where(tabela.c.data_referencia == DATA)
        ).scalar_one()


def _runs(engine) -> list[LlmRun]:
    with Session(engine) as s:
        return list(s.execute(
            select(LlmRun).where(LlmRun.data_referencia == DATA).order_by(LlmRun.id)
        ).scalars())


def _staging_vazia(engine) -> bool:
    return all(
        _contar(engine, m.__table__) == 0
        for m in (LlmNoticiaEmpresa, LlmNoticiaMacro, LlmAcaoSinal, LlmMercado)
    )


def test_run_ok_grava_run_e_staging(banco) -> None:
    engine = banco
    r = run_daily_analysis(DATA, cliente=ClienteFalso(_texto_fixture()), resolver=_sem_rede)

    assert r.status == "ok"
    assert r.pregao_referencia == PREGAO_SONDA
    assert r.linhas == {"noticias_empresas": 2, "noticias_macro": 2,
                        "acoes_sinais": 2, "mercados": 2}

    # XXXX9 (fora do universo) foi descartado: so PETR4 e VALE3 sobram.
    with Session(engine) as s:
        tickers = set(s.execute(
            select(LlmNoticiaEmpresa.ticker).where(LlmNoticiaEmpresa.data_referencia == DATA)
        ).scalars())
    assert tickers == {"PETR4", "VALE3"}

    assert _contar(engine, LlmNoticiaEmpresa.__table__) == 2
    assert _contar(engine, LlmNoticiaMacro.__table__) == 2
    assert _contar(engine, LlmAcaoSinal.__table__) == 2
    assert _contar(engine, LlmMercado.__table__) == 2

    [run] = _runs(engine)
    assert run.id == r.run_id
    assert run.status == "ok"
    assert run.tokens_entrada == 10
    assert run.buscas_json == ["petrobras hoje"]
    assert run.resposta_json["resumo_geral"] == json.loads(_texto_fixture())["resumo_geral"]
    assert run.citacoes_json[0]["url"] == "https://x"


def test_reexecutar_o_dia_nao_duplica_staging(banco) -> None:
    engine = banco
    run_daily_analysis(DATA, cliente=ClienteFalso(_texto_fixture()), resolver=_sem_rede)
    r2 = run_daily_analysis(DATA, cliente=ClienteFalso(_texto_fixture()), resolver=_sem_rede)

    runs = _runs(engine)
    assert len(runs) == 2
    assert r2.run_id == runs[-1].id

    assert _contar(engine, LlmNoticiaEmpresa.__table__) == 2
    assert _contar(engine, LlmNoticiaMacro.__table__) == 2
    assert _contar(engine, LlmAcaoSinal.__table__) == 2
    assert _contar(engine, LlmMercado.__table__) == 2

    # As linhas da staging apontam para a execucao mais recente.
    with Session(engine) as s:
        run_ids = set(s.execute(
            select(LlmNoticiaEmpresa.run_id).where(LlmNoticiaEmpresa.data_referencia == DATA)
        ).scalars())
    assert run_ids == {r2.run_id}


def test_json_invalido_vira_erro_json(banco) -> None:
    engine = banco
    r = run_daily_analysis(DATA, cliente=ClienteFalso("{nao e json"), resolver=_sem_rede)

    assert r.status == "erro_json"
    [run] = _runs(engine)
    assert run.status == "erro_json"
    assert run.resposta_json is None
    assert _staging_vazia(engine)


def test_enum_invalido_vira_erro_validacao(banco) -> None:
    engine = banco
    dados = json.loads(_texto_fixture())
    dados["noticias_empresas"][0]["setor"] = "setor_inventado"

    r = run_daily_analysis(DATA, cliente=ClienteFalso(json.dumps(dados)), resolver=_sem_rede)

    assert r.status == "erro_validacao"
    [run] = _runs(engine)
    assert run.status == "erro_validacao"
    assert _staging_vazia(engine)


def test_api_quebrada_vira_erro_api(banco) -> None:
    engine = banco
    r = run_daily_analysis(DATA, cliente=ClienteQuebrado())

    assert r.status == "erro_api"
    assert "boom" in (r.erro or "")
    [run] = _runs(engine)
    assert run.status == "erro_api"
    assert "boom" in run.erro
    assert run.resposta_bruta is None
    assert _staging_vazia(engine)


def test_dry_run_nao_toca_no_banco(banco, monkeypatch) -> None:
    def _proibido():
        raise AssertionError("dry-run nao pode abrir sessao")

    monkeypatch.setattr(db_session, "get_sessionmaker", _proibido)
    cliente = ClienteFalso(_texto_fixture())

    r = run_daily_analysis(DATA, dry_run=True, cliente=cliente, resolver=_sem_rede)

    assert r.status == "dry_run"
    assert "PETR4" in (r.prompt or "")
    assert r.run_id is None
    assert _runs(banco) == []


def test_sem_chave_levanta_value_error(banco, monkeypatch) -> None:
    pytest.importorskip("google.genai")
    monkeypatch.setattr(config.get_settings(), "gemini_api_key", "")

    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        run_daily_analysis(DATA)
