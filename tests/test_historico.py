"""Carga historica sem banco e sem rede: RepositorioMemoria e coletores de mentira.

Os coletores de mentira contam unidades, obedecem `deve_parar` entre unidades e pulam o
que ja foi feito quando `resume=True` (um checkpoint de brinquedo). Assim a pausa, a
retomada e o cancelamento sao testados de verdade, sem Google, Yahoo ou FinBERT.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from tcc_scraper.collectors.base import AbortarColeta
from tcc_scraper.config import Settings
from tcc_scraper.orquestrador import historico as H
from tcc_scraper.orquestrador.historico import (
    Historico,
    HistoricoEmAndamento,
    TransicaoInvalida,
    segmentos,
)
from tcc_scraper.orquestrador.repositorio import RepositorioMemoria

AGORA = datetime(2026, 10, 19, 18, 30, tzinfo=UTC)
ORDEM_PREVIA = [
    "yahoo",
    "b3_indices",
    "bcb",
    "indices_bcb",
    "cvm_cadastro",
    "b3_cadastro",
    "b3_proventos",
    "cvm_itr_dfp",
]
ORDEM_GOOGLE_ANO = ["google_news", "enriquecimento"]
# Ids no painel (juiza_wp e a juiza do ano, que roda no coletor "juiza").
ORDEM_PRINCIPAL_ANO = [
    "wordpress",
    "processar_wp",
    "cvm_ipe",
    "juiza_wp",
    "finbert",
    "juiza",
    "build_entrega",
    "build_dataset",
]
# Coletores que a principal chama, na ordem (juiza aparece duas vezes, como no catalogo).
ORDEM_PRINCIPAL_COLETORES = [
    "wordpress",
    "processar_wp",
    "cvm_ipe",
    "juiza",
    "finbert",
    "juiza",
    "build_entrega",
    "build_dataset",
]


def _settings(data_start: date = date(2025, 9, 1)) -> Settings:
    return Settings(
        gemini_api_key="",
        data_start=data_start,
        data_fim_coleta=date(2026, 10, 19),
    )


class Falso:
    """Coletores de mentira. `hook(coletor, key)` roda antes de cada unidade (pausar, travar)."""

    NOMES = {
        "YahooPriceCollector": "yahoo",
        "B3IndicesCollector": "b3_indices",
        "BcbCollector": "bcb",
        "IndicesBcbCollector": "indices_bcb",
        "CvmCadastroCollector": "cvm_cadastro",
        "B3CadastroCollector": "b3_cadastro",
        "B3ProventosCollector": "b3_proventos",
        "CvmDemonstracoesCollector": "cvm_itr_dfp",
        "GoogleNewsCollector": "google_news",
        "WordPressCollector": "wordpress",
        "CvmIpeCollector": "cvm_ipe",
        "ArticleBodyCollector": "enriquecimento",
    }
    FUNCOES = {
        "score_pending": "finbert",
        "julgar_pendentes": "juiza",
        "processar_wp": "processar_wp",
        "build_entrega": "build_entrega",
    }

    def __init__(self) -> None:
        self.chamadas: list[tuple[str, dict]] = []
        self.processadas: list[str] = []
        self.feitos: set[str] = set()
        self.hook = None

    def resolver(self, alvo: str):
        nome = alvo.split(":")[1]
        if nome == "build_dataset_v1":
            # Sem observador, como o de verdade: so anota a chamada e devolve um resultado.
            def dataset(**kwargs):
                self.chamadas.append(
                    ("build_dataset", {"resume": True, "apenas_erros": False, "args": kwargs})
                )
                return {"linhas": 0}

            return dataset
        if nome in self.FUNCOES:
            coletor = self.FUNCOES[nome]

            def funcao(observador=None, **kwargs):
                self.chamadas.append(
                    (coletor, {"resume": True, "apenas_erros": False, "args": kwargs})
                )
                return self._rodar(coletor, observador, resume=True)

            return funcao
        coletor = self.NOMES[nome]
        return lambda **kwargs: _ColetorFalso(self, coletor, kwargs)

    def _rodar(self, coletor: str, observador, *, resume: bool) -> dict:
        chaves = [f"{coletor}-{i}" for i in range(3)]
        if resume:
            chaves = [k for k in chaves if k not in self.feitos]
        observador.plano(len(chaves))
        itens = 0
        for key in chaves:
            if self.hook is not None:
                self.hook(coletor, key)
            if observador.deve_parar():
                return {"total": len(chaves), "itens": itens, "interrompido": 1}
            self.processadas.append(key)
            self.feitos.add(key)
            observador.unidade(key, ok=True, itens=2, saturada=False, erro=None)
            itens += 2
        return {"total": len(chaves), "ok": len(chaves), "erro": 0, "itens": itens}


class _ColetorFalso:
    def __init__(self, falso: Falso, coletor: str, args: dict | None = None) -> None:
        self._falso = falso
        self._coletor = coletor
        self._args = args or {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def run(self, *, observador, resume: bool = True, apenas_erros: bool = False) -> dict:
        self._falso.chamadas.append(
            (
                self._coletor,
                {"resume": resume, "apenas_erros": apenas_erros, "args": self._args},
            )
        )
        return self._falso._rodar(self._coletor, observador, resume=resume)


def _montar(monkeypatch, settings: Settings | None = None):
    falso = Falso()
    monkeypatch.setattr(H, "_resolver", falso.resolver)
    repo = RepositorioMemoria()
    hist = Historico(repo, settings or _settings(), agora=lambda: AGORA)
    return repo, hist, falso


def _esperar(condicao, limite: float = 5.0) -> None:
    fim = time.monotonic() + limite
    while not condicao():
        if time.monotonic() > fim:
            raise AssertionError("tempo esgotado esperando a condicao")
        time.sleep(0.005)


def _linha_progresso(repo, ano: int, coletor: str) -> dict:
    return next(r for r in repo.progresso_historico(ano) if r["coletor"] == coletor)


def _registro(status: str, **extra) -> dict:
    base = {
        "id": "hist-teste",
        "status": status,
        "modo": "pendentes",
        "anos": [2026],
        "ano_atual": None,
        "coletor_atual": None,
        "unidade_atual": None,
        "iniciada_em": AGORA,
        "finalizada_em": None,
        "itens_por_min": 0.0,
        "erro": None,
        "atualizada_em": AGORA,
    }
    base.update(extra)
    return base


# -- janela de coleta ------------------------------------------------------------------
def test_segmentos_da_janela_atual_sao_2026_e_2025():
    segs = segmentos(_settings(date(2025, 9, 1)))
    assert [(s.ano, s.inicio, s.fim) for s in segs] == [
        (2026, date(2026, 1, 1), date(2026, 10, 20)),
        (2025, date(2025, 8, 1), date(2026, 1, 1)),
    ]


def test_segmentos_desde_2021_somam_seis_anos():
    segs = segmentos(_settings(date(2021, 9, 1)))
    assert [s.ano for s in segs] == [2026, 2025, 2024, 2023, 2022, 2021]


def test_segmentos_filtra_por_anos_pedidos():
    assert [s.ano for s in segmentos(_settings(), [2025])] == [2025]


def test_passos_da_juiza_levam_o_ano_e_rodam_em_backlog():
    passos = H._passos(_settings(), [2026])
    juizas = [p for p in passos if p.coletor == "juiza"]
    assert juizas == [
        H.Passo(
            2026,
            "juiza",
            {"anos": [2026], "origens": ["wordpress", "cvm_ipe"], "modo": "backlog"},
            painel="juiza_wp",
        ),
        H.Passo(2026, "juiza", {"anos": [2026], "modo": "backlog"}, depende_google=True),
    ]


def test_finbert_recebe_o_ano_e_espera_o_google():
    passos = H._passos(_settings(), [2026])
    finbert = next(p for p in passos if p.coletor == "finbert")
    assert finbert == H.Passo(2026, "finbert", {"anos": [2026]}, depende_google=True)


def test_passos_wordpress_recuam_31_dias_e_processamento_fica_no_segmento():
    s = _settings(date(2025, 9, 1))
    passos = H._passos(s, [2025])
    ini = s.inicio_noticias
    assert [p for p in passos if p.coletor == "wordpress"] == [
        H.Passo(2025, "wordpress", {"desde": ini - timedelta(days=31), "ate": date(2026, 1, 1)}),
    ]
    assert [p for p in passos if p.coletor == "processar_wp"] == [
        H.Passo(2025, "processar_wp", {"desde": ini, "ate": date(2026, 1, 1)}),
    ]


def test_duas_trilhas_separam_google_do_resto():
    passos = H._passos(_settings(), [2026, 2025])
    google = [(p.ano, p.coletor) for p in passos if p.trilha == H.TRILHA_GOOGLE]
    assert google == [
        (2026, "google_news"),
        (2026, "enriquecimento"),
        (2025, "google_news"),
        (2025, "enriquecimento"),
    ]
    principal_2026 = [
        p.id_painel for p in passos if p.trilha == H.TRILHA_PRINCIPAL and p.ano == 2026
    ]
    assert principal_2026 == ORDEM_PREVIA + ORDEM_PRINCIPAL_ANO


def test_so_finbert_juiza_e_entrega_dependem_do_google():
    passos = H._passos(_settings(), [2026, 2025])
    dependentes = {p.id_painel for p in passos if p.depende_google}
    assert dependentes == {"finbert", "juiza", "build_entrega", "build_dataset"}


def test_build_dataset_vem_logo_depois_de_build_entrega_em_cada_segmento():
    passos = H._passos(_settings(), [2026, 2025])
    principal = [(p.ano, p.coletor) for p in passos if p.trilha == H.TRILHA_PRINCIPAL]
    for ano in (2026, 2025):
        coletores = [c for a, c in principal if a == ano]
        assert coletores[coletores.index("build_entrega") + 1] == "build_dataset"
        assert coletores.count("build_dataset") == 1
    dataset = next(p for p in passos if p.coletor == "build_dataset")
    assert dataset.args == {}
    assert dataset.trilha == H.TRILHA_PRINCIPAL


def test_juiza_wp_leva_origens_sem_google_e_o_ano():
    passos = H._passos(_settings(), [2026])
    juiza_wp = next(p for p in passos if p.id_painel == "juiza_wp")
    assert juiza_wp.args == {
        "anos": [2026],
        "origens": ["wordpress", "cvm_ipe"],
        "modo": "backlog",
    }


def test_wp_desligado_tira_wordpress_e_processamento_da_carga():
    s = Settings(
        gemini_api_key="",
        data_start=date(2025, 9, 1),
        data_fim_coleta=date(2026, 10, 19),
        wp_enabled=False,
    )
    nomes = [p.coletor for p in H._passos(s, [2026])]
    assert "google_news" in nomes
    assert "wordpress" not in nomes
    assert "processar_wp" not in nomes


# -- ordem e flags dos coletores ---------------------------------------------------------
def test_carga_completa_com_duas_trilhas_termina_concluida(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    hist.disparar([2026, 2025])
    hist.join(10)

    nomes = [c for c, _ in falso.chamadas]
    assert nomes.count("yahoo") == 1
    # Cada trilha roda em ordem; so a intercalacao entre elas nao e determinista.
    assert [c for c in nomes if c not in ORDEM_GOOGLE_ANO] == (
        ORDEM_PREVIA + ORDEM_PRINCIPAL_COLETORES * 2
    )
    assert [c for c in nomes if c in ORDEM_GOOGLE_ANO] == ORDEM_GOOGLE_ANO * 2
    assert repo.historico_atual()["status"] == "concluida"


def test_finbert_e_juiza_do_ano_so_rodam_depois_do_google_do_ano(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    libera = threading.Event()

    def trava(coletor, key):
        if coletor == "google_news":
            libera.wait(5)

    falso.hook = trava
    hist.disparar([2026])
    # A principal chega ao portao do ano (juiza_wp ja rodou) enquanto o Google ainda coleta.
    _esperar(lambda: "juiza" in [c for c, _ in falso.chamadas])
    nomes = [c for c, _ in falso.chamadas]
    assert "finbert" not in nomes
    assert "enriquecimento" not in nomes

    libera.set()
    hist.join(10)

    nomes = [c for c, _ in falso.chamadas]
    assert nomes.index("finbert") > nomes.index("enriquecimento")
    assert repo.historico_atual()["status"] == "concluida"


def test_pendentes_usa_resume_e_reprocessar_erros_usa_apenas_erros(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    hist.disparar([2026], "pendentes")
    hist.join(10)
    yahoo = next(kw for c, kw in falso.chamadas if c == "yahoo")
    gnews = next(kw for c, kw in falso.chamadas if c == "google_news")
    assert yahoo["resume"] is False
    assert gnews["resume"] is True

    repo2, hist2, falso2 = _montar(monkeypatch)
    hist2.disparar([2026], "reprocessar_erros")
    hist2.join(10)
    assert all(kw["apenas_erros"] for c, kw in falso2.chamadas if c == "google_news")


def test_modo_invalido_e_ano_fora_da_janela_sao_recusados(monkeypatch):
    _, hist, _ = _montar(monkeypatch)
    with pytest.raises(ValueError, match="modo inválido"):
        hist.disparar([2026], "qualquer")
    with pytest.raises(ValueError, match="janela de coleta"):
        hist.disparar([1990])


# -- pausa, retomada e cancelamento --------------------------------------------------------
def test_pausar_entre_unidades_e_retomar_continua_de_onde_parou(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    disparou = []

    def hook(coletor, key):
        if coletor == "google_news" and key == "google_news-1" and not disparou:
            disparou.append(key)
            assert hist.controlar("pausar") == "pausando"

    falso.hook = hook
    hist.disparar([2026, 2025])
    _esperar(lambda: repo.historico_atual()["status"] == "pausada")

    # `pausada` so e gravada quando as duas trilhas estao estacionadas.
    assert hist._paradas == hist._vivas == 2
    gnews = _linha_progresso(repo, 2026, "google_news")
    assert gnews["unidades_feitas"] == 1
    assert gnews["status_segmento"] == "pausado"

    assert hist.controlar("retomar") == "em_andamento"
    hist.join(10)

    assert repo.historico_atual()["status"] == "concluida"
    # Nenhuma unidade refeita: a retomada seguiu de onde a pausa cortou.
    assert falso.processadas.count("google_news-0") == 1
    assert falso.processadas.count("google_news-1") == 1
    assert falso.processadas.count("google_news-2") == 1
    gnews = _linha_progresso(repo, 2026, "google_news")
    assert gnews["unidades_feitas"] == 3
    assert gnews["unidades_total"] == 3
    assert gnews["status_segmento"] == "concluido"


def test_cancelar_durante_execucao_grava_cancelada_com_finalizada_em(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    cancelou = []

    def hook(coletor, key):
        if coletor == "google_news" and key == "google_news-1" and not cancelou:
            cancelou.append(key)
            assert hist.controlar("cancelar") == "cancelando"

    falso.hook = hook
    hist.disparar([2026, 2025])
    hist.join(10)

    atual = repo.historico_atual()
    assert atual["status"] == "cancelada"
    assert atual["finalizada_em"] is not None
    assert "google_news-2" not in falso.processadas


def test_cancelar_pausada_vai_direto_a_cancelada(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    pausou = []

    def hook(coletor, key):
        if coletor == "google_news" and key == "google_news-1" and not pausou:
            pausou.append(key)
            hist.controlar("pausar")

    falso.hook = hook
    hist.disparar([2026, 2025])
    _esperar(lambda: repo.historico_atual()["status"] == "pausada")

    assert hist.controlar("cancelar") == "cancelando"
    hist.join(10)
    atual = repo.historico_atual()
    assert atual["status"] == "cancelada"
    assert atual["finalizada_em"] is not None


def test_transicoes_invalidas_dizem_o_motivo(monkeypatch):
    repo, hist, _ = _montar(monkeypatch)
    hist.disparar([2026])
    hist.join(10)
    assert repo.historico_atual()["status"] == "concluida"

    casos = {
        "pausar": r"pausar exige carga em_andamento \(atual: concluida\)",
        "retomar": r"retomar exige carga pausada \(atual: concluida\)",
        "cancelar": r"cancelar exige carga em_andamento, pausando ou pausada",
    }
    for acao, motivo in casos.items():
        with pytest.raises(TransicaoInvalida, match=motivo):
            hist.controlar(acao)


def test_controle_sem_carga_nenhuma_e_transicao_invalida(monkeypatch):
    _, hist, _ = _montar(monkeypatch)
    with pytest.raises(TransicaoInvalida, match="nenhuma existe"):
        hist.controlar("pausar")


# -- bloqueio e disparo ----------------------------------------------------------------
def test_bloqueia_em_rodando_e_pausando_mas_nao_pausada(monkeypatch):
    repo, hist, _ = _montar(monkeypatch)
    repo.criar_historico(_registro("em_andamento"))
    assert hist.bloqueia() is True
    repo.atualizar_historico("hist-teste", status="pausando")
    assert hist.bloqueia() is True
    repo.atualizar_historico("hist-teste", status="pausada")
    assert hist.bloqueia() is False


def test_disparar_duas_vezes_recusa_a_segunda(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    solta = threading.Event()

    def trava(coletor, key):
        if coletor == "yahoo":
            solta.wait(5)

    falso.hook = trava
    hist.disparar([2026])
    try:
        with pytest.raises(HistoricoEmAndamento):
            hist.disparar([2026])
    finally:
        solta.set()
        hist.join(10)
    assert repo.historico_atual()["status"] == "concluida"


def test_id_da_carga_tem_segundos_e_dois_disparos_no_mesmo_minuto_nao_colidem(monkeypatch):
    falso = Falso()
    monkeypatch.setattr(H, "_resolver", falso.resolver)
    repo = RepositorioMemoria()
    relogio = [AGORA]  # 18:30:00 UTC = 15:30:00 em Sao Paulo
    hist = Historico(repo, _settings(), agora=lambda: relogio[0])
    hist.disparar([2026])
    hist.join(10)
    assert repo.historico_atual()["id"] == "hist-20261019-153000"
    relogio[0] = AGORA + timedelta(seconds=7)
    hist.disparar([2026])
    hist.join(10)
    assert repo.historico_atual()["id"] == "hist-20261019-153007"


def test_disparo_cria_linhas_pendentes_de_todos_os_passos(monkeypatch):
    repo, hist, _ = _montar(monkeypatch)
    repo.criar_historico(_registro("concluida"))  # carga anterior encerrada nao bloqueia
    hist.disparar([2026, 2025])
    hist.join(10)
    coletores_2025 = {r["coletor"] for r in repo.progresso_historico(2025)}
    assert "google_news" in coletores_2025
    assert "yahoo" not in coletores_2025  # previa so no primeiro ano


# -- reinicio ------------------------------------------------------------------------
def test_recuperar_retoma_em_andamento_e_fecha_cancelando(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    repo.criar_historico(_registro("em_andamento", anos=[2026]))
    hist.recuperar_apos_reinicio()
    hist.join(10)
    assert repo.historico_atual()["status"] == "concluida"
    assert "yahoo" in [c for c, _ in falso.chamadas]

    repo2, hist2, _ = _montar(monkeypatch)
    repo2.criar_historico(_registro("cancelando", anos=[2026]))
    hist2.recuperar_apos_reinicio()
    atual = repo2.historico_atual()
    assert atual["status"] == "cancelada"
    assert atual["finalizada_em"] is not None


# -- progresso: unidade em curso e ultimo erro -----------------------------------------
def _progresso_direto(repo: RepositorioMemoria | None = None):
    """Um ProgressoHistorico solto, sem orquestrador: testa so o observador."""
    if repo is None:
        repo = RepositorioMemoria()
        repo.criar_historico(_registro("em_andamento"))
        repo.criar_progresso_historico(
            [
                {
                    "ano": 2026,
                    "coletor": "google_news",
                    "unidades_total": 0,
                    "unidades_feitas": 0,
                    "unidades_erro": 0,
                    "itens": 0,
                    "status_segmento": "pendente",
                    "unidade_atual": None,
                    "ultimo_erro_unidade": None,
                    "ultimo_erro_mensagem": None,
                    "atualizado_em": AGORA,
                }
            ]
        )
    progresso = H.ProgressoHistorico(
        repo,
        "hist-teste",
        H.Passo(2026, "google_news"),
        pausar=threading.Event(),
        cancelar=threading.Event(),
        ritmo=H._Ritmo(),
        agora=lambda: AGORA,
    )
    return repo, progresso


def test_inicio_unidade_grava_a_unidade_em_curso_na_hora():
    repo, progresso = _progresso_direto()
    progresso.inicio_unidade("PETR4|x")
    assert _linha_progresso(repo, 2026, "google_news")["unidade_atual"] == "PETR4|x"


def test_erro_de_unidade_grava_na_hora_e_flush_seguinte_nao_apaga():
    repo, progresso = _progresso_direto()
    progresso.unidade("k", ok=False, itens=0, saturada=False, erro="boom")
    linha = _linha_progresso(repo, 2026, "google_news")
    assert linha["ultimo_erro_unidade"] == "k"
    assert linha["ultimo_erro_mensagem"] == "boom"

    progresso.unidade("k2", ok=True, itens=1, saturada=False, erro=None)
    progresso.flush()
    linha = _linha_progresso(repo, 2026, "google_news")
    assert linha["ultimo_erro_unidade"] == "k"
    assert linha["ultimo_erro_mensagem"] == "boom"


def test_objeto_novo_apos_reinicio_nao_apaga_o_ultimo_erro_gravado():
    repo, progresso = _progresso_direto()
    progresso.unidade("k", ok=False, itens=0, saturada=False, erro="boom")
    _, reiniciado = _progresso_direto(repo)  # objeto novo, sem erro em memoria
    reiniciado.flush()
    assert _linha_progresso(repo, 2026, "google_news")["ultimo_erro_mensagem"] == "boom"


def test_falha_do_passo_grava_o_erro_no_progresso_e_carga_falha(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)

    def hook(coletor, key):
        if coletor == "cvm_ipe":
            raise RuntimeError("x")

    falso.hook = hook
    hist.disparar([2026])
    hist.join(10)

    assert repo.historico_atual()["status"] == "falhou"
    linha = _linha_progresso(repo, 2026, "cvm_ipe")
    assert linha["status_segmento"] == "falhou"
    assert linha["ultimo_erro_mensagem"].startswith("RuntimeError: x")


def test_passo_concluido_zera_a_unidade_em_curso(monkeypatch):
    repo, hist, _ = _montar(monkeypatch)
    hist.disparar([2026])
    hist.join(10)
    linha = _linha_progresso(repo, 2026, "google_news")
    assert linha["status_segmento"] == "concluido"
    assert linha["unidade_atual"] is None


def test_disparo_cria_linhas_com_unidade_e_erro_vazios(monkeypatch):
    repo, hist, _ = _montar(monkeypatch)
    criadas: list[dict] = []
    original = repo.criar_progresso_historico

    def espiao(linhas):
        criadas.extend(linhas)
        original(linhas)

    monkeypatch.setattr(repo, "criar_progresso_historico", espiao)
    hist.disparar([2026])
    hist.join(10)

    assert criadas
    for linha in criadas:
        assert linha["unidade_atual"] is None
        assert linha["ultimo_erro_unidade"] is None
        assert linha["ultimo_erro_mensagem"] is None


# -- duas trilhas: falha, retomada e concorrencia -------------------------------------------
def test_falha_numa_trilha_aborta_a_outra_e_a_carga_falha(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)

    def hook(coletor, key):
        if coletor == "cvm_ipe":
            raise RuntimeError("x")

    falso.hook = hook
    hist.disparar([2026, 2025])
    hist.join(10)

    atual = repo.historico_atual()
    assert atual["status"] == "falhou"
    assert "RuntimeError" in atual["erro"]
    assert hist.ocupado is False


def _criar_linhas_do_plano(repo, anos: list[int], concluidos: set[str]) -> None:
    """Linhas de progresso de todos os passos, como o disparo as cria; `concluidos` ja rodaram."""
    repo.criar_progresso_historico(
        [
            {
                "ano": p.ano,
                "coletor": p.id_painel,
                "unidades_total": 0,
                "unidades_feitas": 0,
                "unidades_erro": 0,
                "itens": 0,
                "status_segmento": "concluido" if p.id_painel in concluidos else "pendente",
                "unidade_atual": None,
                "ultimo_erro_unidade": None,
                "ultimo_erro_mensagem": None,
                "atualizado_em": AGORA,
            }
            for p in H._passos(_settings(), anos)
        ]
    )


def test_retomada_pelo_status_nao_refaz_os_passos_concluidos(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    repo.criar_historico(_registro("em_andamento", anos=[2026]))
    concluidos = set(ORDEM_PREVIA) | {"google_news"}
    _criar_linhas_do_plano(repo, [2026], concluidos)

    hist.recuperar_apos_reinicio()
    hist.join(10)

    chamados = {c for c, _ in falso.chamadas}
    assert not chamados & concluidos
    assert {"enriquecimento", "wordpress", "finbert", "build_entrega"} <= chamados
    assert repo.historico_atual()["status"] == "concluida"


def test_ritmo_aguenta_duas_trilhas_registrando_ao_mesmo_tempo():
    ritmo = H._Ritmo()
    erros: list[Exception] = []

    def registra() -> None:
        try:
            for _ in range(1000):
                ritmo.registrar(AGORA, 1)
                ritmo.media(AGORA)
        except Exception as exc:  # so chega aqui se a fila for corrompida
            erros.append(exc)

    threads = [threading.Thread(target=registra) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert erros == []
    # Todas as amostras caem no mesmo instante: 2000 itens em 1 minuto (piso de 1 min).
    assert ritmo.media(AGORA) == 2000.0


# -- falha da trilha Google: so ela para, e o portao do ano nao trava -------------------------
def _google_bloqueada(falso: Falso) -> None:
    """O Google bloqueia (503) logo na primeira unidade do google_news."""

    def hook(coletor, key):
        if coletor == "google_news":
            raise AbortarColeta("bloqueado pelo Google (503)")

    falso.hook = hook


def test_falha_da_google_nao_derruba_a_principal(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    _google_bloqueada(falso)
    hist.disparar([2026, 2025])
    hist.join(10)

    assert hist.ocupado is False
    # A principal foi ate o fim: globais e, em cada ano, as etapas que nao dependem da Google.
    for coletor in ORDEM_PREVIA:
        assert _linha_progresso(repo, 2026, coletor)["status_segmento"] == "concluido"
    for ano in (2026, 2025):
        for coletor in ("wordpress", "processar_wp", "cvm_ipe", "juiza_wp"):
            assert _linha_progresso(repo, ano, coletor)["status_segmento"] == "concluido"
    # O passo que falhou fica `falhou`; a carga termina `falhou` com a mensagem da Google.
    assert _linha_progresso(repo, 2026, "google_news")["status_segmento"] == "falhou"
    atual = repo.historico_atual()
    assert atual["status"] == "falhou"
    assert atual["erro"].startswith("trilha google: AbortarColeta")


def test_portao_sem_google_nao_roda_e_nao_trava(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    original = H._resolver

    # Google conclui 2026 e bloqueia no segmento de 2025 (o primeiro passo dele).
    def resolver_com_bloqueio(alvo: str):
        fabrica = original(alvo)
        if alvo.split(":")[1] != "GoogleNewsCollector":
            return fabrica

        def com_bloqueio(**kwargs):
            if kwargs["desde"].year == 2025:
                raise AbortarColeta("bloqueado pelo Google (503)")
            return fabrica(**kwargs)

        return com_bloqueio

    monkeypatch.setattr(H, "_resolver", resolver_com_bloqueio)
    hist.disparar([2026, 2025])
    hist.join(10)

    # Nao travou: a thread terminou (sem o join com timeout ela ficaria viva).
    assert hist.ocupado is False
    assert repo.historico_atual()["status"] == "falhou"
    # O portao de 2026 abriu: finbert, juiza, entrega e dataset de 2026 rodaram.
    for coletor in ("finbert", "juiza", "build_entrega", "build_dataset"):
        assert _linha_progresso(repo, 2026, coletor)["status_segmento"] == "concluido"
    # O portao de 2025 nunca abriu: esses passos nao rodaram e ficam pendentes.
    for coletor in ("finbert", "juiza", "build_entrega", "build_dataset"):
        assert _linha_progresso(repo, 2025, coletor)["status_segmento"] == "pendente"
    assert [c for c, _ in falso.chamadas].count("finbert") == 1
    # A principal seguiu para o segmento de 2025 e concluiu o que nao depende da Google.
    assert _linha_progresso(repo, 2025, "wordpress")["status_segmento"] == "concluido"
    assert _linha_progresso(repo, 2025, "juiza_wp")["status_segmento"] == "concluido"


def test_falha_da_principal_ainda_derruba_a_google(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)

    def hook(coletor, key):
        if coletor == "google_news":
            # A Google so para no ponto de parada: espera a principal cair antes de seguir.
            _esperar(lambda: hist._abortar.is_set())
        if coletor == "cvm_ipe":
            raise RuntimeError("x")

    falso.hook = hook
    hist.disparar([2026, 2025])
    hist.join(10)

    atual = repo.historico_atual()
    assert atual["status"] == "falhou"
    assert "RuntimeError" in atual["erro"]
    assert "trilha google" not in atual["erro"]
    assert hist.ocupado is False
    # A Google parou no meio: o resto do 2026 e o 2025 nao rodaram.
    assert _linha_progresso(repo, 2026, "enriquecimento")["status_segmento"] == "pendente"
    assert _linha_progresso(repo, 2025, "google_news")["status_segmento"] == "pendente"


def test_pausa_e_retomada_funcionam_com_a_google_ja_falhada(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    pausou: list[bool] = []

    def hook(coletor, key):
        if coletor == "google_news":
            raise AbortarColeta("bloqueado pelo Google (503)")
        if coletor == "wordpress" and not pausou:
            pausou.append(True)
            # So pausa depois que a Google ja encerrou: o teste nao depende da ordem das threads.
            _esperar(lambda: hist._google_encerrada)
            hist.controlar("pausar")

    falso.hook = hook
    hist.disparar([2026])
    # Com a Google morta, a principal sozinha estaciona e a carga vira `pausada`.
    _esperar(lambda: repo.historico_atual()["status"] == "pausada")
    assert hist.controlar("retomar") == "em_andamento"
    hist.join(10)

    assert hist.ocupado is False
    assert _linha_progresso(repo, 2026, "cvm_ipe")["status_segmento"] == "concluido"
    atual = repo.historico_atual()
    assert atual["status"] == "falhou"
    assert atual["erro"].startswith("trilha google")


def test_cancelar_com_a_google_ja_falhada_encerra_a_carga(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    cancelou: list[bool] = []

    def hook(coletor, key):
        if coletor == "google_news":
            raise AbortarColeta("bloqueado pelo Google (503)")
        if coletor == "wordpress" and not cancelou:
            cancelou.append(True)
            _esperar(lambda: hist._google_encerrada)
            hist.controlar("cancelar")

    falso.hook = hook
    hist.disparar([2026])
    hist.join(10)

    assert hist.ocupado is False
    # Falha da Google prevalece sobre o cancelamento (mesma precedencia da falha da principal).
    atual = repo.historico_atual()
    assert atual["status"] == "falhou"
    assert atual["erro"].startswith("trilha google")
    assert _linha_progresso(repo, 2026, "cvm_ipe")["status_segmento"] == "pendente"


# -- log por passo ---------------------------------------------------------------------------
def _logs_do_passo(repo, hist_id: str, etapa: str) -> list[dict]:
    return [
        log
        for log in repo.logs
        if log["origem"] == "historico" and log["execucao_id"] == hist_id and log["etapa"] == etapa
    ]


def test_passo_gera_linha_de_iniciado_e_concluido_com_contadores(monkeypatch):
    repo, hist, _ = _montar(monkeypatch)
    hist_id = hist.disparar([2026])
    hist.join(10)

    logs = _logs_do_passo(repo, hist_id, "yahoo")
    assert [log["mensagem"] for log in logs] == [
        "passo yahoo de 2026 (trilha principal) iniciado",
        "passo yahoo de 2026 (trilha principal) concluído: 3 de 3 unidades",
    ]
    assert all(log["nivel"] == "info" for log in logs)


def test_passo_ja_concluido_gera_linha_de_pulado(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    repo.criar_historico(_registro("em_andamento", anos=[2026]))
    _criar_linhas_do_plano(repo, [2026], {"yahoo"})

    hist.recuperar_apos_reinicio()
    hist.join(10)

    logs = _logs_do_passo(repo, "hist-teste", "yahoo")
    assert [log["mensagem"] for log in logs] == [
        "passo yahoo de 2026 (trilha principal) pulado: já constava como concluído nesta carga",
    ]
    assert "yahoo" not in {c for c, _ in falso.chamadas}


def test_passo_que_falha_gera_linha_de_erro(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)

    def hook(coletor, key):
        if coletor == "cvm_ipe":
            raise RuntimeError("x")

    falso.hook = hook
    hist_id = hist.disparar([2026])
    hist.join(10)

    logs = _logs_do_passo(repo, hist_id, "cvm_ipe")
    assert [(log["nivel"], log["mensagem"]) for log in logs] == [
        ("info", "passo cvm_ipe de 2026 (trilha principal) iniciado"),
        ("erro", "passo cvm_ipe de 2026 (trilha principal) falhou: RuntimeError: x"),
    ]


def test_reprocessar_sem_erros_avisa_que_nada_foi_reprocessado(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    original = falso._rodar

    def sem_erros(coletor, observador, *, resume):
        if coletor == "cvm_ipe":
            observador.plano(0)
            return {"total": 0, "ok": 0, "erro": 0, "itens": 0}
        return original(coletor, observador, resume=resume)

    falso._rodar = sem_erros
    hist_id = hist.disparar([2026], "reprocessar_erros")
    hist.join(10)

    avisos = [
        log for log in _logs_do_passo(repo, hist_id, "cvm_ipe") if log["nivel"] == "aviso"
    ]
    assert len(avisos) == 1
    assert avisos[0]["mensagem"] == (
        f"carga {hist_id}: 2026 cvm_ipe: nenhuma unidade com erro; nada reprocessado "
        "(não significa que o ano foi coletado; use o modo pendentes)"
    )
    assert _linha_progresso(repo, 2026, "cvm_ipe")["status_segmento"] == "concluido"
