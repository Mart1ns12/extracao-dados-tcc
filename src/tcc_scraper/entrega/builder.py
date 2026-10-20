"""Montagem das 8 tabelas da entrega a partir das fontes raw/staging.

Cada `montar_*` e pura: recebe listas de dicts (linhas das fontes) e devolve `list[dict]` com
as chaves de `COLUNAS`. `build_entrega` carrega as fontes, monta cada tabela, grava por upsert
e, se pedido, exporta CSV. Regras de mapeamento: desenho 5.1 a 5.8.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from functools import cached_property
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, tuple_

from tcc_scraper.collectors.b3_base import data_b3, decimal_b3
from tcc_scraper.collectors.b3_cadastro import classificacao_b3
from tcc_scraper.config import (
    BENCHMARK_TICKER,
    CONTAS_CVM,
    INDICES_YAHOO,
    MACRO_ENTREGA,
    MUDANCAS_TICKER,
    NOMES_EMPRESA,
    TICKER_CODE_CVM,
    UNITS,
    get_settings,
    universo_coleta,
)
from tcc_scraper.db.models import Article, ArticleSentiment, ArticleTicker, Macro, Price
from tcc_scraper.db.models_entrega import (
    COLUNAS,
    AcoesEmitida,
    Balanco,
    Cadastro,
    Indice,
    MudancaTicker,
    Noticia,
    Preco,
    Provento,
)
from tcc_scraper.db.models_fontes import (
    RawB3Cadastro,
    RawB3Proventos,
    RawB3Suplemento,
    RawCvmAcoes,
    RawCvmBalanco,
    RawCvmCadastro,
    StagingNoticiaJulgamento,
)
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.entrega.filtros import Julgamento, aprovado_para_entrega
from tcc_scraper.entrega.writers import escrever_csv
from tcc_scraper.sentiment.finbert import finbert_en, finbert_pt

logger = logging.getLogger(__name__)

SP = ZoneInfo("America/Sao_Paulo")
HORA_ABERTURA = dt.time(10, 0)
HORA_FECHAMENTO = dt.time(18, 0)

# Tipo da acao na B3 (typeStock de GetListedCashDividends) -> sufixo do ticker.
# UNT/UNIT nao aparecem nos proventos (units vem por composicao), mas ficam no mapa.
MAPA_TYPE_STOCK = {"ON": "3", "PN": "4", "PNA": "5", "PNB": "6", "UNT": "11", "UNIT": "11"}
# corporateAction da B3 -> tipo da entrega. Os demais rotulos ficam de fora (desenho 5.7).
MAPA_TIPO = {"DIVIDENDO": "DIVIDENDO", "JRS CAP PROPRIO": "JCP"}
# Sufixo do ticker -> tipo_acao (desenho 5.3).
TIPO_ACAO_POR_SUFIXO = {
    "3": "ON", "4": "PN", "5": "PN", "6": "PN", "11": "UNIT",
    "32": "BDR", "33": "BDR", "34": "BDR", "35": "BDR",
}
# Eventos em acoes do suplemento que ajustam o valor por acao (desenho 5.7).
ROTULOS_AJUSTE = frozenset({"DESDOBRAMENTO", "BONIFICACAO", "GRUPAMENTO"})
# Sufixo do ISIN de cada classe, usado quando o ISIN nao esta em otherCodes do cadastro.
SUFIXO_ISIN = (("OR", "3"), ("PR", "4"), ("CDAM", "11"))
PREFIXOS_LUCRO = ("lucro/prejuizo consolidado do periodo", "lucro/prejuizo do periodo")
TOLERANCIA_TAXA = 1e-6
_SUFIXO_NUMERICO = re.compile(r"(\d+)$")

CHAVES: dict[str, list[str]] = {
    "noticias": ["id", "ticker"],
    "precos": ["data", "ticker"],
    "cadastro": ["ticker"],
    "mudancas_ticker": ["ticker_antigo"],
    "balancos": ["cnpj", "tipo_documento", "data_inicio_periodo", "data_fim_periodo"],
    "acoes_emitidas": ["cnpj", "data_referencia"],
    "proventos": ["ticker", "tipo", "data_com"],
    "indices": ["data", "indice"],
}
MODELOS = {
    "noticias": Noticia,
    "precos": Preco,
    "cadastro": Cadastro,
    "mudancas_ticker": MudancaTicker,
    "balancos": Balanco,
    "acoes_emitidas": AcoesEmitida,
    "proventos": Provento,
    "indices": Indice,
}


# -- utilitarios ----------------------------------------------------------------------------

def id_noticia(article_id: int) -> str:
    """Codigo da noticia: 'n' + id com zeros a esquerda ate 6 digitos (cresce sem colisao)."""
    return f"n{article_id:06d}"


def _em_sp(instante: dt.datetime) -> dt.datetime:
    """Instante em America/Sao_Paulo. Sem fuso, assume UTC (a camada raw e sempre UTC)."""
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=dt.UTC)
    return instante.astimezone(SP)


def iso_sp(instante: dt.datetime) -> str:
    return _em_sp(instante).isoformat(timespec="seconds")


def momento_pregao(dt_sp: dt.datetime | None, pregoes: set[dt.date]) -> str:
    """Posicao da publicacao em relacao ao pregao. `dt_sp` ja esta em Sao Paulo."""
    if dt_sp is None:
        return "desconhecido"
    if dt_sp.date() not in pregoes:
        return "sem_pregao"
    hora = dt_sp.time()
    if hora < HORA_ABERTURA:
        return "antes_abertura"
    if hora >= HORA_FECHAMENTO:
        return "apos_fechamento"
    return "durante"


def _norm(texto: str | None) -> str:
    """Minusculas, sem acento e com espacos colapsados. Para comparar descricoes da CVM/B3."""
    if not texto:
        return ""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    return " ".join(sem_acento.lower().split())


def _julgamento(linha: dict) -> Julgamento:
    return Julgamento(
        papel=linha["papel"],
        tipo_conteudo=linha["tipo_conteudo"],
        evento=linha["evento"],
        qualidade=float(linha["qualidade"]),
        tickers_adicionais=tuple(linha.get("tickers_adicionais") or ()),
    )


# -- noticias -------------------------------------------------------------------------------

def montar_noticias(
    artigos: list[dict],
    vinculos: list[dict],
    sentimentos: list[dict],
    julgamentos: list[dict],
    pregoes: set[dt.date],
    exigir_juiza: bool,
    modelo_por_lang: dict[str, str],
) -> list[dict]:
    """Uma linha por (artigo, ticker) aprovada para entrega.

    Ticker do par: staging_article_tickers e tickers_adicionais da juiza. Cada par leva o
    julgamento do proprio ticker (versao de prompt mais alta); tickers adicionais herdam o
    julgamento do ticker principal do artigo.
    """
    por_id = {a["id"]: a for a in artigos}

    melhor: dict[tuple[int, str], dict] = {}
    for j in julgamentos:
        chave = (j["article_id"], j["ticker"])
        if chave not in melhor or j["prompt_versao"] > melhor[chave]["prompt_versao"]:
            melhor[chave] = j
    pares: dict[tuple[int, str], Julgamento | None] = {
        chave: _julgamento(j) for chave, j in melhor.items()
    }
    for (article_id, _), julgamento in list(pares.items()):
        if julgamento is None:
            continue
        for extra in julgamento.tickers_adicionais:
            pares.setdefault((article_id, extra), julgamento)
    for v in vinculos:
        pares.setdefault((v["article_id"], v["ticker"]), None)

    sentimento_por_artigo: dict[int, dict[str, dict]] = defaultdict(dict)
    for s in sentimentos:
        sentimento_por_artigo[s["article_id"]][s["model"]] = s

    linhas: list[dict] = []
    for (article_id, ticker), julgamento in pares.items():
        artigo = por_id.get(article_id)
        if artigo is None:
            continue
        origem = artigo["origem"]
        if not aprovado_para_entrega(julgamento, origem, exigir_juiza):
            continue
        publicado = artigo["published_at"]
        com_hora = artigo["published_precision"] == "datetime"
        dt_sp = _em_sp(publicado) if com_hora else None
        prob = sentimento_por_artigo.get(article_id, {}).get(
            modelo_por_lang.get(artigo["lang"]), {}
        )
        linhas.append({
            "id": id_noticia(article_id),
            "ticker": ticker,
            "data_hora_publicacao": iso_sp(publicado) if com_hora else None,
            "data_hora_coleta": iso_sp(artigo["collected_at"]),
            "titulo": artigo["title"],
            "fonte": "CVM" if origem == "cvm_ipe" else (
                artigo["source_name"] or artigo["source_domain"]
            ),
            "link": artigo["resolved_url"] or artigo["url"],
            "prob_positivo": prob.get("prob_pos"),
            "prob_neutro": prob.get("prob_neu"),
            "prob_negativo": prob.get("prob_neg"),
            "data_publicacao": _em_sp(publicado).date(),
            "momento_pregao": momento_pregao(dt_sp, pregoes),
            "origem": origem,
            "papel": julgamento.papel if julgamento else None,
            "tipo_conteudo": julgamento.tipo_conteudo if julgamento else None,
            "evento": julgamento.evento if julgamento else None,
            "qualidade": julgamento.qualidade if julgamento else None,
        })
    linhas.sort(key=lambda r: (r["id"], r["ticker"]))
    return linhas


# -- precos e indices -----------------------------------------------------------------------

def _volume(valor: int | None) -> int | None:
    return int(valor) if valor else None


def montar_precos(raw_prices: list[dict]) -> list[dict]:
    """Acoes (sem .SA), IVVB11 e as series de MACRO_ENTREGA. Indices vao para `indices`."""
    linhas: list[dict] = []
    for r in raw_prices:
        simbolo = r["ticker"]
        if simbolo in INDICES_YAHOO or r["close"] is None:
            continue
        if simbolo in MACRO_ENTREGA:
            # Ajuste so por desdobramento e bonificacao; dolar e Ibov nao tem volume de acao.
            ticker = MACRO_ENTREGA[simbolo]
            ajustado: float | None = r["close"]
            volume = None
        else:
            ticker = simbolo[:-3] if simbolo.endswith(".SA") else simbolo
            ajustado = r["adj_close"]
            volume = _volume(r["volume"])
        linhas.append({
            "data": r["date"],
            "ticker": ticker,
            "abertura": r["open"],
            "maxima": r["high"],
            "minima": r["low"],
            "fechamento": r["close"],
            "fechamento_ajustado": ajustado,
            "volume": volume,
        })
    linhas.sort(key=lambda r: (r["data"], r["ticker"]))
    return linhas


_NOMES_INDICE_BCB = {"cdi": "CDI", "ipca": "IPCA"}


def montar_indices(raw_prices: list[dict], raw_macro: list[dict]) -> list[dict]:
    """CDI e IPCA (BCB, valor como esta) e IDIV/SMLL/IFIX (fechamento do Yahoo)."""
    linhas = [
        {"data": m["date"], "indice": _NOMES_INDICE_BCB[m["series"]], "valor": m["value"]}
        for m in raw_macro
        if m["series"] in _NOMES_INDICE_BCB
    ]
    linhas += [
        {"data": r["date"], "indice": r["ticker"], "valor": r["close"]}
        for r in raw_prices
        if r["ticker"] in INDICES_YAHOO and r["close"] is not None
    ]
    linhas.sort(key=lambda r: (r["data"], r["indice"]))
    return linhas


# -- cadastro e mudancas --------------------------------------------------------------------

def _tipo_acao(ticker: str) -> str | None:
    casamento = _SUFIXO_NUMERICO.search(ticker)
    return TIPO_ACAO_POR_SUFIXO.get(casamento.group(1)) if casamento else None


def montar_cadastro(
    b3: list[dict], cvm: list[dict], universo: Iterable[str]
) -> list[dict]:
    """Uma linha por ticker do universo. CNPJ da B3 com fallback no cadastro da CVM."""
    b3_por_cd = {r["cd_cvm"]: r for r in b3}
    cvm_por_cd = {r["cd_cvm"]: r for r in cvm}
    linhas: list[dict] = []
    for ticker in sorted(set(universo)):
        cd = TICKER_CODE_CVM.get(ticker)
        b = b3_por_cd.get(cd, {})
        c = cvm_por_cd.get(cd, {})
        tipo = _tipo_acao(ticker)
        if tipo is None:
            logger.warning("entrega: cadastro: %s sem tipo de acao reconhecido; pulado", ticker)
            continue
        setor, subsetor, _ = classificacao_b3(b.get("industry_classification"))
        unidade = UNITS.get(ticker)
        linhas.append({
            "ticker": ticker,
            "cnpj": b.get("cnpj") or c.get("cnpj"),
            "empresa": (
                NOMES_EMPRESA.get(ticker)
                or b.get("trading_name")
                or c.get("denom_social")
                or ticker
            ),
            "setor": setor,
            "subsetor": subsetor,
            "tipo_acao": tipo,
            "unit_qtd_on": unidade[0] if unidade else None,
            "unit_qtd_pn": unidade[1] if unidade else None,
        })
    return linhas


def montar_mudancas() -> list[dict]:
    return [
        {"ticker_antigo": antigo, "ticker_novo": novo, "data_mudanca": data}
        for antigo, novo, data in MUDANCAS_TICKER
    ]


# -- balancos e acoes emitidas (CVM) --------------------------------------------------------

def _valor_escalado(linha: dict) -> float:
    """Valores 'MIL' viram reais (x1000, com Decimal para nao somar erro de ponto flutuante)."""
    valor = linha["vl_conta"]
    if linha["escala_moeda"] == "MIL":
        return float(Decimal(str(valor)) * 1000)
    return float(valor)


def _ultimo(ordem: str | None) -> bool:
    return _norm(ordem) == "ultimo"


def _aceita_conta(coluna: str, ds_conta: str | None) -> bool:
    """Decide pela descricao se a linha serve para a coluna (bancos tem plano proprio)."""
    ds = _norm(ds_conta)
    if coluna == "receita_liquida":
        return True
    if coluna == "lucro_bruto":
        return (
            ds.startswith("resultado bruto")
            and "intermediacao" not in ds
            and "operacoes" not in ds
        )
    if coluna == "lucro_liquido":
        return ds.startswith(PREFIXOS_LUCRO)
    if coluna == "patrimonio_liquido":
        return ds.startswith("patrimonio liquido")
    return False


def _valor_da_conta(linhas: list[dict], demonstracao: str, coluna: str) -> float | None:
    """Valor da coluna entre as contas de CONTAS_CVM, com o codigo de maior numero primeiro."""
    codigos = sorted(
        (cd for cd, col in CONTAS_CVM[demonstracao].items() if col == coluna), reverse=True
    )
    for cd in codigos:
        for linha in linhas:
            if linha["cd_conta"] == cd and _aceita_conta(coluna, linha["ds_conta"]):
                return linha["valor"]
    return None


def montar_balancos(balancos: list[dict]) -> list[dict]:
    """Uma linha por (cnpj, tipo, inicio, fim) da DRE, com o PL do mesmo dt_refer copiado."""
    validos = [dict(r, valor=_valor_escalado(r)) for r in balancos if _ultimo(r["ordem_exerc"])]

    maior: dict[tuple, int] = {}
    for r in validos:
        chave = (r["cnpj"], r["tipo_documento"], r["dt_refer"])
        maior[chave] = max(maior.get(chave, r["versao"]), r["versao"])

    por_documento: dict[tuple, list[dict]] = defaultdict(list)
    for r in validos:
        chave = (r["cnpj"], r["tipo_documento"], r["dt_refer"])
        if r["versao"] == maior[chave]:
            por_documento[chave].append(r)

    saida: dict[tuple, dict] = {}
    for (cnpj, tipo, refer), itens in por_documento.items():
        dre = [r for r in itens if r["demonstracao"] == "DRE"]
        bpp = [r for r in itens if r["demonstracao"] == "BPP" and r["dt_fim_exerc"] == refer]
        patrimonio = _valor_da_conta(bpp, "BPP", "patrimonio_liquido")
        grupos: dict[tuple, list[dict]] = defaultdict(list)
        for r in dre:
            grupos[(r["dt_ini_exerc"], r["dt_fim_exerc"])].append(r)
        for (inicio, fim), grupo in grupos.items():
            saida[(cnpj, tipo, inicio, fim)] = {
                "cnpj": cnpj,
                "tipo_documento": tipo,
                "data_inicio_periodo": inicio,
                "data_fim_periodo": fim,
                "data_entrega": grupo[0]["dt_receb"],
                "receita_liquida": _valor_da_conta(grupo, "DRE", "receita_liquida"),
                "lucro_bruto": _valor_da_conta(grupo, "DRE", "lucro_bruto"),
                "lucro_liquido": _valor_da_conta(grupo, "DRE", "lucro_liquido"),
                "patrimonio_liquido": patrimonio,
            }
    return [saida[k] for k in sorted(saida, key=lambda k: (k[0], k[1], k[2], k[3]))]


def montar_acoes_emitidas(raw_cvm_acoes: list[dict]) -> list[dict]:
    """Maior versao por (cnpj, dt_refer); na mesma data, a DFP vence o ITR."""
    melhor: dict[tuple, tuple[tuple, dict]] = {}
    for r in raw_cvm_acoes:
        chave = (r["cnpj"], r["dt_refer"])
        ranking = (r["tipo_documento"] == "DFP", r["versao"])
        if chave not in melhor or ranking > melhor[chave][0]:
            melhor[chave] = (ranking, r)
    return [
        {
            "cnpj": r["cnpj"],
            "data_referencia": r["dt_refer"],
            "qtd_on": r["qt_on"],
            "qtd_pn": r["qt_pn"],
        }
        for _, r in sorted(melhor.values(), key=lambda par: (par[1]["cnpj"], par[1]["dt_refer"]))
    ]


# -- proventos ------------------------------------------------------------------------------

def fator_ajuste(evento: dict) -> float:
    """Multiplicador que leva o valor por acao ANTES do evento para a base pos-evento.

    O `factor` do suplemento da B3 tem semantica por rotulo, conferida na fixture
    b3_supplement_PETR.json (desdobramento 1:2 de 2008 = "100,00000000000"):
    - DESDOBRAMENTO e BONIFICACAO: factor = % de acoes novas (100 = dobra; 33,33 = 1 para 3).
      O valor antigo divide por 1 + factor/100.
    - GRUPAMENTO: factor = razao de acoes (0,01 = 100 para 1). O valor antigo
      multiplica por 1/factor.
    """
    fator = decimal_b3(str(evento["factor"]))
    if fator is None or fator == 0:
        raise ValueError(f"fator de ajuste invalido: {evento.get('factor')!r}")
    rotulo = evento["label"]
    if rotulo in ("DESDOBRAMENTO", "BONIFICACAO"):
        return float(1 / (1 + fator / 100))
    if rotulo == "GRUPAMENTO":
        return float(1 / fator)
    raise ValueError(f"evento de ajuste desconhecido: {rotulo!r}")


def _mapa_isin(cadastro_b3: list[dict]) -> dict[str, str]:
    """ISIN -> codigo do ticker, a partir de otherCodes do GetDetail da B3."""
    mapa: dict[str, str] = {}
    for linha in cadastro_b3:
        for outro in (linha.get("payload") or {}).get("otherCodes") or []:
            if outro.get("isin") and outro.get("code"):
                mapa[outro["isin"]] = outro["code"]
    return mapa


def _ticker_do_evento(evento: dict, ic: str, mapa_isin: dict[str, str]) -> str | None:
    """Ticker (ex.: PETR3) de um evento do suplemento, pelo ISIN."""
    isin = evento.get("isinCode") or ""
    if isin in mapa_isin:
        return mapa_isin[isin]
    base = isin.rstrip("0123456789")
    for fim, sufixo in SUFIXO_ISIN:
        if base.endswith(fim):
            return ic + sufixo
    return None


def _data_pagamento(
    pagamentos: list[dict],
    ticker: str,
    ic: str,
    rotulo: str,
    data_com: dt.date,
    valor: float,
    mapa_isin: dict[str, str],
) -> dt.date | None:
    """paymentDate do suplemento em dinheiro que casa com o provento."""
    for evento in pagamentos:
        if _ticker_do_evento(evento, ic, mapa_isin) != ticker:
            continue
        if data_b3(evento.get("lastDatePrior")) != data_com:
            continue
        if (evento.get("label") or "").strip().upper() != rotulo:
            continue
        taxa = decimal_b3(evento.get("rate"))
        if taxa is None or abs(float(taxa) - valor) > TOLERANCIA_TAXA:
            continue
        return data_b3(evento.get("paymentDate"))
    return None


def _ic_da_unidade(unidade: str, cadastro_b3: list[dict]) -> str:
    """issuingCompany da unit, pelo cadastro B3 (otherCodes); sem achar, pelo prefixo."""
    for linha in cadastro_b3:
        if unidade in (linha.get("other_codes") or []):
            return linha["issuing_company"]
    return unidade[:-2]


def montar_proventos(
    raw_b3_proventos: list[dict],
    raw_b3_suplemento: list[dict],
    cadastro_b3: list[dict],
) -> list[dict]:
    """Proventos por ticker (ON/PN) ajustados por desdobramento, mais as units por composicao.

    Colisao na chave (ticker, tipo, data_com): os valores somam (dois JCPs na mesma data-com).
    """
    mapa = _mapa_isin(cadastro_b3)
    pagamentos_por_ic: dict[str, list[dict]] = defaultdict(list)
    ajustes_por_ic: dict[str, list[dict]] = defaultdict(list)
    for s in raw_b3_suplemento:
        if s["tipo"] == "cash":
            pagamentos_por_ic[s["issuing_company"]].append(s["payload"])
        elif s["tipo"] == "stock" and s["payload"].get("label") in ROTULOS_AJUSTE:
            ajustes_por_ic[s["issuing_company"]].append(s["payload"])

    valores: dict[tuple[str, str, dt.date], dict] = {}
    for r in raw_b3_proventos:
        ic = r["issuing_company"]
        sufixo = MAPA_TYPE_STOCK.get((r["type_stock"] or "").strip().upper())
        if sufixo is None:
            logger.warning("entrega: proventos: typeStock %r de %s desconhecido; pulado",
                           r["type_stock"], ic)
            continue
        rotulo = (r["corporate_action"] or "").strip().upper()
        tipo = MAPA_TIPO.get(rotulo)
        if tipo is None:
            logger.warning("entrega: proventos: corporateAction %r fora da entrega; pulado",
                           r["corporate_action"])
            continue
        data_com = r["last_date_prior_ex"]
        valor_bruto = r["value_cash"]
        if data_com is None or valor_bruto is None:
            continue
        ticker = ic + sufixo
        quoted = decimal_b3(r["quoted_per_shares"]) or Decimal(1)
        fator = 1.0
        for evento in ajustes_por_ic.get(ic, []):
            if _ticker_do_evento(evento, ic, mapa) != ticker:
                continue
            data_evento = data_b3(evento.get("lastDatePrior"))
            if data_evento is None or data_evento <= data_com:
                continue
            fator *= fator_ajuste(evento)
        valor = valor_bruto / float(quoted) * fator
        pagamento = _data_pagamento(
            pagamentos_por_ic.get(ic, []), ticker, ic, rotulo, data_com, valor_bruto, mapa
        )
        chave = (ticker, tipo, data_com)
        if chave in valores:
            valores[chave]["valor_por_acao"] += valor
            valores[chave]["data_pagamento"] = valores[chave]["data_pagamento"] or pagamento
        else:
            valores[chave] = {
                "ticker": ticker,
                "tipo": tipo,
                "data_com": data_com,
                "valor_por_acao": valor,
                "data_pagamento": pagamento,
            }

    for unidade, (qtd_on, qtd_pn) in UNITS.items():
        ic = _ic_da_unidade(unidade, cadastro_b3)
        t_on, t_pn = ic + "3", ic + "4"
        datas = {(tipo, dc) for (t, tipo, dc) in valores if t in (t_on, t_pn)}
        for tipo, dc in sorted(datas):
            on = valores.get((t_on, tipo, dc))
            pn = valores.get((t_pn, tipo, dc))
            valores[(unidade, tipo, dc)] = {
                "ticker": unidade,
                "tipo": tipo,
                "data_com": dc,
                "valor_por_acao": (
                    qtd_on * (on["valor_por_acao"] if on else 0.0)
                    + qtd_pn * (pn["valor_por_acao"] if pn else 0.0)
                ),
                "data_pagamento": (on or pn)["data_pagamento"],
            }

    return sorted(valores.values(), key=lambda r: (r["ticker"], r["data_com"], r["tipo"]))


# -- montagem e gravacao --------------------------------------------------------------------



def modelos_finbert() -> dict[str, str]:
    """Modelo do FinBERT por idioma, como gravado em staging_article_sentiment.model."""
    return {"pt": finbert_pt().model_name, "en": finbert_en().model_name}


def _consulta(stmt: Any) -> list[dict]:
    with session_scope() as session:
        return [dict(linha) for linha in session.execute(stmt).mappings()]


class _Fontes:
    """Le cada fonte uma vez, no primeiro uso. Consultas simples; a montagem fica nas funcoes."""

    def __init__(self, inicio_noticias: dt.date, somente_ids: set[int] | None = None) -> None:
        self._limite = dt.datetime.combine(inicio_noticias, dt.time(0), tzinfo=SP).astimezone(
            dt.UTC
        )
        self._somente_ids = somente_ids

    @cached_property
    def artigos(self) -> list[dict]:
        consulta = select(
            Article.id, Article.title, Article.source_name, Article.source_domain,
            Article.resolved_url, Article.url, Article.published_at,
            Article.published_precision, Article.lang, Article.collected_at, Article.origem,
        ).where(Article.published_at >= self._limite)
        if self._somente_ids is not None:
            consulta = consulta.where(Article.id.in_(sorted(self._somente_ids)))
        return _consulta(consulta)

    @cached_property
    def vinculos(self) -> list[dict]:
        return _consulta(select(ArticleTicker.article_id, ArticleTicker.ticker))

    @cached_property
    def sentimentos(self) -> list[dict]:
        return _consulta(
            select(
                ArticleSentiment.article_id, ArticleSentiment.model,
                ArticleSentiment.prob_pos, ArticleSentiment.prob_neu, ArticleSentiment.prob_neg,
            )
        )

    @cached_property
    def julgamentos(self) -> list[dict]:
        return _consulta(
            select(
                StagingNoticiaJulgamento.article_id, StagingNoticiaJulgamento.ticker,
                StagingNoticiaJulgamento.prompt_versao, StagingNoticiaJulgamento.papel,
                StagingNoticiaJulgamento.tipo_conteudo, StagingNoticiaJulgamento.evento,
                StagingNoticiaJulgamento.tickers_adicionais,
                StagingNoticiaJulgamento.qualidade,
            )
        )

    @cached_property
    def precos_raw(self) -> list[dict]:
        return _consulta(
            select(
                Price.ticker, Price.date, Price.open, Price.high, Price.low, Price.close,
                Price.adj_close, Price.volume,
            )
        )

    @cached_property
    def macro(self) -> list[dict]:
        return _consulta(select(Macro.series, Macro.date, Macro.value))

    @cached_property
    def pregoes(self) -> set[dt.date]:
        return {r["date"] for r in self.precos_raw if r["ticker"] == BENCHMARK_TICKER}

    @cached_property
    def b3_cadastro(self) -> list[dict]:
        return _consulta(
            select(
                RawB3Cadastro.cd_cvm, RawB3Cadastro.cnpj, RawB3Cadastro.trading_name,
                RawB3Cadastro.issuing_company, RawB3Cadastro.industry_classification,
                RawB3Cadastro.other_codes, RawB3Cadastro.payload,
            )
        )

    @cached_property
    def cvm_cadastro(self) -> list[dict]:
        return _consulta(
            select(RawCvmCadastro.cd_cvm, RawCvmCadastro.cnpj, RawCvmCadastro.denom_social)
        )

    @cached_property
    def balancos(self) -> list[dict]:
        return _consulta(
            select(
                RawCvmBalanco.cnpj, RawCvmBalanco.tipo_documento, RawCvmBalanco.dt_refer,
                RawCvmBalanco.versao, RawCvmBalanco.demonstracao, RawCvmBalanco.cd_conta,
                RawCvmBalanco.dt_ini_exerc, RawCvmBalanco.dt_fim_exerc, RawCvmBalanco.ds_conta,
                RawCvmBalanco.ordem_exerc, RawCvmBalanco.escala_moeda, RawCvmBalanco.vl_conta,
                RawCvmBalanco.dt_receb,
            )
        )

    @cached_property
    def acoes(self) -> list[dict]:
        return _consulta(
            select(
                RawCvmAcoes.cnpj, RawCvmAcoes.tipo_documento, RawCvmAcoes.dt_refer,
                RawCvmAcoes.versao, RawCvmAcoes.qt_on, RawCvmAcoes.qt_pn,
            )
        )

    @cached_property
    def proventos(self) -> list[dict]:
        return _consulta(
            select(
                RawB3Proventos.issuing_company, RawB3Proventos.type_stock,
                RawB3Proventos.corporate_action, RawB3Proventos.value_cash,
                RawB3Proventos.last_date_prior_ex, RawB3Proventos.quoted_per_shares,
            )
        )

    @cached_property
    def suplemento(self) -> list[dict]:
        return _consulta(
            select(RawB3Suplemento.issuing_company, RawB3Suplemento.tipo, RawB3Suplemento.payload)
        )


def _montar(tabela: str, f: _Fontes, exigir_juiza: bool, modelos: dict[str, str]) -> list[dict]:
    if tabela == "noticias":
        return montar_noticias(
            f.artigos, f.vinculos, f.sentimentos, f.julgamentos, f.pregoes, exigir_juiza, modelos
        )
    if tabela == "precos":
        return montar_precos(f.precos_raw)
    if tabela == "cadastro":
        return montar_cadastro(f.b3_cadastro, f.cvm_cadastro, universo_coleta())
    if tabela == "mudancas_ticker":
        return montar_mudancas()
    if tabela == "balancos":
        return montar_balancos(f.balancos)
    if tabela == "acoes_emitidas":
        return montar_acoes_emitidas(f.acoes)
    if tabela == "proventos":
        return montar_proventos(f.proventos, f.suplemento, f.b3_cadastro)
    if tabela == "indices":
        return montar_indices(f.precos_raw, f.macro)
    raise ValueError(f"tabela de entrega desconhecida: {tabela!r}")


# Tamanho dos lotes de SELECT/DELETE por id: mantem cada IN abaixo do limite de parametros.
LOTE_DELETE = 500


def _em_lotes(itens: list) -> list[list]:
    return [itens[i:i + LOTE_DELETE] for i in range(0, len(itens), LOTE_DELETE)]


def _apagar_reprovadas(linhas: list[dict], artigos_janela: set[int]) -> None:
    """Tira de `noticias` o que a janela deixou de aprovar (desenho 5.1).

    O upsert so grava: uma linha entregue antes de ligar o filtro, ou um artigo rejulgado como
    panorama, ficaria para sempre. Apaga (a) os artigos da janela sem nenhuma linha aprovada,
    por id, e (b) os pares (id, ticker) da janela que nao estao entre as aprovadas.
    """
    aprovadas = {(r["id"], r["ticker"]) for r in linhas}
    com_aprovada = {id_ for id_, _ in aprovadas}
    codigos = {id_noticia(a) for a in artigos_janela}
    with session_scope() as session:
        for lote in _em_lotes(sorted(codigos - com_aprovada)):
            session.execute(delete(Noticia.__table__).where(Noticia.id.in_(lote)))
        for lote in _em_lotes(sorted(codigos & com_aprovada)):
            existentes = session.execute(
                select(Noticia.id, Noticia.ticker).where(Noticia.id.in_(lote))
            ).all()
            obsoletas = [(id_, ticker) for id_, ticker in existentes
                         if (id_, ticker) not in aprovadas]
            if obsoletas:
                session.execute(delete(Noticia.__table__).where(
                    tuple_(Noticia.id, Noticia.ticker).in_(obsoletas)
                ))


def _gravar(
    tabela: str, linhas: list[dict], artigos_janela: set[int] | None = None
) -> None:
    """Upsert pela chave da spec. Sem linhas, nao faz upsert.

    Em `noticias`, com `artigos_janela` (ids dos artigos lidos), tambem apaga o que a janela
    deixou de aprovar: ver `_apagar_reprovadas`.
    """
    chave = CHAVES[tabela]
    if linhas:
        with session_scope() as session:
            bulk_upsert(
                session,
                MODELOS[tabela].__table__,
                linhas,
                index_elements=chave,
                update_columns=[c for c in COLUNAS[tabela] if c not in chave],
            )
    if tabela == "noticias" and artigos_janela is not None:
        _apagar_reprovadas(linhas, artigos_janela)


def build_entrega(
    *,
    tabelas: list[str] | None = None,
    csv: bool = False,
    observador: Any | None = None,
    somente_ids: set[int] | None = None,
) -> dict[str, int]:
    """Monta e grava as tabelas da entrega. Devolve {tabela: n_linhas} das que deram certo.

    Erro numa tabela e registrado (log e observador) e nao impede as outras. Com `csv=True`,
    cada tabela tambem vai para `<entrega_csv_dir>/<tabela>.csv`. `somente_ids` restringe as
    noticias (artigos lidos, gravados e apagados) a esses ids de artigo; e para teste.
    """
    settings = get_settings()
    alvo = list(tabelas) if tabelas else list(COLUNAS)
    desconhecidas = [t for t in alvo if t not in COLUNAS]
    if desconhecidas:
        raise ValueError(f"tabelas de entrega desconhecidas: {desconhecidas}")

    fontes = _Fontes(settings.inicio_noticias, somente_ids)
    modelos = modelos_finbert()
    if observador is not None:
        observador.plano(len(alvo))

    resultado: dict[str, int] = {}
    for tabela in alvo:
        try:
            linhas = _montar(tabela, fontes, settings.entrega_filtro_juiza, modelos)
            janela = {a["id"] for a in fontes.artigos} if tabela == "noticias" else None
            _gravar(tabela, linhas, artigos_janela=janela)
            if csv:
                escrever_csv(tabela, linhas, COLUNAS[tabela], settings.entrega_csv_dir)
        except Exception as exc:
            logger.exception("entrega: %s falhou", tabela)
            if observador is not None:
                observador.unidade(
                    tabela, ok=False, itens=0, saturada=False, erro=str(exc)[:200]
                )
            continue
        resultado[tabela] = len(linhas)
        logger.info("entrega: %s: %d linhas", tabela, len(linhas))
        if observador is not None:
            observador.unidade(tabela, ok=True, itens=len(linhas), saturada=False, erro=None)
    return resultado
