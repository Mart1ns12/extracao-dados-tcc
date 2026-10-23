"""Catalogo de coletores: ids, flags, paridade da janela de anos e execucao com duble."""

from __future__ import annotations

from datetime import date

import pytest

from tcc_scraper.collectors.b3_indices import B3IndicesCollector
from tcc_scraper.collectors.cvm_demonstracoes import CvmDemonstracoesCollector
from tcc_scraper.collectors.cvm_ipe import CvmIpeCollector
from tcc_scraper.collectors.indices import IndicesBcbCollector
from tcc_scraper.config import Settings, get_settings
from tcc_scraper.orquestrador.coletores import CATALOGO, args_fixos, resolver

ORDEM = [
    "yahoo",
    "b3_indices",
    "b3_carteira_ibov",
    "bcb",
    "indices_bcb",
    "cvm_cadastro",
    "b3_cadastro",
    "b3_proventos",
    "cvm_itr_dfp",
    "google_news",
    "cvm_ipe",
    "enriquecimento",
    "finbert",
    "juiza",
    "build_entrega",
]
# Acoes avulsas do painel: so no `coletar`, nunca no daily nem na carga historica.
AVULSOS = ["corrigir_horas", "juiza_concordancia"]
RESULTADO_FALSO = {"total": 1, "ok": 1, "erro": 0, "itens": 0, "saturadas": 0, "interrompido": 0}


def _resolvedor_classe(registro: list):
    """Resolvedor falso: cada alvo vira uma classe de coletor que registra o que recebeu."""

    def resolver_falso(alvo: str):
        class _Coletor:
            def __init__(self, **kwargs):
                registro.append(("init", alvo, kwargs))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def run(self, *, observador, **kwargs):
                registro.append(("run", alvo, kwargs))
                return dict(RESULTADO_FALSO)

        return _Coletor

    return resolver_falso


def test_catalogo_tem_os_15_ids_da_carga_e_as_2_avulsas_no_fim():
    assert list(CATALOGO) == ORDEM + AVULSOS


def test_sem_checkpoint_por_padrao_so_nos_esperados():
    # b3_carteira_ibov: retrato do dia; com checkpoint, a rodada seguinte nao olharia a carteira.
    assert {c for c, d in CATALOGO.items() if not d.resume} == {
        "yahoo",
        "b3_proventos",
        "enriquecimento",
        "b3_carteira_ibov",
        "corrigir_horas",
        "juiza_concordancia",
    }


def test_funcoes_sao_so_as_esperadas():
    assert {c for c, d in CATALOGO.items() if not d.classe} == {
        "finbert",
        "juiza",
        "build_entrega",
        "corrigir_horas",
        "juiza_concordancia",
    }


def test_acoes_avulsas_nao_recebem_observador_e_aceitam_so_o_contrato():
    assert CATALOGO["corrigir_horas"].recebe_observador is False
    assert CATALOGO["corrigir_horas"].aceita == frozenset({"aplicar", "limite"})
    assert CATALOGO["juiza_concordancia"].recebe_observador is False
    assert CATALOGO["juiza_concordancia"].aceita == frozenset({"n", "semente"})
    # So as duas funcoes avulsas tem a flag; os demais continuam recebendo o observador.
    assert {c for c, d in CATALOGO.items() if not d.recebe_observador} == set(AVULSOS)


def test_alvos_das_acoes_avulsas_sao_as_funcoes_do_cli():
    from tcc_scraper.enrich.article_fetcher import corrigir_horas_publicacao
    from tcc_scraper.llm.juiza_concordancia import medir_concordancia

    assert resolver(CATALOGO["corrigir_horas"].alvo) is corrigir_horas_publicacao
    assert resolver(CATALOGO["juiza_concordancia"].alvo) is medir_concordancia


def test_executar_acao_avulsa_nao_passa_observador():
    chamadas: list = []

    def funcao_falsa(**kwargs):
        chamadas.append(kwargs)
        return {"gravados": 0}

    CATALOGO["corrigir_horas"].executar(
        object(), args={"dry_run": True}, resolver_alvo=lambda alvo: funcao_falsa
    )
    assert chamadas == [{"dry_run": True}]


