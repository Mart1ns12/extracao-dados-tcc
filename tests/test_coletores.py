"""Catalogo de coletores: ids, flags, paridade da janela de anos e execucao com duble."""

from __future__ import annotations

import pytest

from tcc_scraper.collectors.cvm_demonstracoes import CvmDemonstracoesCollector
from tcc_scraper.collectors.cvm_ipe import CvmIpeCollector
from tcc_scraper.collectors.indices import IndicesBcbCollector
from tcc_scraper.config import Settings, get_settings
from tcc_scraper.orquestrador.coletores import CATALOGO, args_fixos

ORDEM = [
    "yahoo",
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


def test_catalogo_tem_os_13_ids_na_ordem_da_carga():
    assert list(CATALOGO) == ORDEM


def test_sem_checkpoint_por_padrao_so_nos_tres_esperados():
    assert {c for c, d in CATALOGO.items() if not d.resume} == {
        "yahoo",
        "b3_proventos",
        "enriquecimento",
    }


def test_funcoes_sao_so_as_tres_esperadas():
    assert {c for c, d in CATALOGO.items() if not d.classe} == {
        "finbert",
        "juiza",
        "build_entrega",
    }


def test_todo_coletor_com_anos_tem_janela():
    for cid, defn in CATALOGO.items():
        if "anos" in defn.aceita:
            assert defn.janela_anos is not None, cid


@pytest.mark.parametrize(
    ("cid", "classe"),
    [
        ("indices_bcb", IndicesBcbCollector),
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


def test_args_fixos_da_juiza_vem_do_teto_diario():
    s = Settings(gemini_api_key="")
    assert args_fixos("juiza", s) == {"limite_itens": s.juiza_max_itens_dia}
    assert args_fixos("finbert", s) == {}


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
