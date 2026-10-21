"""Daily com plano de etapas (tipo "coletar"): o comando `coletar` nao usa o catalogo das 9 etapas.

Sem rede e sem banco: RepositorioMemoria e etapas falsas.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from tcc_scraper.config import Settings
from tcc_scraper.orquestrador import etapas as E
from tcc_scraper.orquestrador.daily import MOTIVO_SEM_PLANO, Daily, ExecucaoEmAndamento
from tcc_scraper.orquestrador.etapas import EtapaDef, ResultadoEtapa
from tcc_scraper.orquestrador.repositorio import RepositorioMemoria

# 18:30 UTC = 15:30 em Sao Paulo (sem horario de verao).
AGORA = datetime(2026, 10, 19, 18, 30, tzinfo=UTC)


class Relogio:
    def __init__(self, inicio: datetime = AGORA) -> None:
        self.t = inicio

    def __call__(self) -> datetime:
        return self.t


def _etapa(eid: str, ordem: int, run) -> EtapaDef:
    return EtapaDef(eid, ordem, f"nome {eid}", f"coletor {eid}", run)


def _ok(ctx, p):
    p.plano(2)
    p.unidade("a", ok=True, itens=3, saturada=False, erro=None)
    p.unidade("b", ok=True, itens=1, saturada=False, erro=None)
    return ResultadoEtapa("concluida", itens=4)


def _levanta(ctx, p):
    raise RuntimeError("boom")


def _preso():
    """Etapa que so termina quando o teste libera: para observar `ocupado_com`."""
    preso = threading.Event()
    liberar = threading.Event()

    def run(ctx, p):
        preso.set()
        liberar.wait(5)
        return ResultadoEtapa("concluida", itens=0)

    return run, preso, liberar


def _daily(repo, monkeypatch, fakes, settings=None, agora=None) -> Daily:
    monkeypatch.setattr(E, "ETAPAS", tuple(fakes))
    settings = settings or Settings(gemini_api_key="")
    return Daily(repo, settings=settings, agora=agora or Relogio())


def test_criar_coletar_grava_plano_e_roda_so_o_plano(monkeypatch):
    repo = RepositorioMemoria()
    # Se o coletar usasse o catalogo do daily, esta etapa levantaria e a execucao falharia.
    d = _daily(repo, monkeypatch, [_etapa("daily_x", 1, _levanta)])

    eid = d.criar_coletar((_etapa("cvm_itr_dfp", 1, _ok), _etapa("build_entrega", 2, _ok)))

    ex = repo.execucoes[eid]
    assert ex["tipo"] == "coletar"
    assert ex["status"] == "agendada"
    assert ex["etapa_alvo"] is None
    assert [(e["etapa_id"], e["status"]) for e in repo.obter(eid)["etapas"]] == [
        ("cvm_itr_dfp", "pendente"),
        ("build_entrega", "pendente"),
    ]

    d.iniciar(eid)
    d._thread.join(5)

    assert repo.execucoes[eid]["status"] == "concluida"
    assert [e["status"] for e in repo.obter(eid)["etapas"]] == ["concluida", "concluida"]


def test_falha_de_item_nao_para_os_demais(monkeypatch):
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [])

    eid = d.criar_coletar(
        (_etapa("a", 1, _ok), _etapa("b", 2, _levanta), _etapa("c", 3, _ok))
    )
    d.iniciar(eid)
    d._thread.join(5)

    ex = repo.obter(eid)
    assert ex["status"] == "parcial"
    assert [e["status"] for e in ex["etapas"]] == ["concluida", "falhou", "concluida"]


def test_coletar_sem_orcamento_enquanto_o_daily_pula(monkeypatch):
    sem_orcamento = Settings(gemini_api_key="", daily_orcamento_min=0)

    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [], settings=sem_orcamento)
    eid = d.criar_coletar((_etapa("a", 1, _ok), _etapa("b", 2, _ok)))
    d.iniciar(eid)
    d._thread.join(5)
    assert repo.execucoes[eid]["status"] == "concluida"

    # Contraste: o daily com o mesmo orcamento zerado pula tudo.
    repo2 = RepositorioMemoria()
    d2 = _daily(
        repo2, monkeypatch, [_etapa("a", 1, _ok), _etapa("b", 2, _ok)], settings=sem_orcamento
    )
    eid2 = d2.criar("daily")
    d2.iniciar(eid2)
    d2._thread.join(5)
    assert [e["status"] for e in repo2.obter(eid2)["etapas"]] == ["pulada", "pulada"]
    assert repo2.execucoes[eid2]["status"] == "falhou"


def test_ocupado_com_diferencia_coletar_de_daily(monkeypatch):
    run, preso, liberar = _preso()
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [])
    eid = d.criar_coletar((_etapa("x", 1, run),))
    d.iniciar(eid)
    assert preso.wait(5)
    assert d.ocupado_com == "coletar"
    liberar.set()
    d._thread.join(5)
    assert d.ocupado_com is None

    run2, preso2, liberar2 = _preso()
    repo2 = RepositorioMemoria()
    d2 = _daily(repo2, monkeypatch, [_etapa("x", 1, run2)])
    eid2 = d2.criar("daily")
    d2.iniciar(eid2)
    assert preso2.wait(5)
    assert d2.ocupado_com == "daily"
    liberar2.set()
    d2._thread.join(5)
    assert d2.ocupado_com is None


def test_reinicio_falha_coletar_agendado_sem_plano(monkeypatch):
    repo = RepositorioMemoria()
    a = _daily(repo, monkeypatch, [])
    eid = a.criar_coletar((_etapa("cvm_itr_dfp", 1, _ok),))

    # Processo novo, mesmo repositorio: o plano que estava so na memoria de A se perdeu.
    b = _daily(repo, monkeypatch, [])
    assert b.recuperar_apos_reinicio() == 0

    ex = repo.obter(eid)
    assert ex["status"] == "falhou"
    assert "reenvie" in ex["erro"]
    assert [e["status"] for e in ex["etapas"]] == ["pulada"]


def test_iniciar_coletar_sem_plano_falha(monkeypatch):
    repo = RepositorioMemoria()
    a = _daily(repo, monkeypatch, [])
    eid = a.criar_coletar((_etapa("cvm_itr_dfp", 1, _ok),))

    b = _daily(repo, monkeypatch, [])
    b.iniciar(eid)
    b._thread.join(5)

    ex = repo.obter(eid)
    assert ex["status"] == "falhou"
    assert ex["erro"] == MOTIVO_SEM_PLANO


def test_criar_coletar_recusa_plano_vazio_e_execucao_ativa(monkeypatch):
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [_etapa("a", 1, _ok)])

    with pytest.raises(ValueError, match="coletar sem etapas"):
        d.criar_coletar(())

    d.criar("daily")
    with pytest.raises(ExecucaoEmAndamento):
        d.criar_coletar((_etapa("b", 1, _ok),))
    assert len(repo.execucoes) == 1
