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
ORDEM_ANO = ["google_news", "cvm_ipe", "enriquecimento", "finbert", "juiza", "build_entrega"]


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
        "CvmIpeCollector": "cvm_ipe",
        "ArticleBodyCollector": "enriquecimento",
    }
    FUNCOES = {
        "score_pending": "finbert",
        "julgar_pendentes": "juiza",
        "build_entrega": "build_entrega",
    }

    def __init__(self) -> None:
        self.chamadas: list[tuple[str, dict]] = []
        self.processadas: list[str] = []
        self.feitos: set[str] = set()
        self.hook = None

    def resolver(self, alvo: str):
        nome = alvo.split(":")[1]
        if nome in self.FUNCOES:
            coletor = self.FUNCOES[nome]

            def funcao(observador=None, **_kwargs):
                self.chamadas.append((coletor, {"resume": True, "apenas_erros": False}))
                return self._rodar(coletor, observador, resume=True)

            return funcao
        coletor = self.NOMES[nome]
        return lambda **_kwargs: _ColetorFalso(self, coletor)

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
    def __init__(self, falso: Falso, coletor: str) -> None:
        self._falso = falso
        self._coletor = coletor

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def run(self, *, observador, resume: bool = True, apenas_erros: bool = False) -> dict:
        self._falso.chamadas.append(
            (self._coletor, {"resume": resume, "apenas_erros": apenas_erros})
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


def test_passo_da_juiza_leva_o_ano_e_roda_em_backlog():
    passos = H._passos(_settings(), [2026])
    juizas = [p for p in passos if p.coletor == "juiza"]
    assert juizas == [H.Passo(2026, "juiza", {"anos": [2026], "modo": "backlog"})]


# -- ordem e flags dos coletores ---------------------------------------------------------
def test_ordem_da_carga_tem_previa_so_no_primeiro_ano(monkeypatch):
    repo, hist, falso = _montar(monkeypatch)
    hist.disparar([2026, 2025])
    hist.join(10)

    nomes = [c for c, _ in falso.chamadas]
    assert nomes == ORDEM_PREVIA + ORDEM_ANO + ORDEM_ANO
    assert nomes.count("yahoo") == 1
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
