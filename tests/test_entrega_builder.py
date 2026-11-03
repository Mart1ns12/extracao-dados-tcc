"""Montagem da entrega sem banco e sem rede: montar_* com linhas sinteticas e fixtures reais.

Fixtures usadas: b3_supplement_PETR.json (desdobramento 2008, bonificacao 1994, grupamento
2000 e JCPs de 2026), b3_getdetail_9512.json (cadastro da Petrobras) e b3_getdetail_12653.json
(Klabin). Plano de contas de banco sintetico, com a forma que a fixture CVM do Itau tem.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tcc_scraper.config import MUDANCAS_TICKER, NOMES_EMPRESA
from tcc_scraper.db.models_entrega import COLUNAS
from tcc_scraper.entrega import builder as B
from tcc_scraper.entrega.filtros import Julgamento, aprovado_para_entrega

FIXTURES = Path(__file__).parent / "fixtures"
SP = ZoneInfo("America/Sao_Paulo")
UTC = dt.UTC
PT = "lucas-leme/FinBERT-PT-BR"
MODELOS = {"pt": PT, "en": "ProsusAI/finbert"}
PETROBRAS = "33000167000101"


def _carregar(nome: str):
    return json.loads((FIXTURES / nome).read_text(encoding="utf-8"))


def _suplemento_petr() -> list[dict]:
    payload = _carregar("b3_supplement_PETR.json")[0]
    linhas = [
        {"issuing_company": "PETR", "tipo": "stock", "payload": item}
        for item in payload["stockDividends"]
    ]
    linhas += [
        {"issuing_company": "PETR", "tipo": "cash", "payload": item}
        for item in payload["cashDividends"]
    ]
    return linhas


def _cadastro_petr() -> list[dict]:
    d = _carregar("b3_getdetail_9512.json")
    return [{
        "cd_cvm": 9512,
        "cnpj": d["cnpj"],
        "trading_name": d["tradingName"],
        "issuing_company": "PETR",
        "industry_classification": d["industryClassification"],
        "other_codes": [o["code"] for o in d["otherCodes"]],
        "payload": d,
    }]


def _prov(ic="PETR", type_stock="ON", acao="DIVIDENDO", valor=0.45,
          data_com=dt.date(2008, 1, 10), cotacao="1"):
    return {
        "issuing_company": ic,
        "type_stock": type_stock,
        "corporate_action": acao,
        "value_cash": valor,
        "last_date_prior_ex": data_com,
        "quoted_per_shares": cotacao,
    }


def _artigo(aid=1234567, origem="google_news", precisao="datetime", lang="pt",
            publicado=dt.datetime(2026, 6, 18, 22, 42, tzinfo=UTC)):
    return {
        "id": aid,
        "title": "Petrobras anuncia campo",
        "source_name": "InfoMoney",
        "source_domain": "infomoney.com.br",
        "resolved_url": "https://www.infomoney.com.br/x",
        "url": "https://news.google.com/x",
        "published_at": publicado,
        "published_precision": precisao,
        "lang": lang,
        "collected_at": dt.datetime(2026, 6, 18, 23, 5, tzinfo=UTC),
        "origem": origem,
    }


def _jul(article_id=1234567, ticker="PETR4", versao=1, papel="protagonista",
           tipo="reportagem", adicionais=None):
    return {
        "article_id": article_id,
        "ticker": ticker,
        "prompt_versao": versao,
        "papel": papel,
        "tipo_conteudo": tipo,
        "evento": "resultado",
        "tickers_adicionais": adicionais or [],
        "qualidade": 0.8,
    }


PREGOES = {dt.date(2026, 6, 18)}


# -- ids, horarios e momento do pregao -------------------------------------------------------

def test_id_noticia_com_zeros_e_sem_colisao_acima_de_seis_digitos() -> None:
    assert B.id_noticia(1234567) == "n1234567"
    assert B.id_noticia(123) == "n000123"


@pytest.mark.parametrize(
    ("instante", "esperado"),
    [
        (None, "desconhecido"),
        (dt.datetime(2026, 6, 20, 12, 0, tzinfo=SP), "sem_pregao"),
        (dt.datetime(2026, 6, 18, 9, 59, tzinfo=SP), "antes_abertura"),
        (dt.datetime(2026, 6, 18, 10, 0, tzinfo=SP), "durante"),
        (dt.datetime(2026, 6, 18, 17, 59, tzinfo=SP), "durante"),
        (dt.datetime(2026, 6, 18, 18, 0, tzinfo=SP), "apos_fechamento"),
    ],
)
def test_momento_pregao_cobre_os_casos(instante, esperado) -> None:
    assert B.momento_pregao(instante, PREGOES) == esperado


# -- noticias --------------------------------------------------------------------------------

def test_noticia_com_hora_grava_iso_sp_e_data_em_sp() -> None:
    linhas = B.montar_noticias(
        [_artigo()], [{"article_id": 1234567, "ticker": "PETR4"}], [], [_jul()],
        PREGOES, True, MODELOS,
    )
    (linha,) = linhas
    assert list(linha) == COLUNAS["noticias"]
    assert linha["id"] == "n1234567"
    assert linha["data_hora_publicacao"] == "2026-06-18T19:42:00-03:00"
    assert linha["data_publicacao"] == dt.date(2026, 6, 18)
    assert linha["momento_pregao"] == "apos_fechamento"
    assert dt.datetime.fromisoformat(linha["data_hora_publicacao"]) <= dt.datetime.fromisoformat(
        linha["data_hora_coleta"]
    )


def test_precisao_date_zera_hora_e_marca_momento_desconhecido() -> None:
    # IPE: a data de entrega vira o inicio do dia em Brasilia (published_precision='date').
    artigo = _artigo(origem="cvm_ipe", precisao="date",
                     publicado=dt.datetime(2026, 6, 18, 3, 0, tzinfo=UTC))
    (linha,) = B.montar_noticias([artigo], [{"article_id": 1234567, "ticker": "PETR4"}], [],
                                 [], PREGOES, True, MODELOS)
    assert linha["data_hora_publicacao"] is None
    assert linha["momento_pregao"] == "desconhecido"
    assert linha["data_publicacao"] == dt.date(2026, 6, 18)
    assert linha["fonte"] == "CVM"


def test_ipe_entra_sem_julgamento_e_com_a_juiza_exigida() -> None:
    artigo = _artigo(origem="cvm_ipe", precisao="date",
                     publicado=dt.datetime(2026, 6, 18, 3, 0, tzinfo=UTC))
    linhas = B.montar_noticias([artigo], [{"article_id": 1234567, "ticker": "PETR4"}], [], [],
                               PREGOES, True, MODELOS)
    assert len(linhas) == 1
    assert linhas[0]["papel"] is None


def test_ticker_adicional_herda_julgamento_e_versao_mais_alta_vence() -> None:
    julgamentos = [
        _jul(versao=1, papel="mencao", tipo="reportagem"),
        _jul(versao=2, papel="protagonista", tipo="reportagem", adicionais=["VIVT3"]),
    ]
    linhas = B.montar_noticias(
        [_artigo()], [{"article_id": 1234567, "ticker": "PETR4"}], [], julgamentos,
        PREGOES, True, MODELOS,
    )
    tickers = sorted(linha["ticker"] for linha in linhas)
    assert tickers == ["PETR4", "VIVT3"]
    assert {linha["papel"] for linha in linhas} == {"protagonista"}


def test_juiza_exigida_barra_panorama() -> None:
    julgamento = [_jul(tipo="resumo_mercado", papel="mencao")]
    vinculo = [{"article_id": 1234567, "ticker": "PETR4"}]
    com_juiza = B.montar_noticias([_artigo()], vinculo, [], julgamento, PREGOES, True, MODELOS)
    sem_juiza = B.montar_noticias([_artigo()], vinculo, [], julgamento, PREGOES, False, MODELOS)
    assert com_juiza == []
    assert len(sem_juiza) == 1


def test_prob_vem_do_modelo_do_idioma_do_artigo() -> None:
    sentimentos = [
        {"article_id": 1234567, "model": PT, "prob_pos": 0.81, "prob_neu": 0.15, "prob_neg": 0.04},
        {"article_id": 1234567, "model": "ProsusAI/finbert",
         "prob_pos": 0.1, "prob_neu": 0.2, "prob_neg": 0.7},
    ]
    (linha,) = B.montar_noticias([_artigo(lang="pt")], [{"article_id": 1234567, "ticker": "PETR4"}],
                                 sentimentos, [_jul()], PREGOES, True, MODELOS)
    probs = (linha["prob_positivo"], linha["prob_neutro"], linha["prob_negativo"])
    assert probs == (0.81, 0.15, 0.04)


@pytest.mark.parametrize(
    ("julgamento", "origem", "exigir", "esperado"),
    [
        (None, "cvm_ipe", True, True),
        (None, "google_news", True, False),
        (None, "google_news", False, True),
        (Julgamento("protagonista", "reportagem", "outro", 0.9, ()), "google_news", True, True),
        (Julgamento("protagonista", "reportagem", "outro", 0.9, ()), "wordpress", True, True),
        (Julgamento("citada_com_fato", "reportagem", "outro", 0.9, ()), "rss_nativo", True, True),
        (Julgamento("mencao", "reportagem", "outro", 0.9, ()), "google_news", True, False),
        (Julgamento("protagonista", "opiniao", "outro", 0.9, ()), "google_news", True, False),
        (Julgamento("errada", "reportagem", "outro", 0.9, ()), "google_news", True, False),
    ],
)
def test_aprovado_para_entrega_combinacoes(julgamento, origem, exigir, esperado) -> None:
    assert aprovado_para_entrega(julgamento, origem, exigir) is esperado


# -- artigos: republicacao entre veiculos -----------------------------------------------------

def test_artigos_exclui_republicacao_que_nao_e_representante(monkeypatch) -> None:
    """A consulta de artigos tira os article_id de staging_historias com article_id != historia.

    Sem banco: a consulta e capturada no lugar de executada, e so o SQL e conferido.
    """
    capturadas = []
    monkeypatch.setattr(B, "_consulta", lambda stmt: capturadas.append(stmt) or [])

    _ = B._Fontes(dt.date(2026, 1, 1)).artigos

    sql = str(capturadas[0].compile())
    assert "staging_historias" in sql
    assert "NOT IN" in sql.upper()
    assert "!=" in sql or "<>" in sql


# -- IPE: documento de emissor que nao valia na data de entrega -------------------------------

LINK_IPE = "https://www.rad.cvm.gov.br/ENET/documento.pdf"


def _montar_ipe(ipe_por_link: dict | None, ticker: str = "NATU3") -> list[dict]:
    artigo = {**_artigo(origem="cvm_ipe", precisao="date",
                        publicado=dt.datetime(2026, 6, 18, 3, 0, tzinfo=UTC)),
              "resolved_url": LINK_IPE, "url": LINK_IPE}
    return B.montar_noticias([artigo], [{"article_id": 1234567, "ticker": ticker}], [], [],
                             PREGOES, False, MODELOS, ipe_por_link=ipe_por_link)


def test_documento_ipe_da_natura_cosmeticos_em_2021_nao_gera_natu3() -> None:
    # Natura Cosmeticos (19550) nao era o NATU3 entre 2019-12-18 e 2025-07-01.
    assert _montar_ipe({LINK_IPE: (19550, dt.date(2021, 3, 15))}) == []


def test_documento_ipe_da_holding_em_2021_gera_natu3() -> None:
    (linha,) = _montar_ipe({LINK_IPE: (24783, dt.date(2021, 3, 15))})
    assert linha["ticker"] == "NATU3"
    assert linha["fonte"] == "CVM"


def test_sem_ipe_por_link_ou_sem_o_documento_nada_muda() -> None:
    assert len(_montar_ipe(None)) == 1
    assert len(_montar_ipe({})) == 1
    assert len(_montar_ipe({"https://outro.link/x": (19550, dt.date(2021, 3, 15))})) == 1


def test_ids_a_apagar_inclui_ipe_sem_par_aprovado_com_juiza_desligada() -> None:
    poda = B._Poda(janela={1, 2}, julgados=set(), exigir_juiza=False, ipe={1})
    assert B._ids_a_apagar(poda, set()) == {B.id_noticia(1)}
    assert B._ids_a_apagar(poda, {B.id_noticia(1)}) == set()


def test_ids_a_apagar_sem_juiza_nao_apaga_julgado_nem_sem_julgamento_fora_do_ipe() -> None:
    poda = B._Poda(janela={2, 3}, julgados={2}, exigir_juiza=False, ipe=set())
    assert B._ids_a_apagar(poda, set()) == set()


def test_ids_a_apagar_com_juiza_exigida_apaga_julgado_sem_aprovada_e_nao_o_sem_julgamento() -> None:
    poda = B._Poda(janela={2, 3}, julgados={2}, exigir_juiza=True, ipe=set())
    assert B._ids_a_apagar(poda, set()) == {B.id_noticia(2)}


# -- precos e indices ------------------------------------------------------------------------

def _preco(ticker, close=10.0, adj=9.0, volume=1000, data=dt.date(2026, 6, 18), abertura=1.0):
    return {"ticker": ticker, "date": data, "open": abertura, "high": 2.0, "low": 0.5,
            "close": close, "adj_close": adj, "volume": volume}


def test_precos_renomeia_macro_e_zera_volume_e_ajuste_do_ibov() -> None:
    linhas = B.montar_precos([
        _preco("PETR4.SA", close=35.0, adj=33.0),
        _preco("^BVSP", close=130000.0, adj=130000.0, volume=9999),
        _preco("USDBRL=X", close=5.4, adj=5.4, volume=0),
        _preco("BZ=F", close=80.0, adj=80.0, volume=0),
        _preco("IVVB11", close=300.0, adj=290.0, volume=0),
        _preco("IDIV", close=5000.0, adj=5000.0),
        _preco("SEMCLOSE", close=None),
    ])
    por_ticker = {(linha["ticker"], linha["data"]): linha for linha in linhas}
    assert list(linhas[0]) == COLUNAS["precos"]
    assert set(t for t, _ in por_ticker) == {"PETR4", "IBOV", "USDBRL", "BRENT", "IVVB11"}
    ibov = por_ticker[("IBOV", dt.date(2026, 6, 18))]
    assert ibov["fechamento_ajustado"] == ibov["fechamento"] == 130000.0
    assert ibov["volume"] is None
    assert por_ticker[("USDBRL", dt.date(2026, 6, 18))]["volume"] is None
    assert por_ticker[("IVVB11", dt.date(2026, 6, 18))]["volume"] is None  # 0 vira em branco
    petr = por_ticker[("PETR4", dt.date(2026, 6, 18))]
    assert (petr["fechamento"], petr["fechamento_ajustado"], petr["volume"]) == (35.0, 33.0, 1000)


def test_abertura_do_ibov_repetindo_o_fechamento_anterior_vira_vazia() -> None:
    d1, d2 = dt.date(2026, 6, 17), dt.date(2026, 6, 18)
    linhas = B.montar_precos([
        _preco("^BVSP", close=130000.0, abertura=129000.0, data=d1),
        _preco("^BVSP", close=130500.0, abertura=130000.0, data=d2),  # == fechamento de d1
    ])
    por_data = {linha["data"]: linha for linha in linhas}
    assert por_data[d2]["abertura"] is None
    assert por_data[d2]["fechamento"] == 130500.0  # so a abertura some, o resto fica


def test_abertura_do_ibov_normal_mantem_e_primeiro_dia_mantem() -> None:
    d1, d2, d3 = dt.date(2026, 6, 16), dt.date(2026, 6, 17), dt.date(2026, 6, 18)
    linhas = B.montar_precos([  # fora de ordem de propósito: a regra ordena por data antes
        _preco("^BVSP", close=131000.0, abertura=130800.0, data=d3),  # != fechamento de d2
        _preco("^BVSP", close=130000.0, abertura=129500.0, data=d1),
        _preco("^BVSP", close=130500.0, abertura=130100.0, data=d2),
    ])
    por_data = {linha["data"]: linha["abertura"] for linha in linhas}
    assert por_data == {d1: 129500.0, d2: 130100.0, d3: 130800.0}
    # Primeiro dia da serie sem pregao anterior: a abertura fica mesmo que bata com o
    # proprio fechamento.
    primeiro = B.montar_precos([_preco("^BVSP", close=130000.0, abertura=130000.0, data=d1)])
    assert primeiro[0]["abertura"] == 130000.0


def test_abertura_repetida_so_e_anulada_no_ibov() -> None:
    d1, d2 = dt.date(2026, 6, 17), dt.date(2026, 6, 18)
    linhas = B.montar_precos([
        _preco("USDBRL=X", close=5.4, abertura=5.3, data=d1, volume=0),
        _preco("USDBRL=X", close=5.5, abertura=5.4, data=d2, volume=0),  # repete, mas nao e IBOV
        _preco("PETR4.SA", close=35.0, abertura=34.0, data=d1),
        _preco("PETR4.SA", close=35.5, abertura=35.0, data=d2),  # repete, mas nao e IBOV
    ])
    aberturas = {(linha["ticker"], linha["data"]): linha["abertura"] for linha in linhas}
    assert aberturas[("USDBRL", d2)] == 5.4
    assert aberturas[("PETR4", d2)] == 35.0


def test_indices_roteia_cdi_ipca_e_fechamento_das_series_b3() -> None:
    macro = [
        {"series": "cdi", "date": dt.date(2026, 6, 18), "value": 0.0551},
        {"series": "ipca", "date": dt.date(2026, 6, 1), "value": 0.26},
        {"series": "selic", "date": dt.date(2026, 6, 18), "value": 14.5},
    ]
    linhas = B.montar_indices([_preco("IDIV", close=5000.0), _preco("PETR4")], macro)
    assert list(linhas[0]) == COLUNAS["indices"]
    assert {(r["indice"], r["valor"]) for r in linhas} == {
        ("CDI", 0.0551), ("IPCA", 0.26), ("IDIV", 5000.0),
    }
    assert all(r["indice"] != "PETR4" for r in linhas)


# -- cadastro e mudancas ---------------------------------------------------------------------

def test_cadastro_classifica_tipo_e_unit_e_usa_cvm_sem_cnpj_da_b3() -> None:
    b3 = _cadastro_petr()
    b3[0]["cnpj"] = None  # forca o fallback para o cadastro da CVM
    cvm = [{"cd_cvm": 9512, "cnpj": PETROBRAS, "denom_social": "PETROBRAS"}]
    linhas = {
        r["ticker"]: r
        for r in B.montar_cadastro(b3, cvm, ["PETR4", "KLBN11", "JBSS32", "CRFB3"])
    }
    petr = linhas["PETR4"]
    assert list(petr) == COLUNAS["cadastro"]
    assert petr["cnpj"] == PETROBRAS
    assert petr["empresa"] == NOMES_EMPRESA["PETR4"]
    assert petr["setor"] == "Petróleo, Gás e Biocombustíveis"
    assert petr["tipo_acao"] == "PN"
    assert petr["unit_qtd_on"] is None and petr["unit_qtd_pn"] is None
    assert linhas["KLBN11"]["tipo_acao"] == "UNIT"
    assert (linhas["KLBN11"]["unit_qtd_on"], linhas["KLBN11"]["unit_qtd_pn"]) == (1, 4)
    assert linhas["JBSS32"]["tipo_acao"] == "BDR"
    assert linhas["CRFB3"]["tipo_acao"] == "ON"


def test_mudancas_ticker_vem_do_config() -> None:
    linhas = B.montar_mudancas()
    assert list(linhas[0]) == COLUNAS["mudancas_ticker"]
    assert len(linhas) == len(MUDANCAS_TICKER)


# -- emissores por periodo (emissores_ticker) ------------------------------------------------

def _cvm_emissores() -> list[dict]:
    return [
        {"cd_cvm": 20575, "cnpj": "02916265000160", "denom_social": "JBS S.A."},
        {"cd_cvm": 80233, "cnpj": "49115815000105", "denom_social": "JBS N.V."},
        {"cd_cvm": 19550, "cnpj": "71673990000177", "denom_social": "NATURA COSMETICOS S.A."},
        {"cd_cvm": 24783, "cnpj": "32785497000197", "denom_social": "NATURA &CO HOLDING S.A."},
        {"cd_cvm": 9512, "cnpj": PETROBRAS, "denom_social": "PETROBRAS"},
    ]


def test_emissores_jbss32_tem_dois_periodos_com_cnpj_e_fator_do_periodo() -> None:
    linhas = B.montar_emissores([], _cvm_emissores(), ["JBSS32"])
    assert all(list(r) == COLUNAS["emissores_ticker"] for r in linhas)
    assert [(r["ordem"], r["ticker_negociado"], r["cd_cvm"]) for r in linhas] == [
        (1, "JBSS3", 20575), (2, "JBSS32", 80233),
    ]
    antes, depois = linhas
    assert (antes["vigencia_inicio"], antes["vigencia_fim"]) == (None, dt.date(2025, 6, 8))
    assert (depois["vigencia_inicio"], depois["vigencia_fim"]) == (dt.date(2025, 6, 9), None)
    assert (antes["cnpj"], antes["fator_acoes"]) == ("02916265000160", 0.5)
    assert (depois["cnpj"], depois["fator_acoes"]) == ("49115815000105", 1.0)
    assert (antes["empresa"], depois["empresa"]) == ("JBS S.A.", "JBS N.V.")


def test_emissores_natu3_tem_tres_periodos_e_volta_a_natura() -> None:
    linhas = B.montar_emissores([], _cvm_emissores(), ["NATU3"])
    assert [(r["ordem"], r["ticker_negociado"], r["cd_cvm"]) for r in linhas] == [
        (1, "NATU3", 19550), (2, "NTCO3", 24783), (3, "NATU3", 19550),
    ]
    assert [r["cnpj"] for r in linhas] == [
        "71673990000177", "32785497000197", "71673990000177",
    ]
    assert linhas[1]["vigencia_inicio"] == dt.date(2019, 12, 18)
    assert linhas[1]["vigencia_fim"] == dt.date(2025, 7, 1)
    assert linhas[2]["vigencia_inicio"] == dt.date(2025, 7, 2)
    assert linhas[2]["vigencia_fim"] is None


def test_emissores_ticker_sem_periodo_usa_b3_ou_cvm_e_uma_linha_aberta() -> None:
    b3 = [{"cd_cvm": 9512, "cnpj": PETROBRAS, "trading_name": "PETROBRAS B3"}]
    (linha,) = B.montar_emissores(b3, [], ["PETR4"])
    assert list(linha) == COLUNAS["emissores_ticker"]
    assert (linha["ticker"], linha["ordem"], linha["ticker_negociado"]) == ("PETR4", 1, "PETR4")
    assert (linha["cd_cvm"], linha["cnpj"], linha["fator_acoes"]) == (9512, PETROBRAS, 1.0)
    assert (linha["vigencia_inicio"], linha["vigencia_fim"]) == (None, None)
    assert linha["empresa"] == "PETROBRAS B3"
    (sem_nada,) = B.montar_emissores([], [], ["PETR4"])
    assert sem_nada["cnpj"] is None
    assert sem_nada["empresa"] == NOMES_EMPRESA["PETR4"]


def test_emissores_ignora_ticker_sem_codigo_cvm() -> None:
    assert B.montar_emissores([], _cvm_emissores(), ["ZZZZ3"]) == []


def test_tabelas_completas_sao_tabelas_da_entrega() -> None:
    esperadas = {"balancos", "acoes_emitidas", "proventos", "emissores_ticker"}
    assert esperadas == B.TABELAS_COMPLETAS
    assert set(COLUNAS) >= B.TABELAS_COMPLETAS


def test_chaves_obsoletas_sobra_o_que_a_montagem_nao_produz_mais() -> None:
    existentes = [("JBSS32", 1), ("JBSS32", 2), ("PETR4", 1)]
    linhas = [{"ticker": "JBSS32", "ordem": 2}, {"ticker": "PETR4", "ordem": 1}]
    assert B._chaves_obsoletas(existentes, linhas, ["ticker", "ordem"]) == [("JBSS32", 1)]


def test_chaves_obsoletas_vazio_quando_nada_sobra() -> None:
    existentes = [("JBSS32", 1), ("PETR4", 1)]
    linhas = [
        {"ticker": "PETR4", "ordem": 1}, {"ticker": "JBSS32", "ordem": 1},
        {"ticker": "NOVO3", "ordem": 1},
    ]
    assert B._chaves_obsoletas(existentes, linhas, ["ticker", "ordem"]) == []


def test_chaves_obsoletas_compara_datas_como_vem_do_banco() -> None:
    cnpj = "71673990000177"
    existentes = [(cnpj, dt.date(2024, 12, 31)), (cnpj, dt.date(2025, 3, 31))]
    linhas = [{"cnpj": cnpj, "data_referencia": dt.date(2025, 3, 31), "qtd_on": 10}]
    assert B._chaves_obsoletas(existentes, linhas, ["cnpj", "data_referencia"]) == [
        (cnpj, dt.date(2024, 12, 31)),
    ]


# -- balancos e acoes emitidas ---------------------------------------------------------------

def _bal(cd, ds, valor, *, demo="DRE", ini=dt.date(2025, 4, 1), fim=dt.date(2025, 6, 30),
         refer=dt.date(2025, 6, 30), versao=1, escala="MIL", tipo="ITR", cd_cvm=9512,
         ordem="ÚLTIMO", receb=dt.date(2025, 8, 8)):
    return {
        "cnpj": PETROBRAS, "tipo_documento": tipo, "dt_refer": refer, "versao": versao,
        "demonstracao": demo, "cd_conta": cd, "dt_ini_exerc": ini, "dt_fim_exerc": fim,
        "ds_conta": ds, "ordem_exerc": ordem, "escala_moeda": escala, "vl_conta": valor,
        "dt_receb": receb, "cd_cvm": cd_cvm,
    }


def test_balanco_mil_vira_reais_exato_e_pl_copiado_para_o_periodo() -> None:
    balancos = [
        _bal("3.01", "Receita de Venda de Bens e/ou Serviços", 117721000.0),
        _bal("3.03", "Resultado Bruto", 40000000.0),
        _bal("3.11", "Lucro/Prejuízo Consolidado do Período", 12000000.0),
        _bal("2.03", "Patrimônio Líquido Consolidado", 412000000.0, demo="BPP",
             ini=dt.date(2025, 6, 30), fim=dt.date(2025, 6, 30)),
    ]
    (linha,) = B.montar_balancos(balancos)
    assert list(linha) == COLUNAS["balancos"]
    assert linha["receita_liquida"] == 117721000000.0
    assert linha["lucro_bruto"] == 40000000000.0
    assert linha["lucro_liquido"] == 12000000000.0
    assert linha["patrimonio_liquido"] == 412000000000.0
    assert linha["data_entrega"] == dt.date(2025, 8, 8)


def test_lucro_liquido_pela_descricao_quando_ha_3_09_e_3_11() -> None:
    # Petrobras: 3.09 e resultado das operacoes continuadas; o lucro esta em 3.11.
    balancos = [
        _bal("3.09", "Resultado Líquido das Operações Continuadas", 1.0, escala="UNIDADE"),
        _bal("3.11", "Lucro/Prejuízo Consolidado do Período", 2.0, escala="UNIDADE"),
    ]
    (linha,) = B.montar_balancos(balancos)
    assert linha["lucro_liquido"] == 2.0


def test_banco_usa_plano_proprio_sem_lucro_bruto() -> None:
    # Plano de banco: 3.03 e o resultado bruto da intermediacao (fica em branco), lucro em
    # 3.09 e PL em 2.08 (2.03 sao passivos financeiros).
    balancos = [
        _bal("3.01", "Receitas da Intermediação Financeira", 900.0, escala="UNIDADE"),
        _bal("3.03", "Resultado Bruto Intermediação Financeira", 500.0, escala="UNIDADE"),
        _bal("3.09", "Lucro/Prejuízo Consolidado do Período", 150.0, escala="UNIDADE"),
        _bal("2.03", "Passivos Financeiros ao Custo Amortizado", 7000.0, demo="BPP",
             ini=dt.date(2025, 6, 30), fim=dt.date(2025, 6, 30), escala="UNIDADE"),
        _bal("2.08", "Patrimônio Líquido Consolidado", 1000.0, demo="BPP",
             ini=dt.date(2025, 6, 30), fim=dt.date(2025, 6, 30), escala="UNIDADE"),
    ]
    (linha,) = B.montar_balancos(balancos)
    assert linha["lucro_bruto"] is None
    assert linha["lucro_liquido"] == 150.0
    assert linha["patrimonio_liquido"] == 1000.0
    assert linha["receita_liquida"] == 900.0


def test_itr_trimestre_e_acumulado_viram_linhas_separadas_e_versao_maior_vence() -> None:
    balancos = [
        _bal("3.01", "Receita", 100.0, escala="UNIDADE", versao=1),
        _bal("3.01", "Receita", 111.0, escala="UNIDADE", versao=2),
        _bal("3.01", "Receita acumulada", 250.0, escala="UNIDADE", versao=2,
             ini=dt.date(2025, 1, 1)),
    ]
    linhas = B.montar_balancos(balancos)
    assert len(linhas) == 2
    assert {linha["receita_liquida"] for linha in linhas} == {111.0, 250.0}


def test_despesa_de_seguros_dentro_da_3_01_sai_da_receita_hapv() -> None:
    # HAPV3 (IFRS 17): a 3.01 do 1T25 ja vem com a despesa de seguros (3.01.02) dentro.
    ini, fim = dt.date(2025, 1, 1), dt.date(2025, 3, 31)
    balancos = [
        _bal("3.01", "Receita de Seguros", 1469235.0, ini=ini, fim=fim, refer=fim),
        _bal("3.01.01", "Receita de seguros", 7626925.0, ini=ini, fim=fim, refer=fim),
        _bal("3.01.02", "Despesa de seguros", -6335219.0, ini=ini, fim=fim, refer=fim),
        _bal("3.01.03", "Outras receitas", 177529.0, ini=ini, fim=fim, refer=fim),
    ]
    (linha,) = B.montar_balancos(balancos)
    assert linha["receita_liquida"] == 7804454000.0
    assert linha["situacao"] == "ajuste_ifrs17"


def test_subconta_negativa_que_nao_e_despesa_de_seguro_nao_muda_a_receita() -> None:
    # RADL: 3.01.02 e imposto sobre vendas (negativo), nao despesa de seguro.
    balancos = [
        _bal("3.01", "Receita de Venda de Bens e/ou Serviços", 100.0, escala="UNIDADE"),
        _bal("3.01.02", "Impostos Incidentes sobre Vendas", -10.0, escala="UNIDADE"),
    ]
    (linha,) = B.montar_balancos(balancos)
    assert linha["receita_liquida"] == 100.0
    assert linha["situacao"] == "ok"


def test_reapresentacao_troca_receita_zerada_pelo_comparativo_posterior_jbs() -> None:
    ini1, fim1 = dt.date(2025, 1, 1), dt.date(2025, 3, 31)
    ini2, fim2 = dt.date(2026, 1, 1), dt.date(2026, 3, 31)
    recebido_original = dt.date(2025, 5, 27)
    recebido_reapresentacao = dt.date(2026, 5, 12)
    balancos = [
        # ITR 1T25 original: receita zerada, com PL, BP e DFC proprios.
        _bal("3.01", "Receita de Venda de Bens e/ou Serviços", 0.0, ini=ini1, fim=fim1,
             refer=fim1, receb=recebido_original),
        _bal("3.11", "Lucro/Prejuízo Consolidado do Período", 724742.0, ini=ini1, fim=fim1,
             refer=fim1, receb=recebido_original),
        _bal("2.03", "Patrimônio Líquido Consolidado", 11837070.0, demo="BPP", ini=fim1,
             fim=fim1, refer=fim1, receb=recebido_original),
        _bal("6.01.01.04", "Depreciação e Amortização", 999.0, demo="DFC", ini=ini1, fim=fim1,
             refer=fim1, receb=recebido_original),
        # ITR 1T26 traz o 1T25 reapresentado como PENULTIMO, e o 1T26 proprio.
        _bal("3.01", "Receita de Venda de Bens e/ou Serviços", 114127485.0, ini=ini1, fim=fim1,
             refer=fim2, ordem="PENÚLTIMO", receb=recebido_reapresentacao),
        _bal("3.11", "Lucro/Prejuízo Consolidado do Período", 3251604.0, ini=ini1, fim=fim1,
             refer=fim2, ordem="PENÚLTIMO", receb=recebido_reapresentacao),
        _bal("6.01.01.04", "Depreciação e Amortização", 1000.0, demo="DFC", ini=ini1,
             fim=fim1, refer=fim2, ordem="PENÚLTIMO", receb=recebido_reapresentacao),
        _bal("3.01", "Receita de Venda de Bens e/ou Serviços", 130000000.0, ini=ini2, fim=fim2,
             refer=fim2, receb=recebido_reapresentacao),
        # ITR 2T25: DRE 3M e DFC 6M. A D&A 3M depende do 1T25 invalido, entao sai None.
        _bal("3.01", "Receita de Venda de Bens e/ou Serviços", 120000000.0,
             ini=dt.date(2025, 4, 1), fim=dt.date(2025, 6, 30), refer=dt.date(2025, 6, 30),
             receb=dt.date(2025, 8, 13)),
        _bal("6.01.01.04", "Depreciação e Amortização", 2000.0, demo="DFC",
             ini=ini1, fim=dt.date(2025, 6, 30), refer=dt.date(2025, 6, 30),
             receb=dt.date(2025, 8, 13)),
    ]
    por_periodo = _por_periodo(B.montar_balancos(balancos))

    t1 = por_periodo[(ini1, fim1)]
    assert t1["receita_liquida"] == 114127485000.0
    assert t1["lucro_liquido"] == 3251604000.0
    assert t1["patrimonio_liquido"] is None
    assert t1["data_entrega"] == recebido_reapresentacao
    assert t1["depreciacao_amortizacao"] == 1000000.0
    assert t1["situacao"] == "reapresentado"

    assert por_periodo[(ini2, fim2)]["receita_liquida"] == 130000000000.0
    assert por_periodo[(ini2, fim2)]["situacao"] == "ok"
    t2 = por_periodo[(dt.date(2025, 4, 1), dt.date(2025, 6, 30))]
    assert t2["receita_liquida"] == 120000000000.0
    assert t2["depreciacao_amortizacao"] is None
    assert t2["situacao"] == "ok"


def test_receita_zero_sem_comparativo_com_receita_fica_zero_com_data_original() -> None:
    ini, fim = dt.date(2025, 1, 1), dt.date(2025, 3, 31)
    balancos = [
        _bal("3.01", "Receita", 0.0, escala="UNIDADE", ini=ini, fim=fim, refer=fim,
             receb=dt.date(2025, 5, 27)),
        _bal("3.01", "Receita", 0.0, escala="UNIDADE", ini=ini, fim=fim,
             refer=dt.date(2026, 3, 31), ordem="PENÚLTIMO", receb=dt.date(2026, 5, 12)),
    ]
    (linha,) = B.montar_balancos(balancos)
    assert linha["receita_liquida"] == 0.0
    assert linha["data_entrega"] == dt.date(2025, 5, 27)


def test_receita_do_ultimo_nao_zero_vence_comparativo_diferente() -> None:
    ini, fim = dt.date(2025, 1, 1), dt.date(2025, 3, 31)
    balancos = [
        _bal("3.01", "Receita", 100.0, escala="UNIDADE", ini=ini, fim=fim, refer=fim,
             receb=dt.date(2025, 5, 27)),
        _bal("3.01", "Receita", 150.0, escala="UNIDADE", ini=ini, fim=fim,
             refer=dt.date(2026, 3, 31), ordem="PENÚLTIMO", receb=dt.date(2026, 5, 12)),
    ]
    (linha,) = B.montar_balancos(balancos)
    assert linha["receita_liquida"] == 100.0
    assert linha["data_entrega"] == dt.date(2025, 5, 27)


def test_comparativo_com_recebimento_nao_posterior_ao_original_e_ignorado() -> None:
    ini, fim = dt.date(2025, 1, 1), dt.date(2025, 3, 31)
    recebido = dt.date(2025, 5, 27)
    balancos = [
        _bal("3.01", "Receita", 0.0, escala="UNIDADE", ini=ini, fim=fim, refer=fim,
             receb=recebido),
        _bal("3.01", "Receita", 150.0, escala="UNIDADE", ini=ini, fim=fim,
             refer=dt.date(2025, 6, 30), ordem="PENÚLTIMO", receb=recebido),
    ]
    (linha,) = B.montar_balancos(balancos)
    assert linha["receita_liquida"] == 0.0
    assert linha["data_entrega"] == recebido


def test_acoes_dfp_vence_itr_na_mesma_data_e_maior_versao_no_mesmo_tipo() -> None:
    base = {"cnpj": PETROBRAS, "dt_refer": dt.date(2025, 12, 31), "qt_on": 1, "qt_pn": 2}
    linhas = B.montar_acoes_emitidas([
        {**base, "tipo_documento": "ITR", "versao": 9, "qt_on": 10},
        {**base, "tipo_documento": "DFP", "versao": 1, "qt_on": 20},
        {**base, "tipo_documento": "DFP", "versao": 2, "qt_on": 30},
    ])
    (linha,) = linhas
    assert list(linha) == COLUNAS["acoes_emitidas"]
    assert linha["qtd_on"] == 30


FIM_2024 = dt.date(2024, 12, 31)


def _dfp_2024(cd: str, ds: str, valor: float, *, demo: str = "BPP", cd_cvm: int = 7617) -> dict:
    """Linha da DFP 2024 (MIL). BPA e BPP sao saldo na data (ini = fim); DRE e DFC, o ano."""
    ini = FIM_2024 if demo in ("BPA", "BPP") else dt.date(2024, 1, 1)
    return _bal(cd, ds, valor, demo=demo, ini=ini, fim=FIM_2024, refer=FIM_2024, tipo="DFP",
                cd_cvm=cd_cvm)


def _por_periodo(linhas: list[dict]) -> dict[tuple, dict]:
    return {(linha["data_inicio_periodo"], linha["data_fim_periodo"]): linha for linha in linhas}


def _itr_q1_petrobras() -> list[dict]:
    """ITR 1T25 da Petrobras (MIL). A DFC do ITR e acumulada: no 1T, o acumulado e o proprio."""
    ini, fim = dt.date(2025, 1, 1), dt.date(2025, 3, 31)
    return [
        _bal("3.01", "Receita de vendas", 50000000.0, ini=ini, fim=fim, refer=fim),
        _bal("6.01.01.04", "Depreciação, depleção e amortização", 19000000.0, demo="DFC",
             ini=ini, fim=fim, refer=fim),
    ]


def _itr_q2_petrobras(da_ytd: float = 39928000.0) -> list[dict]:
    """ITR 2T25 da Petrobras (MIL): DRE trimestral e acumulada, DFC acumulada, BPA em 30/06."""
    ini_ano = dt.date(2025, 1, 1)
    return [
        _bal("3.01", "Receita de vendas", 30000000.0),
        _bal("3.05", "Resultado Antes do Resultado Financeiro e dos Tributos", 30468000.0),
        _bal("3.01", "Receita de vendas", 100000000.0, ini=ini_ano),
        _bal("3.05", "Resultado Antes do Resultado Financeiro e dos Tributos", 73508000.0,
             ini=ini_ano),
        _bal("6.01.01.04", "Depreciação, depleção e amortização", da_ytd, demo="DFC",
             ini=ini_ano),
        _bal("1.01.01", "Caixa e Equivalentes de Caixa", 38177000.0, demo="BPA",
             ini=dt.date(2025, 6, 30)),
        _bal("1.01.02", "Aplicações Financeiras", 13670000.0, demo="BPA",
             ini=dt.date(2025, 6, 30)),
    ]


def test_contas_novas_itr_q2_petrobras_valores_reais_mil() -> None:
    linhas = _por_periodo(B.montar_balancos(_itr_q2_petrobras()))
    acumulado = linhas[(dt.date(2025, 1, 1), dt.date(2025, 6, 30))]
    trimestre = linhas[(dt.date(2025, 4, 1), dt.date(2025, 6, 30))]
    assert acumulado["ebit"] == 73508000000.0
    assert acumulado["depreciacao_amortizacao"] == 39928000000.0
    assert acumulado["caixa"] == 38177000000.0
    assert acumulado["aplicacoes_financeiras"] == 13670000000.0
    assert trimestre["ebit"] == 30468000000.0
    assert trimestre["depreciacao_amortizacao"] is None
    assert trimestre["caixa"] == 38177000000.0
    assert trimestre["aplicacoes_financeiras"] == 13670000000.0


def test_da_do_trimestre_e_diferenca_dos_acumulados() -> None:
    linhas = _por_periodo(B.montar_balancos(_itr_q1_petrobras() + _itr_q2_petrobras()))
    q1 = linhas[(dt.date(2025, 1, 1), dt.date(2025, 3, 31))]
    q2_tri = linhas[(dt.date(2025, 4, 1), dt.date(2025, 6, 30))]
    q2_acum = linhas[(dt.date(2025, 1, 1), dt.date(2025, 6, 30))]
    assert q1["depreciacao_amortizacao"] == 19000000000.0
    assert q2_tri["depreciacao_amortizacao"] == 20928000000.0
    assert q2_acum["depreciacao_amortizacao"] == 39928000000.0


def test_da_do_trimestre_fica_none_se_diferenca_sair_negativa() -> None:
    # Acumulado do 2T menor que o do 1T (reapresentacao): a diferenca sairia negativa.
    dfc_menor = _itr_q2_petrobras(da_ytd=10000000.0)
    linhas = _por_periodo(B.montar_balancos(_itr_q1_petrobras() + dfc_menor))
    q2_tri = linhas[(dt.date(2025, 4, 1), dt.date(2025, 6, 30))]
    assert q2_tri["depreciacao_amortizacao"] is None


def test_emprestimos_somam_debentures_e_ignoram_arrendamento() -> None:
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE"),
        _dfp_2024("2.01.04.01", "Empréstimos e Financiamentos", 1293000.0),
        _dfp_2024("2.01.04.02", "Debêntures", 80000.0),
        _dfp_2024("2.01.04.03", "Financiamento por Arrendamento", 907000.0),
        _dfp_2024("2.02.01.01", "Empréstimos e Financiamentos", 5347000.0),
        _dfp_2024("2.02.01.02", "Debêntures", 4392000.0),
    ])
    assert linha["emprestimos_curto_prazo"] == 1373000000.0
    assert linha["emprestimos_longo_prazo"] == 9739000000.0


def test_sem_conta_de_emprestimo_fica_none_e_nao_zero() -> None:
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE"),
        _dfp_2024("2.03", "Patrimônio Líquido Consolidado", 500.0),
    ])
    assert linha["emprestimos_curto_prazo"] is None
    assert linha["emprestimos_longo_prazo"] is None


def test_emprestimos_usam_pai_quando_subcontas_zeradas_como_csn() -> None:
    # CSN (4030): a divida so vem na conta-pai; as subcontas saem zeradas.
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE"),
        _dfp_2024("2.01.04", "Empréstimos e Financiamentos", 10428559.0),
        _dfp_2024("2.01.04.01", "Empréstimos e Financiamentos", 0.0),
        _dfp_2024("2.01.04.02", "Debêntures", 0.0),
        _dfp_2024("2.02.01", "Empréstimos e Financiamentos", 42495988.0),
        _dfp_2024("2.02.01.01", "Empréstimos e Financiamentos", 0.0),
        _dfp_2024("2.02.01.02", "Debêntures", 0.0),
    ])
    assert linha["emprestimos_curto_prazo"] == 10428559000.0
    assert linha["emprestimos_longo_prazo"] == 42495988000.0


def test_pai_menos_arrendamento_quando_subcontas_zeradas() -> None:
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE"),
        _dfp_2024("2.01.04", "Empréstimos e Financiamentos", 1000.0),
        _dfp_2024("2.01.04.01", "Empréstimos e Financiamentos", 0.0),
        _dfp_2024("2.01.04.03", "Financiamento por Arrendamento", 300.0),
        _dfp_2024("2.02.01", "Empréstimos e Financiamentos", 5000.0),
        _dfp_2024("2.02.01.03", "Financiamento por Arrendamento", 1200.0),
    ])
    assert linha["emprestimos_curto_prazo"] == 700000.0
    assert linha["emprestimos_longo_prazo"] == 3800000.0


def test_subcontas_positivas_ignoram_o_pai() -> None:
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE"),
        _dfp_2024("2.01.04", "Empréstimos e Financiamentos", 9999.0),
        _dfp_2024("2.01.04.01", "Empréstimos e Financiamentos", 1293000.0),
        _dfp_2024("2.01.04.02", "Debêntures", 80000.0),
    ])
    assert linha["emprestimos_curto_prazo"] == 1373000000.0


def test_pai_zerado_sem_subcontas_fica_none() -> None:
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE"),
        _dfp_2024("2.01.04", "Empréstimos e Financiamentos", 0.0),
    ])
    assert linha["emprestimos_curto_prazo"] is None


def test_depreciacao_soma_varias_linhas_e_fica_positiva() -> None:
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE"),
        _dfp_2024("6.01.01.02", "Depreciação e amortização", 1107622.0, demo="DFC"),
        _dfp_2024("6.01.01.03", "Exaustão dos ativos biológicos", 1317945.0, demo="DFC"),
    ])
    assert linha["depreciacao_amortizacao"] == 2425567000.0


def test_depreciacao_com_sinal_negativo_sai_positiva_e_sem_da_fica_none() -> None:
    def conta(cd: str, ds: str, valor: float) -> dict:
        return {"cd_conta": cd, "ds_conta": ds, "valor": valor}

    negativas = [
        conta("6.01.01.02", "Depreciação e amortização", -5.0),
        conta("6.01.01.03", "Exaustão", -3.0),
    ]
    assert B._depreciacao_amortizacao(negativas) == 8.0
    juros = [conta("6.01.01.05", "Juros e variações cambiais", 7.0)]
    assert B._depreciacao_amortizacao(juros) is None


@pytest.mark.parametrize(("ds", "esperado"), [
    ("Depreciação, depleção e amortização", True),
    ("Depreciação, amortização e exaustão", True),
    ("Exaustão dos ativos biológicos", True),
    ("Amortiz. de ativos de contratos com clientes  "
     "- direitos de exclusividade e direito de uso", True),
    ("Depreciação, Amortização e Impairment", True),
    ("Amortização de mais valia", True),
    ("Depreciação IFRS-16", True),
    ("Despesas com depreciação e amortização", True),
    ("Amortização do direito de concessão", True),
    ("Amortização de Custo de Transação de Empréstimos", False),
    ("Amortização de custos de transação de Debêntures", False),
    ("Amortizaçao dos custos de captação", False),
    ("Amortização do custo de transação, ágio e deságio", False),
    ("Amortização de mais valia estoque", False),
    ("Atualização dos ativos financeiros ao custo amortizado", False),
    ("Resultado de Juros e Variação Cambial de Ativos Financeiros ao Custo Amortizado", False),
    ("Amortização de Despesas Antecipadas", False),
    ("Juros, amortização de captação e variações cambiais líquidas", False),
    ("Encargos sobre empréstimos, financiamentos, debêntures e amortização de custo de transação",
     False),
    ("Amortização do custo de aquisição de empréstimo", False),
])
def test_e_depreciacao_inclui_e_exclui_descricoes_reais_da_cvm(ds: str, esperado: bool) -> None:
    assert B._e_depreciacao(ds) is esperado


def test_banco_e_seguradora_saem_em_branco() -> None:
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE", cd_cvm=16659),
        _dfp_2024("3.03", "Resultado Bruto", 400.0, demo="DRE", cd_cvm=16659),
        _dfp_2024("3.05", "Resultado Antes do Resultado Financeiro e dos Tributos", 300.0,
                  demo="DRE", cd_cvm=16659),
        _dfp_2024("3.11", "Lucro/Prejuízo Consolidado do Período", 200.0, demo="DRE", cd_cvm=16659),
        _dfp_2024("2.03", "Patrimônio Líquido Consolidado", 800.0, cd_cvm=16659),
        _dfp_2024("1.01.01", "Caixa e Equivalentes de Caixa", 50.0, demo="BPA", cd_cvm=16659),
        _dfp_2024("6.01.01.03", "Depreciações", 10.0, demo="DFC", cd_cvm=16659),
    ])
    for coluna in B.COLUNAS_SEM_FINANCEIRAS:
        assert linha[coluna] is None, coluna
    assert linha["receita_liquida"] == 1000000.0
    assert linha["lucro_liquido"] == 200000.0
    assert linha["patrimonio_liquido"] == 800000.0


def test_banco_sem_plano_industrial_nao_preenche_ebit_nem_caixa() -> None:
    # ITUB4 tem o proprio plano: 3.05 e "Resultado Antes dos Tributos" e caixa nao casa.
    (linha,) = B.montar_balancos([
        _dfp_2024("3.05", "Resultado Antes dos Tributos sobre o Lucro", 300.0, demo="DRE",
                  cd_cvm=19348),
        _dfp_2024("1.01.01", "Caixa", 50.0, demo="BPA", cd_cvm=19348),
    ])
    assert linha["ebit"] is None
    assert linha["caixa"] is None


def test_dfp_preenche_d_a_no_periodo_anual() -> None:
    (linha,) = B.montar_balancos([
        _dfp_2024("3.01", "Receita de vendas", 1000.0, demo="DRE"),
        _dfp_2024("6.01.01.04", "Depreciação, depleção e amortização", 39928.0, demo="DFC"),
    ])
    assert linha["depreciacao_amortizacao"] == 39928000.0


# -- proventos -------------------------------------------------------------------------------

def test_fator_ajuste_cobre_desdobramento_bonificacao_e_grupamento() -> None:
    eventos = {ev["label"]: ev for ev in _suplemento_petr_payloads()}
    assert B.fator_ajuste(eventos["DESDOBRAMENTO"]) == pytest.approx(0.5)
    assert B.fator_ajuste(eventos["BONIFICACAO"]) == pytest.approx(0.75)
    assert B.fator_ajuste(eventos["GRUPAMENTO"]) == pytest.approx(100.0)


def _suplemento_petr_payloads() -> list[dict]:
    payload = _carregar("b3_supplement_PETR.json")[0]
    vistos: dict[str, dict] = {}
    for ev in payload["stockDividends"]:
        vistos.setdefault(ev["label"], ev)
    return list(vistos.values())


def test_desdobramento_posterior_ajusta_o_valor_por_acao() -> None:
    linhas = B.montar_proventos(
        [_prov(valor=0.45, data_com=dt.date(2008, 1, 10))], _suplemento_petr(), _cadastro_petr()
    )
    (linha,) = linhas
    assert list(linha) == COLUNAS["proventos"]
    assert linha["ticker"] == "PETR3"
    assert linha["valor_por_acao"] == pytest.approx(0.225)


def test_grupamento_e_desdobramento_posteriores_se_compoem() -> None:
    # Data-com em 1999: depois dela vieram o grupamento (2000, x100) e o desdobramento (2008, /2).
    (linha,) = B.montar_proventos(
        [_prov(valor=0.10, data_com=dt.date(1999, 1, 10))], _suplemento_petr(), _cadastro_petr()
    )
    assert linha["valor_por_acao"] == pytest.approx(5.0)


def test_provento_sem_evento_posterior_nao_muda() -> None:
    (linha,) = B.montar_proventos(
        [_prov(valor=0.47156696, data_com=dt.date(2026, 8, 21), acao="DIVIDENDO")],
        _suplemento_petr(), _cadastro_petr(),
    )
    assert linha["valor_por_acao"] == pytest.approx(0.47156696)
    assert linha["data_pagamento"] == dt.date(2026, 12, 21)


def test_dois_jcps_na_mesma_data_com_somam() -> None:
    proventos = [
        _prov(valor=0.67407131, data_com=dt.date(2026, 8, 21), acao="JRS CAP PROPRIO"),
        _prov(valor=0.20250435, data_com=dt.date(2026, 8, 21), acao="JRS CAP PROPRIO"),
    ]
    (linha,) = B.montar_proventos(proventos, _suplemento_petr(), _cadastro_petr())
    assert linha["tipo"] == "JCP"
    assert linha["valor_por_acao"] == pytest.approx(0.67407131 + 0.20250435)
    assert linha["data_pagamento"] is not None


def test_ticker_por_type_stock_e_tipo_por_corporate_action_e_desconhecidos_pulados() -> None:
    proventos = [
        _prov(type_stock="PNA"),
        _prov(type_stock="XYZ"),                                  # tipo desconhecido: pula
        _prov(acao="RENDIMENTO"),                                 # fora da entrega: pula
        _prov(acao="RESTITUICAO DE CAPITAL"),                     # fora da entrega: pula
    ]
    linhas = B.montar_proventos(proventos, [], _cadastro_petr())
    assert [(r["ticker"], r["tipo"]) for r in linhas] == [("PETR5", "DIVIDENDO")]


def test_unit_e_composta_pelas_acoes_on_e_pn_da_mesma_empresa() -> None:
    # KLBN11 = 1 ON + 4 PN (config.UNITS). Sem linha de unit na B3.
    cadastro = [{"issuing_company": "KLBN", "other_codes": ["KLBN3", "KLBN4", "KLBN11"],
                 "payload": {}}]
    proventos = [
        _prov(ic="KLBN", type_stock="ON", valor=0.10, data_com=dt.date(2026, 3, 1)),
        _prov(ic="KLBN", type_stock="PN", valor=0.20, data_com=dt.date(2026, 3, 1)),
    ]
    linhas = B.montar_proventos(proventos, [], cadastro)
    unit = [r for r in linhas if r["ticker"] == "KLBN11"]
    assert len(unit) == 1
    assert unit[0]["valor_por_acao"] == pytest.approx(1 * 0.10 + 4 * 0.20)
    assert unit[0]["tipo"] == "DIVIDENDO"


def test_jbss3_antes_da_troca_vira_jbss32_com_valor_dobrado() -> None:
    # JBSS3 -> JBSS32 ate 2025-06-08 (fator 0,5): R$ 1 por JBSS3 = R$ 2 por JBSS32.
    (linha,) = B.montar_proventos(
        [_prov(ic="JBSS", type_stock="ON", valor=1.0, data_com=dt.date(2024, 5, 1))],
        [], [],
    )
    assert linha["ticker"] == "JBSS32"
    assert linha["valor_por_acao"] == pytest.approx(2.0)


def test_natu3_na_holding_nao_entra() -> None:
    # NATU3 durante o periodo da holding era a subsidiaria, nao o papel negociado.
    linhas = B.montar_proventos(
        [_prov(ic="NATU", type_stock="ON", valor=0.5, data_com=dt.date(2021, 6, 23))],
        [], [],
    )
    assert linhas == []


def test_ntco3_antes_da_troca_vira_natu3_com_o_mesmo_valor() -> None:
    (linha,) = B.montar_proventos(
        [_prov(ic="NTCO", type_stock="ON", valor=0.5, data_com=dt.date(2022, 3, 10))],
        [], [],
    )
    assert linha["ticker"] == "NATU3"
    assert linha["valor_por_acao"] == pytest.approx(0.5)


def test_dividendo_de_bdr_so_no_suplemento_entra_como_jbss32() -> None:
    cadastro = [{
        "issuing_company": "JBSS", "other_codes": ["JBSS32", "JBSS3"],
        "payload": {"otherCodes": [
            {"isin": "BRJBSSBDR002", "code": "JBSS32"},
            {"isin": "BRJBSSACNOR8", "code": "JBSS3"},
        ]},
    }]
    suplemento = [{
        "issuing_company": "JBSS", "tipo": "cash",
        "payload": {
            "label": "DIVIDENDO", "isinCode": "BRJBSSBDR002",
            "lastDatePrior": "18/05/2026", "paymentDate": "22/06/2026",
            "rate": "4,27635785",
        },
    }]
    (linha,) = B.montar_proventos([], suplemento, cadastro)
    assert linha["ticker"] == "JBSS32"
    assert linha["tipo"] == "DIVIDENDO"
    assert linha["data_com"] == dt.date(2026, 5, 18)
    assert linha["valor_por_acao"] == pytest.approx(4.27635785)
    assert linha["data_pagamento"] == dt.date(2026, 6, 22)


# -- eventos em acoes (eventos_acoes) --------------------------------------------------------

def _stock(ic: str, payload: dict) -> dict:
    return {"issuing_company": ic, "tipo": "stock", "payload": payload}


def test_eventos_acoes_petr_desdobramento_grupamento_e_bonificacao() -> None:
    linhas = B.montar_eventos_acoes(_suplemento_petr(), _cadastro_petr())
    assert all(list(linha) == COLUNAS["eventos_acoes"] for linha in linhas)
    por = {(r["ticker"], r["data_com"], r["tipo"]): r for r in linhas}
    for ticker in ("PETR3", "PETR4"):
        desdobramento = por[(ticker, dt.date(2008, 4, 25), "DESDOBRAMENTO")]
        assert desdobramento["fator"] == pytest.approx(2.0)
        grupamento = por[(ticker, dt.date(2000, 6, 21), "GRUPAMENTO")]
        assert grupamento["fator"] == pytest.approx(0.01)
        assert grupamento["cnpj"] == PETROBRAS
    # 3 eventos em acoes x ON e PN; os 24 pagamentos em dinheiro ficam de fora.
    assert len(linhas) == 6
    assert {r["tipo"] for r in linhas} == {"DESDOBRAMENTO", "GRUPAMENTO", "BONIFICACAO"}


def test_grupamento_irbr_fator_e_uma_trigesima_por_acao() -> None:
    evento = _stock("IRBR", {
        "label": "GRUPAMENTO", "factor": "0,03333333334", "lastDatePrior": "24/01/2023",
        "isinCode": "BRIRBRACNOR4", "assetIssued": "BRIRBRACNOR4", "approvedOn": "17/01/2023",
    })
    (linha,) = B.montar_eventos_acoes([evento], [])
    assert linha["ticker"] == "IRBR3"
    assert linha["fator"] == pytest.approx(1 / 30, rel=1e-6)
    assert linha["data_aprovacao"] == dt.date(2023, 1, 17)


def test_mesmo_evento_duas_vezes_gera_uma_linha() -> None:
    evento = _stock("IRBR", {
        "label": "GRUPAMENTO", "factor": "0,03333333334", "lastDatePrior": "24/01/2023",
        "isinCode": "BRIRBRACNOR4", "assetIssued": "BRIRBRACNOR4", "approvedOn": "17/01/2023",
    })
    linhas = B.montar_eventos_acoes([evento, dict(evento)], [])
    assert len(linhas) == 1


def test_incorporacao_e_cis_red_cap_ficam_de_fora() -> None:
    suplemento = [
        _stock("JBSS", {
            "label": "INCORPORACAO", "factor": "50,00000000000", "lastDatePrior": "10/03/2023",
            "isinCode": "BRJBSSACNOR8", "assetIssued": "BRJ8BSARNPR0",
        }),
        _stock("CIS", {
            "label": "CIS RED CAP", "factor": "10,00000000000", "lastDatePrior": "10/03/2023",
            "isinCode": "BRCISACNOR3", "assetIssued": "BRCISACNOR3",
        }),
    ]
    assert B.montar_eventos_acoes(suplemento, []) == []


def test_bonificacao_com_isin_emitido_diferente_do_isin() -> None:
    evento = _stock("RENT", {
        "label": "BONIFICACAO", "factor": "3,84609533429", "lastDatePrior": "29/12/2025",
        "isinCode": "BRRENTACNOR4", "assetIssued": "BRRENTACNPR1", "approvedOn": "19/12/2025",
    })
    (linha,) = B.montar_eventos_acoes([evento], [])
    assert linha["ticker"] == "RENT3"
    assert linha["isin"] == "BRRENTACNOR4"
    assert linha["isin_emitido"] == "BRRENTACNPR1"
    assert linha["fator"] == pytest.approx(1 + 3.84609533429 / 100)


def test_fatores_distintos_na_mesma_chave_se_multiplicam_com_aviso(caplog) -> None:
    base = {
        "label": "BONIFICACAO", "lastDatePrior": "29/12/2025",
        "isinCode": "BRRENTACNOR4", "assetIssued": "BRRENTACNOR4",
    }
    suplemento = [
        _stock("RENT", {**base, "factor": "20,00000000000"}),
        _stock("RENT", {**base, "factor": "30,00000000000"}),
    ]
    (linha,) = B.montar_eventos_acoes(suplemento, [])
    assert linha["fator"] == pytest.approx(1.2 * 1.3)
    avisos = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("multiplicados" in aviso for aviso in avisos)


def test_evento_sem_data_ou_com_fator_invalido_e_pulado() -> None:
    base = {"label": "BONIFICACAO", "isinCode": "BRRENTACNOR4", "assetIssued": "BRRENTACNOR4"}
    suplemento = [
        _stock("RENT", {**base, "factor": "3,00000000000", "lastDatePrior": None}),
        _stock("RENT", {**base, "factor": "", "lastDatePrior": "29/12/2025"}),
    ]
    assert B.montar_eventos_acoes(suplemento, []) == []


# -- escala das acoes (CVM mistura unidades e milhares) ----------------------------------------

MARCOS = [dt.date(2025, 3, 31), dt.date(2025, 6, 30), dt.date(2025, 9, 30)]


def _acao(data: dt.date, on: int, pn: int = 0) -> dict:
    return {"cnpj": PETROBRAS, "tipo_documento": "ITR", "dt_refer": data, "versao": 1,
            "qt_on": on, "qt_pn": pn}


def _volumes_diarios(valor: float, inicio: dt.date = dt.date(2024, 1, 1)) -> dict:
    """Volume de 50 mi por dia, em todo dia de `inicio` a jun/2026, para o CNPJ da Petrobras."""
    dias = [inicio + dt.timedelta(days=n) for n in range((dt.date(2026, 6, 30) - inicio).days + 1)]
    return {PETROBRAS: {d: valor for d in dias}}


def test_acoes_sempre_em_milhares_com_giro_alto_saem_em_unidades() -> None:
    # 4,26 mi de acoes em milhares = 4,26 bi reais de acoes: giro de ~12% ao dia.
    linhas = B.montar_acoes_emitidas([_acao(d, 4_255_763) for d in MARCOS], _volumes_diarios(5e7))
    assert [r["qtd_on"] for r in linhas] == [4_255_763_000] * 3


def test_cnpj_alternando_unidades_e_milhares_fica_tudo_em_unidades() -> None:
    linhas = B.montar_acoes_emitidas([
        _acao(MARCOS[0], 4_255_763_000),
        _acao(MARCOS[1], 4_255_763, 1_000),
        _acao(MARCOS[2], 4_255_763_000),
    ], _volumes_diarios(5e7))
    assert [r["qtd_on"] for r in linhas] == [4_255_763_000] * 3
    assert [r["qtd_pn"] for r in linhas] == [0, 1_000_000, 0]


def test_cnpj_em_unidades_com_giro_baixo_fica_intacto() -> None:
    linhas = B.montar_acoes_emitidas(
        [_acao(d, 4_255_763_000) for d in MARCOS], _volumes_diarios(5e7)
    )
    assert [r["qtd_on"] for r in linhas] == [4_255_763_000] * 3


def test_segmento_sem_volume_herda_escala_oposta_do_vizinho_decidido() -> None:
    # Dez/2024 nao tem volume antes dela (sem giro); o trimestre seguinte, em unidades, decide.
    linhas = B.montar_acoes_emitidas([
        _acao(dt.date(2024, 12, 31), 4_000_000),
        _acao(MARCOS[0], 4_000_000_000),
    ], _volumes_diarios(5e7, inicio=dt.date(2025, 1, 1)))
    assert [r["qtd_on"] for r in linhas] == [4_000_000_000, 4_000_000_000]


def test_heranca_de_escala_se_propaga_em_cadeia_ate_o_segmento_decidido() -> None:
    # Caso Natura: milhares, unidades, milhares antes da listagem (sem volume); so o ultimo
    # segmento tem giro. A escala alterna a cada troca de 1000x a partir dele.
    linhas = B.montar_acoes_emitidas([
        _acao(dt.date(2024, 3, 31), 920_205),
        _acao(dt.date(2024, 6, 30), 920_205_397),
        _acao(dt.date(2024, 9, 30), 920_205),
        _acao(MARCOS[0], 920_205_397),
    ], _volumes_diarios(5e6, inicio=dt.date(2025, 1, 1)))
    assert [r["qtd_on"] for r in linhas] == [920_205_000, 920_205_397, 920_205_000, 920_205_397]


def test_sem_volumes_a_escala_nao_e_corrigida() -> None:
    linhas = B.montar_acoes_emitidas([_acao(d, 4_255_763) for d in MARCOS])
    assert [r["qtd_on"] for r in linhas] == [4_255_763] * 3


def test_valor_isolado_10x_e_corrigido_pelos_vizinhos_e_avisado(caplog) -> None:
    linhas = B.montar_acoes_emitidas([
        _acao(MARCOS[0], 1_000_000_000),
        _acao(MARCOS[1], 10_000_000_000),
        _acao(MARCOS[2], 1_000_000_000),
    ], _volumes_diarios(5e7), anuladas={})
    assert [r["qtd_on"] for r in linhas] == [1_000_000_000] * 3
    assert [r["situacao"] for r in linhas] == ["ok", "corrigido_digito", "ok"]
    avisos = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("2025-06-30" in aviso and "10000000000" in aviso for aviso in avisos)


CMIN = "08902291000115"
GMAT = "24990777000109"


def _acao_de(cnpj: str, data: dt.date, on: int, pn: int = 0) -> dict:
    return {**_acao(data, on, pn), "cnpj": cnpj}


def test_erro_de_digito_10x_no_meio_da_serie_cmin3_e_corrigido() -> None:
    linhas = B.montar_acoes_emitidas([
        _acao_de(CMIN, dt.date(2025, 9, 30), 5_485_338_835),
        _acao_de(CMIN, dt.date(2025, 12, 31), 54_320_445_380),
        _acao_de(CMIN, dt.date(2026, 3, 31), 5_432_044_538),
    ], anuladas={})
    assert [r["qtd_on"] for r in linhas] == [5_485_338_835, 5_432_044_538, 5_432_044_538]
    assert [r["situacao"] for r in linhas] == ["ok", "corrigido_digito", "ok"]


def test_erro_de_digito_10x_no_meio_da_serie_gmat3_2022_e_corrigido() -> None:
    linhas = B.montar_acoes_emitidas([
        _acao_de(GMAT, dt.date(2022, 3, 31), 2_209_346_079),
        _acao_de(GMAT, dt.date(2022, 6, 30), 22_093_460_790),
        _acao_de(GMAT, dt.date(2022, 9, 30), 2_209_346_079),
    ], anuladas={})
    assert [r["qtd_on"] for r in linhas] == [2_209_346_079] * 3
    assert [r["situacao"] for r in linhas] == ["ok", "corrigido_digito", "ok"]


def test_gmat3_2023_anulada_vira_null_e_sem_anulacao_fica_suspeita() -> None:
    serie = [
        _acao_de(GMAT, dt.date(2023, 3, 31), 2_209_346_079),
        _acao_de(GMAT, dt.date(2023, 6, 30), 463_952_163),
        _acao_de(GMAT, dt.date(2023, 9, 30), 2_209_346_079),
    ]
    anulada = B.montar_acoes_emitidas(serie, anuladas={(GMAT, dt.date(2023, 6, 30)): "teste"})
    assert [r["qtd_on"] for r in anulada] == [2_209_346_079, None, 2_209_346_079]
    assert [r["situacao"] for r in anulada] == ["ok", "anulado", "ok"]
    # Com anuladas={} a linha nao e anulada: fica no valor e so vira suspeita.
    sem_anulacao = B.montar_acoes_emitidas(serie, anuladas={})
    assert [r["qtd_on"] for r in sem_anulacao] == [2_209_346_079, 463_952_163, 2_209_346_079]
    assert [r["situacao"] for r in sem_anulacao] == ["ok", "suspeito", "ok"]


def test_mudanca_real_permanente_de_10x_fica_intacta() -> None:
    linhas = B.montar_acoes_emitidas([
        _acao(MARCOS[0], 1_000_000_000),
        _acao(MARCOS[1], 10_000_000_000),
        _acao(MARCOS[2], 10_000_000_000),
    ], anuladas={})
    assert [r["qtd_on"] for r in linhas] == [1_000_000_000, 10_000_000_000, 10_000_000_000]
    assert [r["situacao"] for r in linhas] == ["ok"] * 3


def test_ultima_linha_10x_fica_com_valor_e_vira_suspeita() -> None:
    linhas = B.montar_acoes_emitidas([
        _acao(MARCOS[0], 1_000_000_000),
        _acao(MARCOS[1], 1_000_000_000),
        _acao(MARCOS[2], 10_000_000_000),
    ], anuladas={})
    assert [r["qtd_on"] for r in linhas] == [1_000_000_000, 1_000_000_000, 10_000_000_000]
    assert [r["situacao"] for r in linhas] == ["ok", "ok", "suspeito"]


def test_acoes_cvm_anuladas_tem_as_11_linhas_conferidas_no_banco() -> None:
    from tcc_scraper.config import ACOES_CVM_ANULADAS

    assert set(ACOES_CVM_ANULADAS) == {
        ("24990777000109", dt.date(2023, 6, 30)),
        ("42500384000151", dt.date(2021, 6, 30)),
        ("42500384000151", dt.date(2021, 9, 30)),
        ("60543816000193", dt.date(2021, 3, 31)),
        ("60543816000193", dt.date(2021, 6, 30)),
        ("60543816000193", dt.date(2021, 9, 30)),
        ("53153938000108", dt.date(2023, 12, 31)),
        ("53153938000108", dt.date(2024, 3, 31)),
        ("53153938000108", dt.date(2024, 6, 30)),
        ("53153938000108", dt.date(2024, 9, 30)),
        ("53153938000108", dt.date(2024, 12, 31)),
    }


def test_volumes_por_cnpj_soma_on_e_pn_e_tira_o_sa() -> None:
    cadastro = [
        {"ticker": "PETR3", "cnpj": PETROBRAS},
        {"ticker": "PETR4", "cnpj": PETROBRAS},
        {"ticker": "SEMCNPJ3", "cnpj": None},
    ]
    dia = dt.date(2026, 6, 18)
    precos = [
        {"ticker": "PETR3.SA", "date": dia, "volume": 100},
        {"ticker": "PETR4.SA", "date": dia, "volume": 50},
        {"ticker": "PETR4.SA", "date": dt.date(2026, 6, 19), "volume": None},
        {"ticker": "SEMCNPJ3.SA", "date": dia, "volume": 999},
        {"ticker": "^BVSP", "date": dia, "volume": 999},
    ]
    assert B.volumes_por_cnpj(cadastro, precos) == {PETROBRAS: {dia: 150.0}}


# -- base atual das acoes (qtd_*_base_atual, a partir de eventos_acoes) --------------------------

AZUL_CNPJ = "09305994000129"
AZUL_ISIN = "BRAZULACNOR8"
IRBR_CNPJ = "33376989000191"
IRBR_ISIN = "BRIRBRACNOR4"


def _linha_base(
    cnpj: str, dt_refer: dt.date, dt_receb: dt.date | None, on: int, pn: int = 0
) -> dict:
    return {"cnpj": cnpj, "tipo_documento": "ITR", "dt_refer": dt_refer, "versao": 1,
            "qt_on": on, "qt_pn": pn, "dt_receb": dt_receb}


def _evento_base(ticker: str, cnpj: str, data_com: dt.date, fator: float, isin: str,
                 tipo: str = "GRUPAMENTO", isin_emitido: str | None = None) -> dict:
    return {"ticker": ticker, "data_com": data_com, "tipo": tipo, "fator": fator,
            "isin": isin, "isin_emitido": isin_emitido or isin, "cnpj": cnpj,
            "data_aprovacao": None}


SERIE_AZUL = [
    _linha_base(AZUL_CNPJ, dt.date(2025, 9, 30), dt.date(2025, 11, 10), 2_128_965_121, 896_039_753),
    _linha_base(AZUL_CNPJ, dt.date(2025, 12, 31), dt.date(2026, 3, 31), 2_128_965_121, 896_039_753),
    _linha_base(AZUL_CNPJ, dt.date(2026, 3, 31), dt.date(2026, 5, 7), 54_730_851_778_811, 0),
    _linha_base(AZUL_CNPJ, dt.date(2026, 6, 30), dt.date(2026, 8, 19), 368_557_924, 0),
]
EVENTOS_AZUL = [
    _evento_base("AZUL3", AZUL_CNPJ, dt.date(2026, 2, 18), 0.01333333334, AZUL_ISIN),
    _evento_base("AZUL3", AZUL_CNPJ, dt.date(2026, 4, 17), 0.0000066667, AZUL_ISIN),
]
SERIE_IRBR = [
    _linha_base(IRBR_CNPJ, dt.date(2022, 9, 30), dt.date(2022, 11, 10), 2_467_890_331, 1),
    _linha_base(IRBR_CNPJ, dt.date(2022, 12, 31), dt.date(2023, 3, 21), 82_263_011, 1),
    _linha_base(IRBR_CNPJ, dt.date(2023, 3, 31), dt.date(2023, 5, 10), 82_263_011, 1),
]
EVENTOS_IRBR = [_evento_base("IRBR3", IRBR_CNPJ, dt.date(2023, 1, 24), 0.03333333334, IRBR_ISIN)]


def test_base_atual_azul3_grupamento_converte_so_o_que_a_cvm_nao_refletiu() -> None:
    linhas = B.montar_acoes_emitidas(SERIE_AZUL, eventos=EVENTOS_AZUL, anuladas={})
    por_data = {r["data_referencia"]: r for r in linhas}
    # 2026-03: a CVM publicou a quantidade antiga (fora da base nova); o grupamento de abr/26 entra.
    assert por_data[dt.date(2026, 3, 31)]["qtd_on"] == 54_730_851_778_811
    assert por_data[dt.date(2026, 3, 31)]["qtd_on_base_atual"] == pytest.approx(
        364_874_000, rel=1e-3)
    # 2026-06: quantidade ja na base nova, nenhum evento pendente.
    assert por_data[dt.date(2026, 6, 30)]["qtd_on_base_atual"] == 368_557_924
    # 2025-12: os dois grupamentos (fev e abr/26) ainda estao pendentes.
    assert por_data[dt.date(2025, 12, 31)]["qtd_on_base_atual"] == pytest.approx(189, rel=2e-2)


def test_base_atual_pn_sem_eventos_fica_igual_a_quantidade() -> None:
    linhas = B.montar_acoes_emitidas(SERIE_AZUL, eventos=EVENTOS_AZUL, anuladas={})
    assert [r["qtd_pn_base_atual"] for r in linhas] == [r["qtd_pn"] for r in linhas]


def test_base_atual_irbr3_quantidade_ja_refletida_nao_e_convertida_de_novo() -> None:
    linhas = B.montar_acoes_emitidas(SERIE_IRBR, eventos=EVENTOS_IRBR, anuladas={})
    base = {r["data_referencia"]: r["qtd_on_base_atual"] for r in linhas}
    assert base[dt.date(2022, 9, 30)] == pytest.approx(82_263_011, rel=1e-6)
    assert base[dt.date(2022, 12, 31)] == 82_263_011
    assert base[dt.date(2023, 3, 31)] == 82_263_011


def test_base_atual_evento_com_data_com_igual_a_dt_refer_entra_e_anterior_nao() -> None:
    serie = [_linha_base(AZUL_CNPJ, dt.date(2026, 2, 18), None, 1_000_000)]
    igual = B.montar_acoes_emitidas(serie, eventos=[
        _evento_base("AZUL3", AZUL_CNPJ, dt.date(2026, 2, 18), 2.0, AZUL_ISIN),
    ], anuladas={})
    antes = B.montar_acoes_emitidas(serie, eventos=[
        _evento_base("AZUL3", AZUL_CNPJ, dt.date(2026, 2, 17), 2.0, AZUL_ISIN),
    ], anuladas={})
    assert igual[0]["qtd_on_base_atual"] == 2_000_000
    assert antes[0]["qtd_on_base_atual"] == 1_000_000


def test_base_atual_bonificacao_de_outra_classe_nao_altera_a_base() -> None:
    serie = [_linha_base(AZUL_CNPJ, dt.date(2026, 1, 31), None, 1_000, 500)]
    linhas = B.montar_acoes_emitidas(serie, eventos=[
        _evento_base("AZUL3", AZUL_CNPJ, dt.date(2026, 2, 18), 2.0, AZUL_ISIN,
                     tipo="BONIFICACAO", isin_emitido="BRAZULACNPR6"),
    ], anuladas={})
    assert linhas[0]["qtd_on_base_atual"] == 1_000
    assert linhas[0]["qtd_pn_base_atual"] == 500


def test_base_atual_sem_eventos_e_a_propria_quantidade() -> None:
    linhas = B.montar_acoes_emitidas(SERIE_AZUL, anuladas={})
    assert all(r["qtd_on_base_atual"] == r["qtd_on"] for r in linhas)
    assert all(r["qtd_pn_base_atual"] == r["qtd_pn"] for r in linhas)


# -- escala declarada errada nos balancos -------------------------------------------------------

def _itr_receita(fim: dt.date, valor: float, escala: str) -> dict:
    """Linha da receita (3.01) de um ITR trimestral, com a escala que a companhia declarou."""
    ini = dt.date(fim.year, fim.month - 2, 1)
    return _bal("3.01", "Receita de Venda de Bens e/ou Serviços", valor, ini=ini, fim=fim,
                refer=fim, escala=escala)


def test_documento_em_unidade_em_cnpj_mil_vira_mil() -> None:
    # 3T25 declarou UNIDADE, mas o valor esta em milhares: 120.000 mil = 120 mi de reais.
    linhas = B.montar_balancos([
        _itr_receita(dt.date(2025, 3, 31), 100_000.0, "MIL"),
        _itr_receita(dt.date(2025, 6, 30), 110_000.0, "MIL"),
        _itr_receita(dt.date(2025, 9, 30), 120_000.0, "UNIDADE"),
        _itr_receita(dt.date(2025, 12, 31), 130_000.0, "MIL"),
    ])
    periodos = _por_periodo(linhas)
    assert periodos[(dt.date(2025, 7, 1), dt.date(2025, 9, 30))]["receita_liquida"] == 120_000_000.0


def test_cnpj_sempre_em_unidade_fica_intacto() -> None:
    linhas = B.montar_balancos([
        _itr_receita(dt.date(2025, 3, 31), 100_000_000.0, "UNIDADE"),
        _itr_receita(dt.date(2025, 6, 30), 110_000_000.0, "UNIDADE"),
    ])
    assert [r["receita_liquida"] for r in linhas] == [100_000_000.0, 110_000_000.0]


def test_documento_em_unidade_de_magnitude_compativel_mantem_unidade() -> None:
    # 120 mi em unidades bate com os trimestres em MIL (~110 mi): nao e erro de escala.
    linhas = B.montar_balancos([
        _itr_receita(dt.date(2025, 3, 31), 100_000.0, "MIL"),
        _itr_receita(dt.date(2025, 6, 30), 110_000.0, "MIL"),
        _itr_receita(dt.date(2025, 9, 30), 120_000_000.0, "UNIDADE"),
    ])
    assert [r["receita_liquida"] for r in linhas] == [100_000_000.0, 110_000_000.0, 120_000_000.0]


def test_documento_em_mil_em_cnpj_unidade_vira_unidade() -> None:
    # 3T25 declarou MIL, mas o valor ja esta em unidades: 120 mi, nao 120 bi.
    linhas = B.montar_balancos([
        _itr_receita(dt.date(2025, 3, 31), 100_000_000.0, "UNIDADE"),
        _itr_receita(dt.date(2025, 6, 30), 110_000_000.0, "UNIDADE"),
        _itr_receita(dt.date(2025, 9, 30), 120_000_000.0, "MIL"),
        _itr_receita(dt.date(2025, 12, 31), 130_000_000.0, "UNIDADE"),
    ])
    assert [r["receita_liquida"] for r in linhas] == [
        100_000_000.0, 110_000_000.0, 120_000_000.0, 130_000_000.0,
    ]


# -- emissor por periodo (EMISSORES_POR_PERIODO, pela data de entrega) ----------------------------

JBS_ANTIGA = "02916265000160"  # JBS S.A. (codigo 20575): vale ate 2025-06-08, fator 0,5
NATURA = "71673990000177"  # Natura Cosmeticos (codigo 19550)
NATURA_HOLDING = "32785497000197"  # Natura &Co (codigo 24783), era o emissor de NTCO3


def test_balanco_de_emissor_antes_de_valer_sai_e_depois_fica() -> None:
    # JBS N.V. (80233) so vale a partir de 2025-06-09: o documento entregue antes sai.
    antes = [_bal("3.01", "Receita", 1000.0, cd_cvm=80233, receb=dt.date(2024, 12, 27))]
    assert B.montar_balancos(antes) == []
    depois = [_bal("3.01", "Receita", 1000.0, cd_cvm=80233, receb=dt.date(2025, 8, 13))]
    (linha,) = B.montar_balancos(depois)
    assert linha["data_entrega"] == dt.date(2025, 8, 13)


def test_balanco_natura_fora_do_periodo_da_natu3_sai_e_depois_fica() -> None:
    # Natura Cosmeticos (19550) nao vale de 2019-12-18 a 2025-07-01: ali o emissor e a holding.
    antes = [_bal("3.01", "Receita", 1000.0, cd_cvm=19550, receb=dt.date(2025, 5, 12))]
    assert B.montar_balancos(antes) == []
    depois = [_bal("3.01", "Receita", 1000.0, cd_cvm=19550, receb=dt.date(2025, 8, 11))]
    (linha,) = B.montar_balancos(depois)
    assert linha["data_entrega"] == dt.date(2025, 8, 11)


def test_balanco_sem_cd_cvm_fica() -> None:
    (linha,) = B.montar_balancos([_bal("3.01", "Receita", 1000.0, cd_cvm=None)])
    assert linha["receita_liquida"] == 1_000_000.0


def test_reapresentacao_usa_comparativo_posterior_que_passa_pelo_filtro() -> None:
    # JBS N.V.: o ITR entregue com receita zero e substituido pelo PENULTIMO de uma remessa
    # posterior. Essa linha crua tem o proprio dt_receb e tambem passa pelo filtro do emissor.
    original = _bal("3.01", "Receita", 0.0, cd_cvm=80233, receb=dt.date(2025, 6, 10),
                    ini=dt.date(2025, 1, 1), fim=dt.date(2025, 3, 31),
                    refer=dt.date(2025, 3, 31))
    comparativo = _bal("3.01", "Receita", 100.0, cd_cvm=80233, receb=dt.date(2025, 8, 13),
                       ini=dt.date(2025, 1, 1), fim=dt.date(2025, 3, 31),
                       refer=dt.date(2025, 6, 30), ordem="PENÚLTIMO")
    (linha,) = B.montar_balancos([original, comparativo])
    assert linha["receita_liquida"] == 100_000.0
    assert linha["data_entrega"] == dt.date(2025, 8, 13)


def test_acoes_de_emissor_antes_de_valer_saem_e_base_atual_usa_o_fator() -> None:
    # JBS (20575) vale ate 2025-06-08 com fator 0,5 (1 BDR JBSS32 = 2 acoes JBSS3).
    jbs = {"cnpj": JBS_ANTIGA, "tipo_documento": "ITR", "dt_refer": dt.date(2024, 3, 31),
           "versao": 1, "qt_on": 1_000_000, "qt_pn": 0, "cd_cvm": 20575,
           "dt_receb": dt.date(2024, 5, 10)}
    fora = {**jbs, "cd_cvm": 80233, "dt_refer": dt.date(2024, 12, 31),
            "dt_receb": dt.date(2024, 12, 27)}
    (linha,) = B.montar_acoes_emitidas([jbs, fora])
    assert linha["data_referencia"] == dt.date(2024, 3, 31)
    assert linha["qtd_on"] == 1_000_000
    assert linha["qtd_on_base_atual"] == 500_000
    assert linha["qtd_pn_base_atual"] == 0


def test_acao_de_versao_fora_do_emissor_nao_vence_a_versao_valida() -> None:
    # O filtro vem antes da escolha da maior versao: a versao 2 (fora do periodo) sai.
    base = {"cnpj": JBS_ANTIGA, "tipo_documento": "ITR", "dt_refer": dt.date(2024, 3, 31),
            "qt_on": 1_000_000, "qt_pn": 0, "cd_cvm": 20575, "dt_receb": dt.date(2024, 5, 10)}
    v1 = {**base, "versao": 1}
    v2 = {**base, "versao": 2, "cd_cvm": 80233, "qt_on": 9_000_000,
          "dt_receb": dt.date(2024, 12, 27)}
    (linha,) = B.montar_acoes_emitidas([v1, v2])
    assert linha["qtd_on"] == 1_000_000


def test_volumes_de_ticker_que_trocou_de_emissor_vao_para_o_cnpj_da_epoca() -> None:
    # NATU3 ate 2025-07-01 era o papel da holding (NTCO3); depois, da Natura Cosmeticos.
    cadastro = [{"ticker": "NATU3", "cnpj": NATURA}]
    precos = [
        {"ticker": "NATU3.SA", "date": dt.date(2021, 6, 18), "volume": 100},
        {"ticker": "NATU3.SA", "date": dt.date(2025, 8, 11), "volume": 50},
    ]
    assert B.volumes_por_cnpj(cadastro, precos) == {
        NATURA_HOLDING: {dt.date(2021, 6, 18): 100.0},
        NATURA: {dt.date(2025, 8, 11): 50.0},
    }
