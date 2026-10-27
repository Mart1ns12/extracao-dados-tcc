"""Classificador por codigo (F5): uma camada por teste, com EvidenciaPar montada a mao."""
from __future__ import annotations

import pytest

from tcc_scraper.db.models_fontes import StagingNoticiaJulgamento
from tcc_scraper.enrich.casador import MIN_CORPO, EvidenciaPar
from tcc_scraper.entrega.filtros import entra_por_rotulos
from tcc_scraper.llm.juiza_codigo import (
    CAMADAS_ATIVAS,
    LEAD,
    REGRAS_VERSAO,
    classificar_par,
    rotulo_modelo,
)
from tcc_scraper.llm.juiza_regras import nomes_por_ticker, regra_barata
from tcc_scraper.llm.juiza_schema import JulgamentoItem

NOMES = nomes_por_ticker(
    {
        "PETR4": "Petrobras",
        "VALE3": "Vale",
        "BRAV3": "Brava Energia",
        "ITUB4": "Itau",
        "BBDC4": "Bradesco",
    },
    {},
)

URL_NEUTRA = "https://www.infomoney.com.br/mercados/noticia-qualquer/"


def _ev(
    ticker: str = "PETR4",
    *,
    n_titulo: int = 0,
    n_corpo: int = 0,
    n_titulo_revisavel: int | None = None,
    n_corpo_revisavel: int | None = None,
    primeiro_bloco: int | None = None,
    outras: int = 0,
) -> EvidenciaPar:
    return EvidenciaPar(
        ticker=ticker,
        n_titulo=n_titulo,
        n_corpo=n_corpo,
        n_titulo_revisavel=n_titulo if n_titulo_revisavel is None else n_titulo_revisavel,
        n_corpo_revisavel=n_corpo if n_corpo_revisavel is None else n_corpo_revisavel,
        primeiro_bloco=primeiro_bloco,
        outras_empresas_titulo=outras,
        invalidas={},
    )


def _artigo(titulo: str, url: str = URL_NEUTRA, origem: str = "wordpress") -> dict:
    return {
        "title": titulo,
        "source_domain": "infomoney.com.br",
        "origem": origem,
        "url": url,
    }


def _valida(julgamento: dict, ticker: str = "PETR4") -> JulgamentoItem:
    return JulgamentoItem(id="42", ticker=ticker, **julgamento)


def test_revisavel_vai_para_R_com_julgamento_None():
    ev = _ev(n_titulo=0, n_corpo=0, n_titulo_revisavel=1)
    assert ev.revisavel and not ev.vincula
    assert classificar_par(ev, _artigo("Petrobras anuncia algo"), NOMES) == ("R", None)


def test_par_sem_vinculo_nem_revisavel_levanta_ValueError():
    ev = _ev(n_titulo=0, n_corpo=1, n_titulo_revisavel=0, n_corpo_revisavel=1)
    assert not ev.vincula and not ev.revisavel
    with pytest.raises(ValueError):
        classificar_par(ev, _artigo("Petrobras anuncia algo"), NOMES)


def test_K1_url_conteudo_de_marca_vira_patrocinado():
    ev = _ev(n_titulo=1)
    url = "https://www.infomoney.com.br/conteudo-de-marca/petrobras-investe/"
    camada, julg = classificar_par(ev, _artigo("Petrobras investe em energia", url=url), NOMES)
    assert camada == "K1"
    assert julg == {
        "papel": "mencao",
        "tipo_conteudo": "patrocinado",
        "evento": "outro",
        "qualidade": 0.0,
        "confianca": 1.0,
        "tickers_adicionais": [],
    }
    assert _valida(julg).papel == "mencao"


@pytest.mark.parametrize("segmento", ["patrocinado", "publieditorial", "branded-content"])
def test_K1_outros_segmentos_de_marca(segmento):
    ev = _ev(n_titulo=1)
    url = f"https://exame.com/{segmento}/algo/"
    camada, _ = classificar_par(ev, _artigo("Petrobras investe", url=url), NOMES)
    assert camada == "K1"


