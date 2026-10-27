"""Montagem das 9 tabelas da entrega a partir das fontes raw/staging.

Cada `montar_*` e pura: recebe listas de dicts (linhas das fontes) e devolve `list[dict]` com
as chaves de `COLUNAS`. `build_entrega` carrega as fontes, monta cada tabela, grava por upsert
e, se pedido, exporta CSV. Regras de mapeamento: desenho 5.1 a 5.8.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, tuple_

from tcc_scraper.collectors.b3_base import data_b3, decimal_b3
from tcc_scraper.collectors.b3_cadastro import classificacao_b3
from tcc_scraper.config import (
    BENCHMARK_TICKER,
    CD_CVM_BANCOS_SEGURADORAS,
    CONTAS_CVM,
    INDICES_YAHOO,
    MACRO_ENTREGA,
    MUDANCAS_TICKER,
    NOMES_EMPRESA,
    TICKER_CODE_CVM,
    UNITS,
    conta_ajuste_dfc,
    get_settings,
    universo_coleta,
)
from tcc_scraper.db.models import Article, ArticleSentiment, ArticleTicker, Macro, Price
from tcc_scraper.db.models_entrega import (
    COLUNAS,
    AcoesEmitida,
    Balanco,
    Cadastro,
    ComposicaoIbov,
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
    StagingHistoria,
    StagingNoticiaJulgamento,
)
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.entrega.composicao_ibov import (
    PASTA_CARTEIRAS,
    carregar_carteiras_csv,
    linha_do_tempo_ibov,
    montar_vigencias,
)
from tcc_scraper.entrega.filtros import Julgamento, aprovado_para_entrega
from tcc_scraper.entrega.writers import escrever_csv
from tcc_scraper.llm.juiza_prompt import VERSAO as VERSAO_JUIZA
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
# Em branco para bancos e seguradoras (spec): contas operacionais, de divida e de caixa.
COLUNAS_SEM_FINANCEIRAS = (
    "lucro_bruto", "ebit", "depreciacao_amortizacao", "emprestimos_curto_prazo",
    "emprestimos_longo_prazo", "caixa", "aplicacoes_financeiras",
)
# D&A na DFC indireta: termos que entram e falsos positivos reais da CVM que saem
# (custo de transacao, captacao, juros, "custo amortizado" de ativo financeiro etc.).
_DA_INCLUI = ("deprecia", "amortiz", "exaust", "deplec")
_DA_EXCLUI = (
    "custo de transacao", "custos de transacao", "captacao", "emprestimo", "debenture",
    "juros", "encargos", "amortizado", "despesas antecipadas", "estoque",
    "custo de aquisicao",
)
TOLERANCIA_TAXA = 1e-6
# Escala das acoes na CVM: janela do giro (pregoes) e minimo de pregoes com volume para ter giro.
JANELA_GIRO_PREGOES = 60
MIN_PREGOES_GIRO = 20
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
    "composicao_ibov": ["ticker", "vigencia_inicio"],
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
    "composicao_ibov": ComposicaoIbov,
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


def _anula_abertura_repetida(linhas: list[dict]) -> list[dict]:
    """Abertura igual ao fechamento do pregao anterior vira None (defeito do Yahoo no IBOV).

    Recebe as linhas de um unico ticker e devolve copias ordenadas por data; as originais nao mudam.
    """
    saida: list[dict] = []
    fechamento_anterior: float | None = None
    for linha in sorted(linhas, key=lambda r: r["data"]):
        abertura = linha["abertura"]
        repetida = (
            abertura is not None
            and fechamento_anterior is not None
            and round(abertura, 2) == round(fechamento_anterior, 2)
        )
        saida.append({**linha, "abertura": None if repetida else abertura})
        fechamento_anterior = linha["fechamento"]
    return saida


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
    # O Yahoo repete o fechamento anterior como abertura do IBOV em ~32% dos dias:
    # esse valor fica vazio.
    ibov = _anula_abertura_repetida([r for r in linhas if r["ticker"] == "IBOV"])
    linhas = [r for r in linhas if r["ticker"] != "IBOV"] + ibov
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


def montar_composicao(fechamentos: dict[tuple[str, dt.date], dt.date | None]) -> list[dict]:
    """Vigencias do IBOV: carteiras oficiais, rebalanceamentos e trocas (composicao_ibov.py).

    `fechamentos`: (ticker, vigencia_inicio) -> vigencia_fim gravada pelo coletor
    b3_carteira_ibov. Quando o coletor fechou uma vigencia que a reconstrucao deixa aberta,
    vale o fechamento do banco, porque veio da carteira publicada pela B3.
    """
    linhas = montar_vigencias(linha_do_tempo_ibov(carregar_carteiras_csv(PASTA_CARTEIRAS)))
    for linha in linhas:
        fim = fechamentos.get((linha["ticker"], linha["vigencia_inicio"]))
        if fim is not None:
            linha["vigencia_fim"] = fim
    return linhas


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
    if coluna == "ebit":
        return ds.startswith("resultado antes do resultado financeiro")
    if coluna == "caixa":
        return ds.startswith("caixa e equivalentes")
    if coluna == "aplicacoes_financeiras":
        return ds.startswith("aplicacoes financeiras")
    if coluna in ("emprestimos_curto_prazo", "emprestimos_longo_prazo"):
        return ds.startswith(("emprestimos e financiamentos", "debentures"))
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


def _soma(valores: list[float]) -> float | None:
    """Soma em Decimal (sem erro de ponto flutuante); None se nao houver valor."""
    if not valores:
        return None
    return float(sum(Decimal(str(v)) for v in valores))


def _soma_das_contas(linhas: list[dict], demonstracao: str, coluna: str) -> float | None:
    """Soma todas as contas de CONTAS_CVM da coluna (ex.: emprestimos + debentures)."""
    codigos = {cd for cd, col in CONTAS_CVM[demonstracao].items() if col == coluna}
    return _soma([
        linha["valor"] for linha in linhas
        if linha["cd_conta"] in codigos and _aceita_conta(coluna, linha["ds_conta"])
    ])


# Conta-pai e arrendamento (.03) de cada prazo: usados quando as subcontas vem zeradas.
_PAI_EMPRESTIMOS: dict[str, tuple[str, str]] = {
    "emprestimos_curto_prazo": ("2.01.04", "2.01.04.03"),
    "emprestimos_longo_prazo": ("2.02.01", "2.02.01.03"),
}


def _emprestimos(bpp: list[dict], coluna: str) -> float | None:
    """Emprestimos de curto ou longo prazo. As subcontas (.01 e .02) valem quando somam > 0.
    Senao, a conta-pai menos o arrendamento (.03): CSN, Sabesp, Rede D'Or, Magalu e CMIN
    lancam a divida so no pai. O arrendamento fica fora, como pede a spec. Limitacao da
    fonte: se a empresa preenche so o pai e deixa o .03 em 0, o valor pode incluir
    arrendamento, que a CVM nao separa."""
    soma = _soma_das_contas(bpp, "BPP", coluna)
    if soma is not None and soma > 0:
        return soma
    pai, arrendamento = _PAI_EMPRESTIMOS[coluna]
    total = _soma([
        linha["valor"] for linha in bpp
        if linha["cd_conta"] == pai and _aceita_conta(coluna, linha["ds_conta"])
    ])
    if total is None or total == 0:
        return soma
    arrendado = _soma([linha["valor"] for linha in bpp if linha["cd_conta"] == arrendamento])
    return float(Decimal(str(total)) - Decimal(str(arrendado or 0.0)))


def _e_depreciacao(ds_conta: str | None) -> bool:
    ds = _norm(ds_conta)
    return any(t in ds for t in _DA_INCLUI) and not any(t in ds for t in _DA_EXCLUI)


def _depreciacao_amortizacao(linhas_dfc: list[dict]) -> float | None:
    """D&A da DFC indireta: soma das linhas 6.01.01.NN de D&A, sempre positiva."""
    total = _soma([
        linha["valor"] for linha in linhas_dfc
        if conta_ajuste_dfc(linha["cd_conta"]) and _e_depreciacao(linha["ds_conta"])
    ])
    return abs(total) if total is not None else None


def _da_do_periodo(
    da_acumulada: dict[tuple, float | None],
    cnpj: str, inicio: dt.date, fim: dt.date, inicios_dfc: set[dt.date],
) -> float | None:
    """D&A do periodo da linha. Igual ao da DFC quando o periodo bate; no trimestre do
    ITR, acumulado ate o fim menos acumulado ate a vespera do inicio (D5). None se faltar
    um dos dois ou se a diferenca sair negativa (reapresentacao)."""
    if (cnpj, inicio, fim) in da_acumulada:
        return da_acumulada[(cnpj, inicio, fim)]
    for inicio_ano in inicios_dfc:
        if inicio_ano >= inicio:
            continue
        ate_fim = da_acumulada.get((cnpj, inicio_ano, fim))
        ate_vespera = da_acumulada.get((cnpj, inicio_ano, inicio - dt.timedelta(days=1)))
        if ate_fim is None or ate_vespera is None:
            continue
        diferenca = float(Decimal(str(ate_fim)) - Decimal(str(ate_vespera)))
        return diferenca if diferenca >= 0 else None
    return None


def _documento(r: dict) -> tuple:
    return (r["cnpj"], r["tipo_documento"], r["dt_refer"], r["versao"])


def _magnitude_do_documento(linhas: list[dict], escala: str) -> float | None:
    """Maior |valor| das linhas ULTIMO, lido na escala dada. Zeros e vazios nao contam."""
    valores = [
        abs(_valor_escalado({**r, "escala_moeda": escala}))
        for r in linhas if _ultimo(r["ordem_exerc"]) and r["vl_conta"]
    ]
    return max(valores, default=None)


def _corrigir_escala_balancos(balancos: list[dict]) -> list[dict]:
    """Escala que a companhia declarou errada no documento. Devolve copias; o raw nao muda.

    Escala do documento = a mais frequente nas suas linhas. Se difere da majoritaria do CNPJ e
    a magnitude do documento fica 100x abaixo (UNIDADE em CNPJ MIL) ou acima (MIL em CNPJ
    UNIDADE) da mediana dos documentos na majoritaria, o documento usa a majoritaria.
    """
    por_documento: dict[tuple, list[dict]] = defaultdict(list)
    for r in balancos:
        por_documento[_documento(r)].append(r)
    escala_doc = {
        chave: Counter(r["escala_moeda"] for r in linhas).most_common(1)[0][0]
        for chave, linhas in por_documento.items()
    }
    docs_do_cnpj: dict[str, list[tuple]] = defaultdict(list)
    for chave in por_documento:
        docs_do_cnpj[chave[0]].append(chave)

    corrigidas: dict[tuple, str] = {}
    for cnpj, chaves in docs_do_cnpj.items():
        majoritaria = Counter(escala_doc[k] for k in chaves).most_common(1)[0][0]
        magnitudes = {k: _magnitude_do_documento(por_documento[k], escala_doc[k]) for k in chaves}
        referencia = [m for k, m in magnitudes.items() if escala_doc[k] == majoritaria and m]
        if not referencia:
            continue
        mediana = statistics.median(referencia)
        for k in chaves:
            declarada, magnitude = escala_doc[k], magnitudes[k]
            if declarada == majoritaria or not magnitude:
                continue
            razao = magnitude / mediana
            if declarada == "UNIDADE" and majoritaria == "MIL" and razao < 1 / 100:
                nova = "MIL"
            elif declarada == "MIL" and majoritaria == "UNIDADE" and razao > 100:
                nova = "UNIDADE"
            else:
                continue
            corrigidas[k] = nova
            logger.warning(
                "entrega: balancos: escala declarada corrigida (cnpj %s, %s, %s): %s -> %s",
                cnpj, k[1], k[2], declarada, nova,
            )
    return [
        {**r, "escala_moeda": corrigidas.get(_documento(r), r["escala_moeda"])} for r in balancos
    ]


def montar_balancos(balancos: list[dict]) -> list[dict]:
    """Uma linha por (cnpj, tipo, inicio, fim) da DRE. PL, caixa, aplicacoes e emprestimos
    do mesmo dt_refer valem para todas as linhas do documento; a D&A segue o periodo.
    Bancos e seguradoras (CD_CVM_BANCOS_SEGURADORAS) saem em branco nas colunas da spec."""
    balancos = _corrigir_escala_balancos(balancos)
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

    # D&A acumulada no ano por (cnpj, inicio, fim), de cada documento. Serve para derivar
    # o trimestre do ITR: a CVM so publica a DFC acumulada (D5).
    da_acumulada: dict[tuple, float | None] = {}
    for (cnpj, _tipo, refer), itens in por_documento.items():
        dfc_doc: dict[tuple, list[dict]] = defaultdict(list)
        for r in itens:
            if r["demonstracao"] == "DFC":
                dfc_doc[(r["dt_ini_exerc"], r["dt_fim_exerc"])].append(r)
        for (inicio, fim), linhas in dfc_doc.items():
            chave = (cnpj, inicio, fim)
            # O documento cuja data de referencia e o fim do periodo e o dono do numero.
            if chave not in da_acumulada or fim == refer:
                da_acumulada[chave] = _depreciacao_amortizacao(linhas)

    saida: dict[tuple, dict] = {}
    for (cnpj, tipo, refer), itens in por_documento.items():
        dre = [r for r in itens if r["demonstracao"] == "DRE"]
        bpa = [r for r in itens if r["demonstracao"] == "BPA" and r["dt_fim_exerc"] == refer]
        bpp = [r for r in itens if r["demonstracao"] == "BPP" and r["dt_fim_exerc"] == refer]
        inicios_dfc = {r["dt_ini_exerc"] for r in itens if r["demonstracao"] == "DFC"}
        financeira = itens[0].get("cd_cvm") in CD_CVM_BANCOS_SEGURADORAS
        patrimonio = _valor_da_conta(bpp, "BPP", "patrimonio_liquido")
        # Posicoes no fim do periodo: copiadas para todas as linhas da DRE (como o PL).
        posicoes = {
            "emprestimos_curto_prazo": _emprestimos(bpp, "emprestimos_curto_prazo"),
            "emprestimos_longo_prazo": _emprestimos(bpp, "emprestimos_longo_prazo"),
            "caixa": _valor_da_conta(bpa, "BPA", "caixa"),
            "aplicacoes_financeiras": _valor_da_conta(bpa, "BPA", "aplicacoes_financeiras"),
        }
        grupos: dict[tuple, list[dict]] = defaultdict(list)
        for r in dre:
            grupos[(r["dt_ini_exerc"], r["dt_fim_exerc"])].append(r)
        for (inicio, fim), grupo in grupos.items():
            linha = {
                "cnpj": cnpj,
                "tipo_documento": tipo,
                "data_inicio_periodo": inicio,
                "data_fim_periodo": fim,
                "data_entrega": grupo[0]["dt_receb"],
                "receita_liquida": _valor_da_conta(grupo, "DRE", "receita_liquida"),
                "lucro_bruto": _valor_da_conta(grupo, "DRE", "lucro_bruto"),
                "lucro_liquido": _valor_da_conta(grupo, "DRE", "lucro_liquido"),
                "patrimonio_liquido": patrimonio,
                "ebit": _valor_da_conta(grupo, "DRE", "ebit"),
                "depreciacao_amortizacao": _da_do_periodo(
                    da_acumulada, cnpj, inicio, fim, inicios_dfc
                ),
                **posicoes,
            }
            if financeira:
                for coluna in COLUNAS_SEM_FINANCEIRAS:
                    linha[coluna] = None
            saida[(cnpj, tipo, inicio, fim)] = linha
    return [saida[k] for k in sorted(saida, key=lambda k: (k[0], k[1], k[2], k[3]))]


def volumes_por_cnpj(
    cadastro: list[dict], precos_raw: list[dict]
) -> dict[str, dict[dt.date, float]]:
    """Volume diario somado por CNPJ (ON, PN e unit). Volume None ou zero nao conta."""
    cnpj_por_ticker = {r["ticker"]: r["cnpj"] for r in cadastro if r["cnpj"]}
    volumes: dict[str, dict[dt.date, float]] = {}
    for r in precos_raw:
        simbolo = r["ticker"]
        ticker = simbolo[:-3] if simbolo.endswith(".SA") else simbolo
        cnpj = cnpj_por_ticker.get(ticker)
        volume = _volume(r["volume"])
        if cnpj is None or volume is None:
            continue
        dia = volumes.setdefault(cnpj, {})
        dia[r["date"]] = dia.get(r["date"], 0.0) + volume
    return volumes


def _total_acoes(linha: dict) -> int:
    return (linha["qt_on"] or 0) + (linha["qt_pn"] or 0)


def _troca_de_escala(razao: float) -> bool:
    """Razao entre totais consecutivos de ~1000x, para cima ou para baixo."""
    return 500 <= razao <= 2000 or 1 / 2000 <= razao <= 1 / 500


def _segmentar(serie: list[dict]) -> list[list[dict]]:
    """Abre segmento novo quando o total salta ~1000x em relacao ao ultimo total nao zero."""
    segmentos: list[list[dict]] = []
    anterior = 0
    for linha in serie:
        total = _total_acoes(linha)
        nova = not segmentos or bool(total and anterior and _troca_de_escala(total / anterior))
        if nova:
            segmentos.append([])
        segmentos[-1].append(linha)
        if total:
            anterior = total
    return segmentos


def _giro(linha: dict, volume_cnpj: dict[dt.date, float]) -> float | None:
    """Mediana do volume dos ultimos 60 pregoes ate dt_refer, dividida pelo total de acoes.
    Pregao = data com volume do CNPJ. Menos de 20 pregoes, ou total zero, nao tem giro."""
    total = _total_acoes(linha)
    datas = sorted(d for d in volume_cnpj if d <= linha["dt_refer"])[-JANELA_GIRO_PREGOES:]
    if not total or len(datas) < MIN_PREGOES_GIRO:
        return None
    mediana = statistics.median(volume_cnpj[d] for d in datas)
    return mediana / total if mediana > 0 else None


def _em_milhares(segmento: list[dict], volume_cnpj: dict[dt.date, float]) -> bool | None:
    """True se o giro mediano diz milhares (giro acima de 10% ao dia, impossivel em unidades).
    None se nenhuma linha do segmento tem giro."""
    logs = [math.log10(g) for g in (_giro(r, volume_cnpj) for r in segmento) if g is not None]
    return statistics.median(logs) > -1 if logs else None


def _herdar_escala(decisoes: list[bool | None]) -> list[bool]:
    """Segmento sem giro herda pela cadeia: toda fronteira e uma troca de 1000x, entao a escala
    alterna a partir do segmento decidido pelo giro mais proximo (ex.: trimestres de antes da
    listagem, sem volume). Sem nenhum segmento decidido, fica em unidades, como esta."""
    decididos = [i for i, d in enumerate(decisoes) if d is not None]
    saida: list[bool] = []
    for i, decisao in enumerate(decisoes):
        if decisao is None and decididos:
            j = min(decididos, key=lambda k: abs(k - i))
            decisao = decisoes[j] != (abs(i - j) % 2 == 1)
        saida.append(bool(decisao))
    return saida


def _alertar_isolados(cnpj: str, serie: list[dict]) -> None:
    """Valor que sai de dois vizinhos que concordam (menos de 2x entre si) sem ser troca de
    1000x. Nao corrige, so avisa."""
    for i in range(1, len(serie) - 1):
        ant, atual, prox = (_total_acoes(serie[k]) for k in (i - 1, i, i + 1))
        if not (ant and atual and prox):
            continue
        if _troca_de_escala(atual / ant) or _troca_de_escala(atual / prox):
            continue
        baixo, alto = min(ant, prox), max(ant, prox)
        if alto / baixo < 2 and not baixo / 2 <= atual <= alto * 2:
            logger.warning(
                "entrega: acoes_emitidas: %s em %s com valor isolado %s (vizinhos concordam); "
                "nao corrigido",
                cnpj, serie[i]["dt_refer"], atual,
            )


def _x1000(valor: int | None) -> int | None:
    return None if valor is None else int(valor) * 1000


def _corrigir_escala_acoes(
    linhas: list[dict], volumes: dict[str, dict[dt.date, float]]
) -> list[dict]:
    """Segmentos em milhares viram unidades (x1000). Recebe as linhas escolhidas, em ordem de
    (cnpj, dt_refer), e devolve copias; as originais nao mudam."""
    por_cnpj: dict[str, list[dict]] = defaultdict(list)
    for r in linhas:
        por_cnpj[r["cnpj"]].append(dict(r))
    multiplicadas = 0
    for cnpj, serie in por_cnpj.items():
        _alertar_isolados(cnpj, serie)
        segmentos = _segmentar(serie)
        decisoes = _herdar_escala(
            [_em_milhares(seg, volumes.get(cnpj, {})) for seg in segmentos]
        )
        for seg, milhares in zip(segmentos, decisoes, strict=True):
            if not milhares:
                continue
            multiplicadas += len(seg)
            for r in seg:
                r["qt_on"] = _x1000(r["qt_on"])
                r["qt_pn"] = _x1000(r["qt_pn"])
    if multiplicadas:
        logger.info(
            "entrega: acoes_emitidas: %d linhas em milhares multiplicadas por 1000", multiplicadas
        )
    return [r for serie in por_cnpj.values() for r in serie]


def montar_acoes_emitidas(
    raw_cvm_acoes: list[dict], volumes: dict[str, dict[dt.date, float]] | None = None
) -> list[dict]:
    """Maior versao por (cnpj, dt_refer); na mesma data, a DFP vence o ITR.

    Com `volumes` (ver `volumes_por_cnpj`), corrige a escala: a CVM mistura unidades e
    milhares. Sem ele, sai como a CVM publica.
    """
    melhor: dict[tuple, tuple[tuple, dict]] = {}
    for r in raw_cvm_acoes:
        chave = (r["cnpj"], r["dt_refer"])
        ranking = (r["tipo_documento"] == "DFP", r["versao"])
        if chave not in melhor or ranking > melhor[chave][0]:
            melhor[chave] = (ranking, r)
    escolhidas = [
        r for _, r in sorted(melhor.values(), key=lambda par: (par[1]["cnpj"], par[1]["dt_refer"]))
    ]
    if volumes is not None:
        escolhidas = _corrigir_escala_acoes(escolhidas, volumes)
    return [
        {
            "cnpj": r["cnpj"],
            "data_referencia": r["dt_refer"],
            "qtd_on": r["qt_on"],
            "qtd_pn": r["qt_pn"],
        }
        for r in escolhidas
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
        # Republicacao entre veiculos conta como 1 noticia: sai o que nao e representante da historia.
        duplicados = select(StagingHistoria.article_id).where(
            StagingHistoria.article_id != StagingHistoria.historia_article_id
        )
        consulta = consulta.where(Article.id.not_in(duplicados))
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
                RawCvmBalanco.dt_receb, RawCvmBalanco.cd_cvm,
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

    @cached_property
    def composicao_fechamentos(self) -> dict[tuple[str, dt.date], dt.date | None]:
        linhas = _consulta(
            select(
                ComposicaoIbov.ticker, ComposicaoIbov.vigencia_inicio, ComposicaoIbov.vigencia_fim
            ).where(ComposicaoIbov.vigencia_fim.is_not(None))
        )
        return {(r["ticker"], r["vigencia_inicio"]): r["vigencia_fim"] for r in linhas}


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
        cadastro = montar_cadastro(f.b3_cadastro, f.cvm_cadastro, universo_coleta())
        return montar_acoes_emitidas(f.acoes, volumes_por_cnpj(cadastro, f.precos_raw))
    if tabela == "proventos":
        return montar_proventos(f.proventos, f.suplemento, f.b3_cadastro)
    if tabela == "indices":
        return montar_indices(f.precos_raw, f.macro)
    if tabela == "composicao_ibov":
        return montar_composicao(f.composicao_fechamentos)
    raise ValueError(f"tabela de entrega desconhecida: {tabela!r}")


# Tamanho dos lotes de SELECT/DELETE por id: mantem cada IN abaixo do limite de parametros.
LOTE_DELETE = 500


def _em_lotes(itens: list) -> list[list]:
    return [itens[i:i + LOTE_DELETE] for i in range(0, len(itens), LOTE_DELETE)]


@dataclass(frozen=True)
class _Poda:
    """O que `_apagar_reprovadas` precisa saber da janela de noticias."""

    janela: set[int]  # artigos lidos (published_at >= inicio_noticias)
    julgados: set[int]  # artigos com julgamento na versao vigente do prompt da juiza
    exigir_juiza: bool  # entrega_filtro_juiza


def _apagar_reprovadas(linhas: list[dict], poda: _Poda) -> None:
    """Tira de `noticias` o que a juiza reprovou (desenho 5.1). So mexe em artigo julgado.

    O upsert so grava: uma linha entregue antes de ligar o filtro, ou um artigo rejulgado como
    panorama, ficaria para sempre. Com o filtro ligado, artigo julgado sem nenhuma linha
    aprovada sai por id, e o par (id, ticker) que nao esta entre as aprovadas sai por chave
    composta (ticker que deixou de valer). Com o filtro desligado, so saem os pares obsoletos.
    Artigo sem julgamento fica como esta: nem entra com o filtro ligado, nem sai o ja entregue.
    """
    sem_julgamento = poda.janela - poda.julgados
    if poda.exigir_juiza and sem_julgamento:
        logger.warning(
            "entrega: noticias: %d artigos da janela sem julgamento; mantidos como estao",
            len(sem_julgamento),
        )
    aprovadas = {(r["id"], r["ticker"]) for r in linhas}
    com_aprovada = {id_ for id_, _ in aprovadas}
    julgados = {id_noticia(a) for a in poda.janela & poda.julgados}
    with session_scope() as session:
        if poda.exigir_juiza:
            for lote in _em_lotes(sorted(julgados - com_aprovada)):
                session.execute(delete(Noticia.__table__).where(Noticia.id.in_(lote)))
        for lote in _em_lotes(sorted(julgados & com_aprovada)):
            existentes = session.execute(
                select(Noticia.id, Noticia.ticker).where(Noticia.id.in_(lote))
            ).all()
            obsoletas = [(id_, ticker) for id_, ticker in existentes
                         if (id_, ticker) not in aprovadas]
            if obsoletas:
                session.execute(delete(Noticia.__table__).where(
                    tuple_(Noticia.id, Noticia.ticker).in_(obsoletas)
                ))


def _gravar(tabela: str, linhas: list[dict], poda: _Poda | None = None) -> None:
    """Upsert pela chave da spec. Sem linhas, nao faz upsert.

    Em `noticias`, com `poda`, tambem apaga o que a juiza reprovou: ver `_apagar_reprovadas`.
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
    if tabela == "noticias" and poda is not None:
        _apagar_reprovadas(linhas, poda)


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
            poda = None
            if tabela == "noticias":
                poda = _Poda(
                    janela={a["id"] for a in fontes.artigos},
                    julgados={
                        j["article_id"] for j in fontes.julgamentos
                        if j["prompt_versao"] == VERSAO_JUIZA
                    },
                    exigir_juiza=settings.entrega_filtro_juiza,
                )
            _gravar(tabela, linhas, poda)
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
