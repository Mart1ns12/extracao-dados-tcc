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
        (Julgamento("citada_com_fato", "reportagem", "outro", 0.9, ()), "rss_nativo", True, True),
        (Julgamento("mencao", "reportagem", "outro", 0.9, ()), "google_news", True, False),
        (Julgamento("protagonista", "opiniao", "outro", 0.9, ()), "google_news", True, False),
        (Julgamento("errada", "reportagem", "outro", 0.9, ()), "google_news", True, False),
    ],
)
def test_aprovado_para_entrega_combinacoes(julgamento, origem, exigir, esperado) -> None:
    assert aprovado_para_entrega(julgamento, origem, exigir) is esperado


# -- precos e indices ------------------------------------------------------------------------

def _preco(ticker, close=10.0, adj=9.0, volume=1000, data=dt.date(2026, 6, 18)):
    return {"ticker": ticker, "date": data, "open": 1.0, "high": 2.0, "low": 0.5,
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


# -- balancos e acoes emitidas ---------------------------------------------------------------

def _bal(cd, ds, valor, *, demo="DRE", ini=dt.date(2025, 4, 1), fim=dt.date(2025, 6, 30),
         refer=dt.date(2025, 6, 30), versao=1, escala="MIL", tipo="ITR"):
    return {
        "cnpj": PETROBRAS, "tipo_documento": tipo, "dt_refer": refer, "versao": versao,
        "demonstracao": demo, "cd_conta": cd, "dt_ini_exerc": ini, "dt_fim_exerc": fim,
        "ds_conta": ds, "ordem_exerc": "ÚLTIMO", "escala_moeda": escala, "vl_conta": valor,
        "dt_receb": dt.date(2025, 8, 8),
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