def test_K1_segmento_parcial_nao_casa():
    ev = _ev(n_titulo=1, outras=0)
    url = "https://www.infomoney.com.br/mercados/conteudo-de-marca-x/"
    camada, _ = classificar_par(ev, _artigo("Petrobras tem lucro no trimestre", url=url), NOMES)
    assert camada == "K3"


def test_K2_regra_barata_panorama_usa_o_proprio_julgamento():
    titulo = "Ibovespa fecha em alta com Petrobras e Vale"
    ev = _ev("PETR4", n_titulo=1, outras=0)
    artigo = _artigo(titulo)
    esperado = regra_barata(artigo, "PETR4", NOMES)
    assert esperado is not None
    camada, julg = classificar_par(ev, artigo, NOMES)
    assert camada == "K2"
    assert julg == esperado
    assert julg == {
        "papel": "mencao",
        "tipo_conteudo": "resumo_mercado",
        "evento": "macro",
        "qualidade": 0.1,
        "confianca": 0.9,
        "tickers_adicionais": [],
    }


def test_K2_padroes_lista_sem_regra_barata():
    titulo = "5 acoes para acompanhar nesta semana"
    ev = _ev("PETR4", n_titulo=1, outras=0)
    artigo = _artigo(titulo)
    assert regra_barata(artigo, "PETR4", NOMES) is None
    camada, julg = classificar_par(ev, artigo, NOMES)
    assert camada == "K2"
    assert julg == {
        "papel": "mencao",
        "tipo_conteudo": "lista",
        "evento": "outro",
        "qualidade": 0.1,
        "confianca": 0.9,
        "tickers_adicionais": [],
    }
    assert _valida(julg).tipo_conteudo == "lista"


def test_T_day_trade_com_compre_vai_para_juiza():
    titulo = "Day trade: Compre Brava Energia (BRAV3) com alvo de R$ 20"
    ev = _ev("BRAV3", n_titulo=1, outras=0)
    artigo = _artigo(titulo)
    assert regra_barata(artigo, "BRAV3", NOMES) is None
    assert classificar_par(ev, artigo, NOMES) == ("T", None)


def test_K3_protagonista_resultado_entra_na_entrega():
    titulo = "Petrobras reverte prejuízo no 4º tri"
    ev = _ev("PETR4", n_titulo=1, outras=0)
    camada, julg = classificar_par(ev, _artigo(titulo), NOMES)
    assert camada == "K3"
    assert julg == {
        "papel": "protagonista",
        "tipo_conteudo": "reportagem",
        "evento": "resultado",
        "qualidade": 0.7,
        "confianca": 0.9,
        "tickers_adicionais": [],
    }
    _valida(julg)
    assert entra_por_rotulos(julg["papel"], julg["tipo_conteudo"])


@pytest.mark.parametrize(
    ("titulo", "evento"),
    [
        ("Petrobras tem lucro no trimestre", "resultado"),
        ("Petrobras aprova dividendos extraordinários", "dividendo_jcp"),
        ("Petrobras anuncia aquisição de refinaria", "fusao_aquisicao"),
        ("Petrobras rebaixa nota após queda", "recomendacao"),
        ("Petrobras revisa projeção de produção", "guidance"),
        ("Presidente da Petrobras deixa o cargo", "governanca"),
        ("Petrobras é alvo de investigação", "juridico"),
        ("Petrobras é notificada pela ANP", "regulatorio"),
        ("Petrobras amplia produção no pré-sal", "operacional"),
        ("Petrobras patrocina evento de vela", "outro"),
    ],
)
def test_K3_mapeamento_de_evento_valido_no_enum(titulo, evento):
    ev = _ev("PETR4", n_titulo=1, outras=0)
    camada, julg = classificar_par(ev, _artigo(titulo), NOMES)
    assert camada == "K3", titulo
    assert julg["evento"] == evento
    assert _valida(julg).evento == evento


