"""Orquestrador do daily sem banco: RepositorioMemoria, etapas falsas e relogio injetado."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from tcc_scraper.config import Settings
from tcc_scraper.orquestrador import etapas as E
from tcc_scraper.orquestrador.daily import Daily, ExecucaoEmAndamento
from tcc_scraper.orquestrador.etapas import Contexto, EtapaDef, ResultadoEtapa
from tcc_scraper.orquestrador.progresso import Progresso
from tcc_scraper.orquestrador.repositorio import RepositorioMemoria

# 18:30 UTC = 15:30 em Sao Paulo (sem horario de verao).
AGORA = datetime(2026, 10, 19, 18, 30, tzinfo=UTC)


class Relogio:
    def __init__(self, inicio: datetime = AGORA) -> None:
        self.t = inicio

    def __call__(self) -> datetime:
        return self.t


class Espiao(RepositorioMemoria):
    """Conta os UPDATEs de etapa, para verificar quando o Progresso faz flush."""

    def __init__(self) -> None:
        super().__init__()
        self.updates_etapa = 0

    def atualizar_etapa(self, execucao_id, etapa_id, **campos):
        self.updates_etapa += 1
        super().atualizar_etapa(execucao_id, etapa_id, **campos)


def _etapa(eid: str, ordem: int, run) -> EtapaDef:
    return EtapaDef(eid, ordem, f"nome {eid}", f"coletor {eid}", run)


def _ok(ctx, p):
    p.plano(2)
    p.unidade("a", ok=True, itens=3, saturada=False, erro=None)
    p.unidade("b", ok=True, itens=1, saturada=False, erro=None)
    return ResultadoEtapa("concluida", itens=4)


def _levanta(ctx, p):
    raise RuntimeError("boom")


def _daily(repo, monkeypatch, fakes, settings=None, agora=None) -> Daily:
    monkeypatch.setattr(E, "ETAPAS", tuple(fakes))
    settings = settings or Settings(gemini_api_key="")
    return Daily(repo, settings=settings, agora=agora or Relogio())


def _por_id(ex: dict, eid: str) -> dict:
    return next(e for e in ex["etapas"] if e["etapa_id"] == eid)


def _ctx(deadline: datetime | None = None, cancelar: threading.Event | None = None) -> Contexto:
    return Contexto(
        execucao_id="E1",
        hoje=date(2026, 10, 19),
        settings=Settings(gemini_api_key=""),
        deadline=deadline or AGORA + timedelta(hours=1),
        cancelar=cancelar or threading.Event(),
        origem="manual",
    )


def _repo_com_etapa() -> Espiao:
    repo = Espiao()
    repo.criar_execucao(
        {"id": "E1", "status": "em_andamento", "iniciada_em": AGORA},
        [{"execucao_id": "E1", "etapa_id": "precos", "ordem": 1, "status": "pendente"}],
    )
    return repo


# -- execucao ------------------------------------------------------------------------
def test_tres_etapas_com_falha_no_meio_resulta_parcial(monkeypatch) -> None:
    repo = RepositorioMemoria()
    fakes = [_etapa("a", 1, _ok), _etapa("b", 2, _levanta), _etapa("c", 3, _ok)]
    d = _daily(repo, monkeypatch, fakes)

    eid = d.executar("daily")

    ex = repo.obter(eid)
    assert ex["status"] == "parcial"
    assert ex["finalizada_em"] is not None
    b = _por_id(ex, "b")
    assert b["status"] == "falhou"
    assert b["ultimo_erro_mensagem"] == "RuntimeError: boom"
    assert _por_id(ex, "c")["status"] == "concluida"


def test_todas_ok_resulta_concluida(monkeypatch) -> None:
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [_etapa("a", 1, _ok), _etapa("b", 2, _ok)])

    ex = repo.obter(d.executar("daily"))

    assert ex["status"] == "concluida"
    assert [e["status"] for e in ex["etapas"]] == ["concluida", "concluida"]
    assert _por_id(ex, "a")["itens"] == 4


def test_criar_duas_vezes_recusa_com_execucao_em_andamento(monkeypatch) -> None:
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [_etapa("a", 1, _ok)])

    d.criar("daily")

    with pytest.raises(ExecucaoEmAndamento):
        d.criar("daily")


def test_cancelar_pula_as_restantes_e_falha_a_execucao(monkeypatch) -> None:
    repo = RepositorioMemoria()
    holder: dict = {}

    def _cancela(ctx, p):
        holder["d"].cancelar()
        return ResultadoEtapa("concluida", itens=1)

    fakes = [_etapa("a", 1, _cancela), _etapa("b", 2, _ok), _etapa("c", 3, _ok)]
    d = _daily(repo, monkeypatch, fakes)
    holder["d"] = d

    ex = repo.obter(d.executar("daily"))

    assert ex["status"] == "falhou"
    assert ex["erro"] == "cancelada"
    assert _por_id(ex, "a")["status"] == "concluida"
    assert _por_id(ex, "b")["status"] == "pulada"
    assert _por_id(ex, "b")["ultimo_erro_mensagem"] == "cancelada"
    assert _por_id(ex, "c")["status"] == "pulada"


def test_deadline_no_passado_pula_coleta_mas_roda_entrega_e_analise(monkeypatch) -> None:
    repo = RepositorioMemoria()
    fakes = [_etapa("precos", 1, _ok), _etapa("entrega", 2, _ok), _etapa("analise", 3, _ok)]
    # Orcamento zero: o deadline e igual ao inicio, entao ja esta estourado na 1a etapa.
    sem_orcamento = Settings(gemini_api_key="", daily_orcamento_min=0)
    d = _daily(repo, monkeypatch, fakes, settings=sem_orcamento)

    ex = repo.obter(d.executar("daily"))

    assert _por_id(ex, "precos")["status"] == "pulada"
    assert _por_id(ex, "precos")["ultimo_erro_mensagem"] == "orçamento esgotado"
    assert _por_id(ex, "entrega")["status"] == "concluida"
    assert _por_id(ex, "analise")["status"] == "concluida"


def test_prazo_em_e_20h_de_sao_paulo_em_utc(monkeypatch) -> None:
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [_etapa("a", 1, _ok)])

    eid = d.criar("daily")

    ex = repo.obter(eid)
    assert ex["prazo_em"] == datetime(2026, 10, 19, 23, 0, tzinfo=UTC)
    assert eid == "2026-10-19-daily-153000"


def test_tipo_etapa_cria_uma_etapa_so(monkeypatch) -> None:
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [_etapa("precos", 1, _ok), _etapa("ipe", 2, _ok)])

    eid = d.criar("etapa", "ipe")

    ex = repo.obter(eid)
    assert [e["etapa_id"] for e in ex["etapas"]] == ["ipe"]
    assert ex["etapa_alvo"] == "ipe"


def test_etapa_fora_do_catalogo_levanta_valueerror(monkeypatch) -> None:
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [_etapa("precos", 1, _ok)])

    with pytest.raises(ValueError):
        d.criar("etapa", "inexistente")


def test_recuperar_apos_reinicio_converte_em_andamento_em_falhou(monkeypatch) -> None:
    repo = RepositorioMemoria()
    d = _daily(repo, monkeypatch, [_etapa("precos", 1, _ok)])
    eid = d.criar("daily")
    repo.atualizar_execucao(eid, status="em_andamento")
    repo.atualizar_etapa(eid, "precos", status="em_andamento")

    assert d.recuperar_apos_reinicio() == 1

    ex = repo.obter(eid)
    assert ex["status"] == "falhou"
    assert _por_id(ex, "precos")["status"] == "falhou"


def test_cancelar_sem_execucao_devolve_false(monkeypatch) -> None:
    d = _daily(RepositorioMemoria(), monkeypatch, [_etapa("a", 1, _ok)])

    assert d.cancelar() is False


def test_warning_do_tcc_scraper_vai_para_o_log_do_painel_com_a_etapa(monkeypatch) -> None:
    repo = RepositorioMemoria()

    def _avisa(ctx, p):
        logging.getLogger("tcc_scraper.teste").warning("TESTE aviso da coleta")
        return ResultadoEtapa("concluida", itens=0)

    d = _daily(repo, monkeypatch, [_etapa("precos", 1, _avisa)])

    d.executar("daily")

    avisos = [m for m in repo.logs if m["mensagem"] == "TESTE aviso da coleta"]
    assert len(avisos) == 1
    assert avisos[0]["nivel"] == "aviso"
    assert avisos[0]["etapa"] == "precos"


def test_analise_sem_chave_gemini_e_pulada_sem_chamar_a_llm() -> None:
    p = Progresso(RepositorioMemoria(), "E1", "analise", _ctx())

    r = E.etapa_analise(_ctx(), p)

    assert r.status == "pulada"
    assert r.erro == "GEMINI_API_KEY ausente"


# -- Progresso -----------------------------------------------------------------------
def test_progresso_faz_flush_em_plano() -> None:
    repo = _repo_com_etapa()
    p = Progresso(repo, "E1", "precos", _ctx(), agora=Relogio())

    p.plano(10)

    assert repo.updates_etapa == 1
    assert repo.etapas[("E1", "precos")]["unidades_total"] == 10


def test_progresso_faz_flush_a_cada_25_unidades() -> None:
    repo = _repo_com_etapa()
    p = Progresso(repo, "E1", "precos", _ctx(), agora=Relogio())
    p.plano(100)
    antes = repo.updates_etapa

    for i in range(24):
        p.unidade(f"u{i}", ok=True, itens=1, saturada=False, erro=None)
    assert repo.updates_etapa == antes  # 24 unidades: ainda sem flush

    p.unidade("u24", ok=True, itens=1, saturada=False, erro=None)
    assert repo.updates_etapa == antes + 1
    assert repo.etapas[("E1", "precos")]["unidades_feitas"] == 25


def test_progresso_faz_flush_apos_2_segundos() -> None:
    repo = _repo_com_etapa()
    relogio = Relogio()
    p = Progresso(repo, "E1", "precos", _ctx(), agora=relogio)
    p.plano(10)
    antes = repo.updates_etapa

    relogio.t += timedelta(seconds=1)
    p.unidade("u0", ok=True, itens=1, saturada=False, erro=None)
    assert repo.updates_etapa == antes

    relogio.t += timedelta(seconds=1)
    p.unidade("u1", ok=False, itens=0, saturada=False, erro="falhou")
    assert repo.updates_etapa == antes + 1
    etapa = repo.etapas[("E1", "precos")]
    assert etapa["unidades_erro"] == 1
    assert etapa["ultimo_erro_unidade"] == "u1"
    assert etapa["ultimo_erro_mensagem"] == "falhou"


def test_deve_parar_respeita_cancelar_e_deadline() -> None:
    cancelar = threading.Event()
    relogio = Relogio()
    deadline = AGORA + timedelta(minutes=10)
    p = Progresso(_repo_com_etapa(), "E1", "precos", _ctx(deadline, cancelar), agora=relogio)
    assert p.deve_parar() is False

    cancelar.set()
    assert p.deve_parar() is True

    cancelar.clear()
    relogio.t = deadline
    assert p.deve_parar() is True


# -- de_stats ------------------------------------------------------------------------
def test_de_stats_concluida() -> None:
    r = E.de_stats({"total": 3, "ok": 3, "erro": 0, "itens": 10, "saturadas": 0, "interrompido": 0})
    assert r.status == "concluida"
    assert r.itens == 10


def test_de_stats_todas_as_unidades_falharam() -> None:
    r = E.de_stats({"total": 2, "ok": 0, "erro": 2, "itens": 0, "saturadas": 0, "interrompido": 0})
    assert r.status == "falhou"
    assert r.erro == "todas as unidades falharam"


def test_de_stats_interrompida() -> None:
    r = E.de_stats({"total": 5, "ok": 2, "erro": 0, "itens": 4, "saturadas": 0, "interrompido": 1})
    assert r.status == "falhou"
    assert r.erro == "interrompida: orçamento ou cancelamento"


# -- catalogo ------------------------------------------------------------------------
def test_catalogo_tem_os_9_ids_ordem_nome_e_coletor_do_painel() -> None:
    esperado = [
        ("precos", 1, "Preços e índices", "yahoo"),
        ("macro", 2, "Macro do BCB", "bcb"),
        ("noticias", 3, "Notícias", "google_news"),
        ("ipe", 4, "Fatos relevantes", "cvm_ipe"),
        ("enriquecimento", 5, "Corpo e sentimento", "finbert"),
        ("juiza", 6, "Juíza de qualidade", "juiza"),
        ("cadastro", 7, "Cadastro e eventos", "cvm_b3"),
        ("entrega", 8, "Montagem das 8 tabelas", "build_entrega"),
        ("analise", 9, "Análise diária", "gemini"),
    ]
    assert [(e.id, e.ordem, e.nome, e.coletor) for e in E.ETAPAS] == esperado
    assert set(E.POR_ID) == {e[0] for e in esperado}


def test_podar_logs_remove_so_os_mais_antigos_que_o_corte() -> None:
    repo = RepositorioMemoria()
    agora = datetime.now(UTC)
    repo.log("daily", "info", None, "recente", None)
    repo.logs[0]["ts"] = agora - timedelta(days=1)
    repo.log("daily", "info", None, "antigo", None)
    repo.logs[1]["ts"] = agora - timedelta(days=10)

    assert repo.podar_logs(7) == 1
    assert [m["mensagem"] for m in repo.logs] == ["recente"]


# -- etapas com coletores falsos (sem rede) -------------------------------------------
class _Observador:
    """Duck-type do Progresso: registra os planos e os logs de cada etapa."""

    def __init__(self) -> None:
        self.planos: list[int] = []
        self.logs: list[tuple[str, str]] = []

    def plano(self, total: int) -> None:
        self.planos.append(total)

    def unidade(self, key, *, ok, itens, saturada, erro) -> None:
        return None

    def deve_parar(self) -> bool:
        return False

    def log(self, nivel: str, mensagem: str) -> None:
        self.logs.append((nivel, mensagem))


def _coletor_falso(nome: str, registro: list, levanta: bool = False, stats: dict | None = None):
    """Coletor que anota o construtor e o run, planeja 1 unidade e, se `levanta`, cai depois."""

    class Falso:
        def __init__(self, *args, **kwargs) -> None:
            registro.append(("init", nome, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> None:
            return None

        def run(self, *, resume=True, limit=None, apenas_erros=False, observador=None) -> dict:
            registro.append(("run", nome, resume))
            observador.plano(1)
            if levanta:
                raise RuntimeError(f"{nome} caiu")
            return dict(stats or {"total": 1, "ok": 1, "erro": 0, "itens": 2, "saturadas": 0})

    return Falso


def test_etapa_cadastro_um_sub_coletor_levanta_e_os_outros_seguem(monkeypatch) -> None:
    registro: list = []
    monkeypatch.setattr(
        "tcc_scraper.collectors.cvm_cadastro.CvmCadastroCollector",
        _coletor_falso("cvm_cadastro", registro),
    )
    monkeypatch.setattr(
        "tcc_scraper.collectors.b3_cadastro.B3CadastroCollector",
        _coletor_falso("b3_cadastro", registro),
    )
    monkeypatch.setattr(
        "tcc_scraper.collectors.b3_proventos.B3ProventosCollector",
        _coletor_falso("b3_proventos", registro, levanta=True),
    )
    monkeypatch.setattr(
        "tcc_scraper.collectors.cvm_demonstracoes.CvmDemonstracoesCollector",
        _coletor_falso("cvm_itr_dfp", registro),
    )
    obs = _Observador()

    r = E.etapa_cadastro(_ctx(), obs)

    assert r.status == "concluida"
    assert r.itens == 6  # 3 sub-coletores com 2 itens cada; o que caiu contribui com 0
    assert len(obs.planos) == 4
    assert [(c[0], c[1], c[2]) for c in registro if c[0] == "run"] == [
        ("run", "cvm_cadastro", False),
        ("run", "b3_cadastro", False),
        ("run", "b3_proventos", False),
        ("run", "cvm_itr_dfp", True),
    ]
    assert ("init", "cvm_itr_dfp", {"anos": [2026]}) in registro
    assert any(n == "erro" and "b3_proventos" in m for n, m in obs.logs)


def test_etapa_cadastro_falha_quando_todos_os_sub_coletores_falham(monkeypatch) -> None:
    registro: list = []
    for alvo, nome in [
        ("tcc_scraper.collectors.cvm_cadastro.CvmCadastroCollector", "cvm_cadastro"),
        ("tcc_scraper.collectors.b3_cadastro.B3CadastroCollector", "b3_cadastro"),
        ("tcc_scraper.collectors.b3_proventos.B3ProventosCollector", "b3_proventos"),
        ("tcc_scraper.collectors.cvm_demonstracoes.CvmDemonstracoesCollector", "cvm_itr_dfp"),
    ]:
        monkeypatch.setattr(alvo, _coletor_falso(nome, registro, levanta=True))

    r = E.etapa_cadastro(_ctx(), _Observador())

    assert r.status == "falhou"
    assert r.erro == "todas as partes falharam"


def test_etapa_cadastro_interrompida_e_falhou(monkeypatch) -> None:
    registro: list = []
    interrompido = {"total": 1, "ok": 0, "erro": 0, "itens": 0, "saturadas": 0, "interrompido": 1}
    monkeypatch.setattr(
        "tcc_scraper.collectors.cvm_cadastro.CvmCadastroCollector",
        _coletor_falso("cvm_cadastro", registro, stats=interrompido),
    )
    for alvo, nome in [
        ("tcc_scraper.collectors.b3_cadastro.B3CadastroCollector", "b3_cadastro"),
        ("tcc_scraper.collectors.b3_proventos.B3ProventosCollector", "b3_proventos"),
        ("tcc_scraper.collectors.cvm_demonstracoes.CvmDemonstracoesCollector", "cvm_itr_dfp"),
    ]:
        monkeypatch.setattr(alvo, _coletor_falso(nome, registro))

    r = E.etapa_cadastro(_ctx(), _Observador())

    assert r.status == "falhou"
    assert r.erro == "interrompida: orçamento ou cancelamento"


def test_etapa_ipe_chama_cvm_ipe_com_o_ano_corrente(monkeypatch) -> None:
    registro: list = []
    monkeypatch.setattr(
        "tcc_scraper.collectors.cvm_ipe.CvmIpeCollector", _coletor_falso("cvm_ipe", registro)
    )

    r = E.etapa_ipe(_ctx(), _Observador())

    assert r.status == "concluida"
    assert ("init", "cvm_ipe", {"anos": [2026]}) in registro
    assert ("run", "cvm_ipe", True) in registro


def test_etapa_macro_roda_bcb_e_indices_com_o_ano_corrente(monkeypatch) -> None:
    registro: list = []
    monkeypatch.setattr(
        "tcc_scraper.collectors.macro_bcb.BcbCollector", _coletor_falso("bcb", registro)
    )
    monkeypatch.setattr(
        "tcc_scraper.collectors.indices.IndicesBcbCollector",
        _coletor_falso("indices_bcb", registro),
    )

    r = E.etapa_macro(_ctx(), _Observador())

    assert r.status == "concluida"
    assert r.itens == 4
    assert ("run", "bcb", False) in registro
    assert ("init", "indices_bcb", {"anos": [2026]}) in registro
    assert ("run", "indices_bcb", False) in registro


# -- entrega e juiza -------------------------------------------------------------------
def test_etapa_entrega_concluida_soma_as_tabelas_gravadas(monkeypatch) -> None:
    from tcc_scraper.db.models_entrega import COLUNAS

    chamadas: list = []

    def falso(**kwargs):
        chamadas.append(kwargs)
        return {t: 2 for t in COLUNAS}

    monkeypatch.setattr("tcc_scraper.entrega.builder.build_entrega", falso)
    obs = _Observador()

    r = E.etapa_entrega(_ctx(), obs)

    assert r.status == "concluida"
    assert r.itens == 2 * len(COLUNAS)
    assert chamadas == [{"observador": obs}]


def test_etapa_entrega_falha_quando_alguma_tabela_nao_gravou(monkeypatch) -> None:
    from tcc_scraper.db.models_entrega import COLUNAS

    sem = {"cadastro", "indices"}

    def parcial(**kwargs):
        return {t: 2 for t in COLUNAS if t not in sem}

    monkeypatch.setattr("tcc_scraper.entrega.builder.build_entrega", parcial)

    r = E.etapa_entrega(_ctx(), _Observador())

    assert r.status == "falhou"
    assert r.itens == 2 * (len(COLUNAS) - len(sem))  # o que gravou continua contado
    assert sorted(r.erro.removeprefix("tabelas com falha: ").split(", ")) == sorted(sem)


def test_etapa_entrega_falha_quando_nenhuma_tabela_gravou(monkeypatch) -> None:
    monkeypatch.setattr("tcc_scraper.entrega.builder.build_entrega", lambda **kw: {})

    r = E.etapa_entrega(_ctx(), _Observador())

    assert r.status == "falhou"
    assert r.erro == "todas as tabelas falharam"


def test_etapa_entrega_excecao_sobe_para_o_daily(monkeypatch) -> None:
    def levanta(**kwargs):
        raise RuntimeError("banco caiu")

    monkeypatch.setattr("tcc_scraper.entrega.builder.build_entrega", levanta)

    with pytest.raises(RuntimeError, match="banco caiu"):
        E.etapa_entrega(_ctx(), _Observador())


def _resultado_juiza(**campos):
    from tcc_scraper.llm.juiza import ResultadoJuiza

    base = {
        "pendentes": 10,
        "regra": 4,
        "jev": 2,
        "gemini": 3,
        "erros": 1,
        "runs": 2,
        "tokens_entrada": 100,
        "tokens_saida": 20,
        "interrompido": 0,
    }
    base.update(campos)
    return ResultadoJuiza(**base)


def test_etapa_juiza_concluida_soma_regra_jev_e_gemini(monkeypatch) -> None:
    chamadas: list = []

    def falso(**kwargs):
        chamadas.append(kwargs)
        return _resultado_juiza()

    monkeypatch.setattr("tcc_scraper.llm.juiza.julgar_pendentes", falso)
    ctx = _ctx()
    obs = _Observador()

    r = E.etapa_juiza(ctx, obs)

    assert r.status == "concluida"
    assert r.itens == 9
    assert r.detalhes["tokens_saida"] == 20
    assert chamadas == [{"limite_itens": ctx.settings.juiza_max_itens_dia, "observador": obs}]


def test_etapa_juiza_concluida_mesmo_so_com_a_regra_barata(monkeypatch) -> None:
    monkeypatch.setattr(
        "tcc_scraper.llm.juiza.julgar_pendentes",
        lambda **kw: _resultado_juiza(jev=0, gemini=0, regra=6),
    )

    r = E.etapa_juiza(_ctx(), _Observador())

    assert r.status == "concluida"
    assert r.itens == 6


def test_etapa_juiza_interrompida_e_falhou(monkeypatch) -> None:
    monkeypatch.setattr(
        "tcc_scraper.llm.juiza.julgar_pendentes",
        lambda **kw: _resultado_juiza(interrompido=1),
    )

    r = E.etapa_juiza(_ctx(), _Observador())

    assert r.status == "falhou"
    assert r.erro == "interrompida: orçamento ou cancelamento"
    assert r.itens == 9


def test_etapa_juiza_excecao_sobe_para_o_daily(monkeypatch) -> None:
    def levanta(**kwargs):
        raise RuntimeError("gemini fora")

    monkeypatch.setattr("tcc_scraper.llm.juiza.julgar_pendentes", levanta)

    with pytest.raises(RuntimeError, match="gemini fora"):
        E.etapa_juiza(_ctx(), _Observador())