def test_acoes_avulsas_ficam_fora_do_daily_e_da_carga_historica():
    from tcc_scraper.orquestrador.etapas import ETAPAS
    from tcc_scraper.orquestrador.historico import _passos

    s = Settings(
        gemini_api_key="", data_start=date(2025, 9, 1), data_fim_coleta=date(2026, 10, 19)
    )
    avulsas = set(AVULSOS)
    assert not avulsas & {e.coletor for e in ETAPAS}
    assert not avulsas & {p.coletor for p in _passos(s, [2025, 2026])}


def test_todo_coletor_com_anos_tem_janela():
    for cid, defn in CATALOGO.items():
        if "anos" in defn.aceita:
            assert defn.janela_anos is not None, cid


@pytest.mark.parametrize(
    ("cid", "classe"),
    [
        ("indices_bcb", IndicesBcbCollector),
        ("b3_indices", B3IndicesCollector),
        ("cvm_itr_dfp", CvmDemonstracoesCollector),
        ("cvm_ipe", CvmIpeCollector),
    ],
)
def test_janela_do_catalogo_bate_com_o_construtor(cid, classe):
    # Sem rede: o construtor so cria o cliente HTTP e calcula os anos padrao.
    s = get_settings()
    ini, fim = CATALOGO[cid].janela_anos(s)
    with classe() as c:
        assert c.anos == list(range(ini, fim + 1))


def test_args_fixos_da_juiza_roda_em_modo_backlog():
    s = Settings(gemini_api_key="")
    assert args_fixos("juiza", s) == {"modo": "backlog"}
    assert args_fixos("finbert", s) == {}


def test_juiza_aceita_anos_e_janela_de_noticias():
    s = Settings(gemini_api_key="")
    defn = CATALOGO["juiza"]
    assert defn.aceita == frozenset({"anos"})
    assert defn.janela_anos(s) == (s.inicio_noticias.year, s.fim_coleta.year)


def test_executar_usa_o_resume_padrao_do_catalogo():
    registro: list = []
    obs = object()
    resolvedor = _resolvedor_classe(registro)

    CATALOGO["yahoo"].executar(obs, resolver_alvo=resolvedor)
    CATALOGO["google_news"].executar(obs, resolver_alvo=resolvedor)

    runs = [kw for tipo, _, kw in registro if tipo == "run"]
    assert runs == [{"resume": False}, {"resume": True}]


def test_executar_resume_explicito_sobrescreve_o_padrao():
    registro: list = []
    CATALOGO["google_news"].executar(
        object(), resume=False, resolver_alvo=_resolvedor_classe(registro)
    )
    assert ("run", CATALOGO["google_news"].alvo, {"resume": False}) in registro


def test_executar_apenas_erros_substitui_resume():
    registro: list = []
    CATALOGO["bcb"].executar(
        object(), apenas_erros=True, resolver_alvo=_resolvedor_classe(registro)
    )
    assert ("run", CATALOGO["bcb"].alvo, {"apenas_erros": True}) in registro


def test_executar_passa_args_ao_construtor():
    registro: list = []
    CATALOGO["indices_bcb"].executar(
        object(), args={"anos": [2025]}, resolver_alvo=_resolvedor_classe(registro)
    )
    assert ("init", CATALOGO["indices_bcb"].alvo, {"anos": [2025]}) in registro


def test_funcao_recebe_observador_e_args():
    chamadas: list = []

    def funcao_falsa(**kwargs):
        chamadas.append(kwargs)
        return {"gravados": 0}

    obs = object()
    CATALOGO["juiza"].executar(
        obs,
        args={"limite_itens": 7},
        resolver_alvo=lambda alvo: funcao_falsa,
    )
    assert chamadas == [{"observador": obs, "limite_itens": 7}]


def test_parametros_aceitos_por_coletor():
    assert CATALOGO["yahoo"].aceita == frozenset({"tickers"})
    assert CATALOGO["google_news"].aceita == frozenset({"tickers", "desde", "ate"})
    assert CATALOGO["cvm_itr_dfp"].aceita == frozenset({"anos"})
