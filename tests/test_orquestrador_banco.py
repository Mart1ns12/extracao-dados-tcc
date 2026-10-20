"""RepositorioBanco contra o MySQL do .env (opt-in: TCC_TESTES_COM_BANCO=1).

Padrao de tests/test_batch_regression.py. Os testes apontam o session_scope para o engine
de teste (fixture `engine` de conftest.py). Dados de teste usam o prefixo TESTE- (ids e
hosts) ou a mensagem "TESTE ..." e sao removidos.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from tcc_scraper.db import session as session_mod
from tcc_scraper.db.models_painel import (
    PainelComandoAplicado,
    PainelEtapa,
    PainelExecucao,
    PainelHeartbeat,
    PainelLog,
)
from tcc_scraper.orquestrador import etapas as E
from tcc_scraper.orquestrador.repositorio import RepositorioBanco

SP = ZoneInfo("America/Sao_Paulo")
PREFIXO = "TESTE-"


def _limpar(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(PainelEtapa).where(PainelEtapa.execucao_id.like(f"{PREFIXO}%")))
        s.execute(delete(PainelExecucao).where(PainelExecucao.id.like(f"{PREFIXO}%")))
        s.execute(delete(PainelLog).where(PainelLog.mensagem.like("TESTE %")))
        s.execute(delete(PainelHeartbeat).where(PainelHeartbeat.host.like(f"{PREFIXO}%")))
        s.execute(
            delete(PainelComandoAplicado).where(PainelComandoAplicado.id.like(f"{PREFIXO}%"))
        )
        s.commit()


@pytest.fixture
def repo(engine, monkeypatch):
    sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda: sm)
    _limpar(engine)
    yield RepositorioBanco(tz="America/Sao_Paulo")
    _limpar(engine)


def _execucao(eid: str, *, status="agendada", origem="manual", iniciada=None) -> dict:
    iniciada = iniciada or dt.datetime(2099, 1, 15, 12, 0, tzinfo=SP)
    return {
        "id": eid,
        "tipo": "daily",
        "etapa_alvo": None,
        "origem": origem,
        "status": status,
        "aguardando": None,
        "iniciada_em": iniciada,
        "finalizada_em": None,
        "prazo_em": iniciada + dt.timedelta(hours=8),
        "erro": None,
        "atualizada_em": iniciada,
    }


def _etapas(eid: str) -> list[dict]:
    return [
        {
            "execucao_id": eid,
            "etapa_id": e.id,
            "ordem": e.ordem,
            "status": "pendente",
            "unidades_total": None,
            "unidades_feitas": 0,
            "unidades_erro": 0,
            "unidades_saturadas": 0,
            "itens": 0,
            "iniciada_em": None,
            "finalizada_em": None,
            "ultimo_erro_unidade": None,
            "ultimo_erro_mensagem": None,
            "atualizada_em": dt.datetime(2099, 1, 15, 12, 0, tzinfo=dt.UTC),
        }
        for e in E.ETAPAS
    ]


def test_criar_execucao_grava_as_9_etapas_e_obter_as_devolve_ordenadas(repo) -> None:
    eid = f"{PREFIXO}criar"
    repo.criar_execucao(_execucao(eid), _etapas(eid))

    ex = repo.obter(eid)

    assert ex["status"] == "agendada"
    assert ex["iniciada_em"].tzinfo is not None
    assert [e["etapa_id"] for e in ex["etapas"]] == [e.id for e in E.ETAPAS]


def test_atualizar_etapa_muda_contadores(repo) -> None:
    eid = f"{PREFIXO}contadores"
    repo.criar_execucao(_execucao(eid), _etapas(eid))

    repo.atualizar_etapa(eid, "precos", unidades_total=85, unidades_feitas=40, itens=900)

    etapa = next(e for e in repo.obter(eid)["etapas"] if e["etapa_id"] == "precos")
    assert (etapa["unidades_total"], etapa["unidades_feitas"], etapa["itens"]) == (85, 40, 900)


def test_atual_prefere_em_andamento_e_listar_ordena_por_iniciada(repo) -> None:
    depois = dt.datetime(2099, 2, 1, 12, 0, tzinfo=SP)
    antes = dt.datetime(2099, 1, 31, 12, 0, tzinfo=SP)
    repo.criar_execucao(_execucao(f"{PREFIXO}agenda", status="agendada", iniciada=depois),
                        _etapas(f"{PREFIXO}agenda"))
    repo.criar_execucao(_execucao(f"{PREFIXO}rodando", status="em_andamento", iniciada=antes),
                        _etapas(f"{PREFIXO}rodando"))

    # Em andamento vence a agendada, mesmo que esta seja mais recente.
    assert repo.atual()["id"] == f"{PREFIXO}rodando"

    proprias = [e["id"] for e in repo.listar(1000) if e["id"].startswith(PREFIXO)]
    assert proprias == [f"{PREFIXO}agenda", f"{PREFIXO}rodando"]


def test_existe_execucao_cron_considera_o_dia_em_sao_paulo(repo) -> None:
    # 00:30 em SP ainda e o dia 15; em UTC seria 03:30 do mesmo dia. Manual nao conta.
    repo.criar_execucao(
        _execucao(
            f"{PREFIXO}cron",
            origem="cron",
            iniciada=dt.datetime(2099, 1, 15, 0, 30, tzinfo=SP),
        ),
        _etapas(f"{PREFIXO}cron"),
    )
    repo.criar_execucao(
        _execucao(
            f"{PREFIXO}manual",
            origem="manual",
            iniciada=dt.datetime(2099, 1, 17, 10, 0, tzinfo=SP),
        ),
        _etapas(f"{PREFIXO}manual"),
    )

    assert repo.existe_execucao_cron(dt.date(2099, 1, 15)) is True
    assert repo.existe_execucao_cron(dt.date(2099, 1, 14)) is False
    assert repo.existe_execucao_cron(dt.date(2099, 1, 17)) is False


def test_marcar_interrompidas_converte_em_andamento_em_falhou(repo, engine) -> None:
    with Session(engine) as s:
        reais = s.execute(
            select(PainelExecucao.id).where(
                PainelExecucao.status == "em_andamento",
                PainelExecucao.id.not_like(f"{PREFIXO}%"),
            )
        ).all()
    if reais:
        pytest.skip("ha execucao real em andamento; teste destrutivo pulado")

    eid = f"{PREFIXO}interrompida"
    repo.criar_execucao(_execucao(eid, status="em_andamento"), _etapas(eid))
    repo.atualizar_etapa(eid, "precos", status="em_andamento")

    assert repo.marcar_interrompidas("interrompida por teste") >= 1

    ex = repo.obter(eid)
    assert ex["status"] == "falhou"
    assert ex["erro"] == "interrompida por teste"
    assert next(e for e in ex["etapas"] if e["etapa_id"] == "precos")["status"] == "falhou"


def test_registrar_comando_e_comando_aplicado(repo, engine) -> None:
    cid = f"{PREFIXO}cmd-1"
    assert repo.comando_aplicado(cid) is False

    repo.registrar_comando(cid, True, None)
    assert repo.comando_aplicado(cid) is True

    # Reaplicar atualiza a linha em vez de duplicar (upsert).
    repo.registrar_comando(cid, False, "falhou de novo")
    with Session(engine) as s:
        linhas = s.execute(
            select(PainelComandoAplicado).where(PainelComandoAplicado.id == cid)
        ).scalars().all()
    assert len(linhas) == 1
    assert linhas[0].ok is False
    assert linhas[0].erro == "falhou de novo"


def test_heartbeat_faz_upsert_por_host(repo, engine) -> None:
    host = f"{PREFIXO}host"
    repo.heartbeat(host, "1.0", None)
    repo.heartbeat(host, "1.1", "daily")

    with Session(engine) as s:
        linhas = (
            s.execute(select(PainelHeartbeat).where(PainelHeartbeat.host == host))
            .scalars()
            .all()
        )
    assert len(linhas) == 1
    assert linhas[0].versao == "1.1"
    assert linhas[0].ocupado_com == "daily"