@pytest.mark.parametrize(
    ("titulo", "evento"),
    [
        ("Petrobras tem lucros no trimestre", "resultado"),
        ("Petrobras reverte prejuízos no trimestre", "resultado"),
        ("Petrobras anuncia aquisições de refinarias", "fusao_aquisicao"),
        ("Petrobras recebe multas da Justiça", "juridico"),
    ],
)
def test_K3_evento_no_plural_casa_o_mesmo_grupo(titulo, evento):
    ev = _ev("PETR4", n_titulo=1, outras=0)
    camada, julg = classificar_par(ev, _artigo(titulo), NOMES)
    assert camada == "K3", titulo
    assert julg["evento"] == evento
    assert _valida(julg).evento == evento


def test_K4_titulo_com_duas_outras_empresas_vai_para_juiza():
    titulo = "Petrobras, Vale e Itau sobem em balanço"
    ev = _ev("PETR4", n_titulo=1, outras=2)
    assert classificar_par(ev, _artigo(titulo), NOMES) == ("K4", None)


def test_K6_corpo_fraco_com_lead_adiantado():
    titulo = "Empresa anuncia novo programa de estágio"
    ev = _ev("PETR4", n_titulo=0, n_corpo=MIN_CORPO, primeiro_bloco=LEAD)
    camada, julg = classificar_par(ev, _artigo(titulo), NOMES)
    assert camada == "K6"
    assert julg == {
        "papel": "mencao",
        "tipo_conteudo": "reportagem",
        "evento": "outro",
        "qualidade": 0.2,
        "confianca": 0.85,
        "tickers_adicionais": [],
    }
    _valida(julg)
    assert not entra_por_rotulos(julg["papel"], julg["tipo_conteudo"])


def test_K7_corpo_fraco_com_lead_no_comeco_vai_para_juiza():
    titulo = "Empresa anuncia novo programa de estágio"
    ev = _ev("PETR4", n_titulo=0, n_corpo=MIN_CORPO, primeiro_bloco=LEAD - 1)
    assert classificar_par(ev, _artigo(titulo), NOMES) == ("K7", None)


def test_K7_corpo_forte_vai_para_juiza():
    titulo = "Empresa anuncia novo programa de estágio"
    ev = _ev("PETR4", n_titulo=0, n_corpo=MIN_CORPO + 1, primeiro_bloco=LEAD)
    assert classificar_par(ev, _artigo(titulo), NOMES) == ("K7", None)


def test_camadas_ativas_e_camadas_sem_julgamento():
    assert {"K1", "K2", "K3", "K6"} == CAMADAS_ATIVAS
    assert REGRAS_VERSAO == 1
    # Camadas fora do conjunto devolvem None, mas a camada continua na tupla.
    sem_julgamento = [
        classificar_par(_ev(n_titulo=1, outras=2), _artigo("Petrobras e Vale em alta"), NOMES),
        classificar_par(_ev(n_titulo=1), _artigo("Day trade: Compre PETR4 hoje"), NOMES),
        classificar_par(_ev(n_corpo=MIN_CORPO, primeiro_bloco=0), _artigo("Empresa anuncia"), NOMES),
        classificar_par(_ev(n_titulo_revisavel=1), _artigo("Empresa anuncia"), NOMES),
    ]
    assert sem_julgamento == [("K4", None), ("T", None), ("K7", None), ("R", None)]


def test_rotulo_modelo_cabe_na_coluna_modelo():
    limite = StagingNoticiaJulgamento.__table__.c.modelo.type.length
    assert limite == 64
    assert rotulo_modelo("K1") == "codigo/r1/K1"
    for camada in ("K1", "K2", "K3", "K6", "R", "T", "K4", "K7"):
        assert len(rotulo_modelo(camada)) <= limite
