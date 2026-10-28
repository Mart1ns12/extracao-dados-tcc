"""Juiza de noticias: julga pares (artigo, ticker) ja coletados, sem busca na web.

Ordem por par pendente: regra barata (sem custo) -> Jev (opcional, `jev_enabled`, no modo
`jev_modo_efetivo`: triagem, decide ou entrega; ver `_aceita_jev`) -> LLM (`juiza_provedor`:
anthropic, padrao, openai ou gemini), so no que sobrou. Grava em
`v1-raw_llm_juiza_runs` cada chamada paga (LLM ou Jev), inclusive as que falharam (com
`custo_usd` e `finalidade`), e em
`v1-staging_noticias_julgamento` so o julgamento aceito. Cada par nao aceito pela LLM ganha
uma tentativa em `v1-staging_juiza_tentativas`; ao esgotar `juiza_max_tentativas` deixa de
ser pendente. Retomavel: par sem julgamento para a versao do prompt volta como pendente na
proxima execucao.

Paradas antecipadas (`ResultadoJuiza.parada`): `jev_sem_chave` (Jev ligado sem chave: a LLM
nao e chamada), `erro_jev` (falha do Jev: o lote fica pendente e a LLM nao e chamada),
`sem_credito` (conta da LLM sem saldo), `erro_api`, `orcamento_usd`, `teto_itens` e `cancelado`.

Tetos: itens (cada par que gasta LLM conta uma vez, mesmo que passe pelo Jev e depois pela
LLM; a regra e de graca) e US$ (ver `juiza_custo.teto_usd`; o custo do Jev entra no mesmo
teto).
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, case, func, or_, select, tuple_

from tcc_scraper.config import APELIDOS, NOMES_EMPRESA, get_settings, universo_coleta
from tcc_scraper.db.models import Article, ArticleBody, ArticleTicker
from tcc_scraper.db.models_fontes import (
    RawLlmJuizaRun,
    StagingJuizaTentativa,
    StagingNoticiaJulgamento,
)
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.enrich import casador
from tcc_scraper.entrega.filtros import Julgamento, aprovado_para_entrega
from tcc_scraper.llm.anthropic_client import AnthropicClient
from tcc_scraper.llm.jev_client import (
    JevFalhaLote,
    decisao_entrega,
    prompt_jev,
    rotulo_modelo_jev,
)
from tcc_scraper.llm.job import garantir_prompt
from tcc_scraper.llm.juiza_custo import custo_jev, custo_resposta, teto_usd
from tcc_scraper.llm.juiza_prompt import VERSAO, renderizar_prompt_juiza
from tcc_scraper.llm.juiza_regras import nomes_por_ticker, regra_barata
from tcc_scraper.llm.juiza_schema import JulgamentoItem
from tcc_scraper.llm.openai_client import OpenAIClient, SemCreditoLLM

logger = logging.getLogger(__name__)

_TRECHO = 300
# Prefixos de modelo de OUTROS provedores: openai e gemini recusam modelos claude* (e cada
# um recusa o do outro). O anthropic exige claude* (ver avisos_config).
_PREFIXOS_ALHEIOS = {"openai": ("gemini", "claude"), "gemini": ("gpt", "claude")}
_CERCA = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_CHAVE_STAGING = ["article_id", "ticker", "prompt_versao"]
_CHAVE_TENTATIVAS = ["article_id", "ticker", "prompt_versao"]


@dataclass
class ResultadoJuiza:
    pendentes: int
    regra: int
    jev: int
    llm: int
    erros: int
    runs: int
    tokens_entrada: int
    tokens_saida: int
    interrompido: int = 0
    custo_usd: float = 0.0
    # Pares pedidos a LLM que voltaram sem julgamento valido (inclui os omitidos).
    invalidos: int = 0
    # Motivo de parada antecipada (ver docstring do modulo).
    parada: str | None = None
    # Provedor da LLM usado na execucao e tokens lidos do cache do provedor.
    provedor: str = ""
    tokens_cache: int = 0
    # Avisos de configuracao (ver avisos_config).
    avisos: list[str] = field(default_factory=list)


@dataclass
class _LoteLlm:
    status: str
    erro: str | None
    aceitos: int
    invalidos: int
    tokens_entrada: int | None
    tokens_saida: int | None
    custo_usd: float
    # Conta sem credito (SemCreditoLLM ou HTTP 402): para a execucao; nao e tentativa do par.
    sem_credito: bool = False
    tokens_cache: int | None = None


@dataclass
class _LoteJev:
    status: str  # ok, parcial ou erro_api
    permanente: bool  # erro_api que repetir nao resolve (401, 402, 403)
    linhas: list[dict]  # staging aceitas
    sobra: list[dict]  # pares que seguem para a LLM
    custo: float


def _juntar(consulta, prompt_versao: int):
    """Joins de pendentes e de contar_pendentes: artigo, vinculo, corpo, julgamento e tentativas."""
    return (
        consulta.select_from(Article)
        .join(ArticleTicker, ArticleTicker.article_id == Article.id)
        .outerjoin(ArticleBody, ArticleBody.article_id == Article.id)
        .outerjoin(StagingNoticiaJulgamento, and_(
            StagingNoticiaJulgamento.article_id == ArticleTicker.article_id,
            StagingNoticiaJulgamento.ticker == ArticleTicker.ticker,
            StagingNoticiaJulgamento.prompt_versao == prompt_versao,
        ))
        .outerjoin(StagingJuizaTentativa, and_(
            StagingJuizaTentativa.article_id == ArticleTicker.article_id,
            StagingJuizaTentativa.ticker == ArticleTicker.ticker,
            StagingJuizaTentativa.prompt_versao == prompt_versao,
        ))
    )


@functools.lru_cache(maxsize=1)
def _regras() -> dict:
    """Regras de casamento do casador, calculadas uma vez por processo."""
    return casador.construir_regras()


def pendentes(
    prompt_versao: int,
    limite: int | None = None,
    *,
    ordem: str = "desc",
    apos: tuple | None = None,
    anos: Sequence[int] | None = None,
    origens: Sequence[str] | None = None,
    max_tentativas: int | None = None,
    trecho: int = _TRECHO,
) -> list[dict]:
    """Pares (artigo, ticker) com vinculo e sem julgamento para esta versao.

    `apos` e o cursor keyset (published_at, id, ticker) do ultimo par da pagina anterior.
    `anos` filtra pelo ano de publicacao em UTC. `origens` filtra `Article.origem`.
    `max_tentativas` deixa de fora os pares
    que ja esgotaram as tentativas. `trecho` e o numero de caracteres do corpo na coluna
    `trecho`. Para origem wordpress, a coluna traz as janelas do corpo completo em volta das
    mencoes do ticker (casador.trecho_mencoes); sem mencao valida, fica o inicio do corpo.
    """
    corpo = func.substr(ArticleBody.body_text, 1, trecho)
    tentativas = func.coalesce(StagingJuizaTentativa.tentativas, 0)
    consulta = _juntar(select(
        Article.id, ArticleTicker.ticker, Article.title, Article.source_domain,
        Article.origem, Article.lang, Article.published_at,
        tentativas.label("tentativas"), corpo.label("trecho"),
    ), prompt_versao).where(StagingNoticiaJulgamento.article_id.is_(None))
    if anos:
        janelas = [and_(
            Article.published_at >= dt.datetime(ano, 1, 1, tzinfo=dt.UTC),
            Article.published_at < dt.datetime(ano + 1, 1, 1, tzinfo=dt.UTC),
        ) for ano in sorted(set(anos))]
        consulta = consulta.where(or_(*janelas))
    if origens:
        consulta = consulta.where(Article.origem.in_(sorted(set(origens))))
    if max_tentativas is not None:
        consulta = consulta.where(tentativas < max_tentativas)

    chaves = (Article.published_at, Article.id, ArticleTicker.ticker)
    if ordem == "asc":
        ordenacao = [c.asc() for c in chaves]
        if apos is not None:
            consulta = consulta.where(tuple_(*chaves) > tuple(apos))
    elif ordem == "desc":
        ordenacao = [c.desc() for c in chaves]
        if apos is not None:
            consulta = consulta.where(tuple_(*chaves) < tuple(apos))
    else:
        raise ValueError(f"ordem desconhecida: {ordem!r}")
    consulta = consulta.order_by(*ordenacao)
    if limite is not None:
        consulta = consulta.limit(limite)
    with session_scope() as s:
        linhas = s.execute(consulta).mappings().all()
        # A consulta acima so traz o inicio do corpo. Posts WordPress precisam do corpo inteiro
        # para a mencao do ticker entrar no trecho: uma query em lote, nunca uma por item.
        ids_wp = list({linha["id"] for linha in linhas if linha["origem"] == "wordpress"})
        corpos_wp = {}
        if ids_wp:
            corpos_wp = dict(s.execute(
                select(ArticleBody.article_id, ArticleBody.body_text)
                .where(ArticleBody.article_id.in_(ids_wp))
            ).all())
    saida = []
    for linha in linhas:
        item = {**dict(linha), "trecho": linha["trecho"] or ""}
        texto_completo = corpos_wp.get(linha["id"])
        if linha["origem"] == "wordpress" and texto_completo:
            mencoes = casador.trecho_mencoes(texto_completo, linha["ticker"], _regras(), trecho)
            if mencoes:
                item["trecho"] = mencoes
        saida.append(item)
    return saida


def _consulta_contagem(max_tentativas: int, prompt_versao: int = VERSAO):
    """Uma linha: pendentes (com tentativas), esgotados e julgados, com os joins de pendentes."""
    tentativas = func.coalesce(StagingJuizaTentativa.tentativas, 0)
    sem_julgamento = StagingNoticiaJulgamento.article_id.is_(None)
    ainda = case((and_(sem_julgamento, tentativas < max_tentativas), 1))
    esgotado = case((and_(sem_julgamento, tentativas >= max_tentativas), 1))
    return _juntar(select(
        func.count(ainda).label("pendentes"),
        func.count(esgotado).label("esgotados"),
        func.count(StagingNoticiaJulgamento.article_id).label("julgados"),
    ), prompt_versao)


def contar_pendentes(max_tentativas: int) -> dict[str, int]:
    """Contagem do prompt atual: pendentes ainda com tentativas, esgotados e julgados."""
    with session_scope() as s:
        linha = s.execute(_consulta_contagem(max_tentativas)).one()
    return {"pendentes": int(linha.pendentes), "esgotados": int(linha.esgotados),
            "julgados": int(linha.julgados)}


def avisos_config(settings: Any) -> list[str]:
    """Problemas de configuracao da juiza, em ordem fixa. Lista vazia = tudo certo.

    `jev_desligado` so informa. `jev_sem_chave` bloqueia a LLM (ver julgar_pendentes).
    `<provedor>_sem_chave` deixa a LLM indisponivel. `modelo_incompativel` (modelo de outro
    provedor) impede a execucao.
    """
    avisos: list[str] = []
    if not settings.jev_enabled:
        avisos.append("jev_desligado")
    elif not settings.jev_api_key:
        avisos.append("jev_sem_chave")
    if not settings.juiza_chave_llm:
        avisos.append(f"{settings.juiza_provedor}_sem_chave")
    provedor, modelo = settings.juiza_provedor, settings.juiza_modelo_efetivo
    if provedor == "anthropic":
        incompativel = not modelo.startswith("claude")
    else:
        incompativel = modelo.startswith(_PREFIXOS_ALHEIOS.get(provedor, ()))
    if incompativel:
        avisos.append("modelo_incompativel")
    return avisos


def julgar_pendentes(
    *,
    limite_itens: int | None = None,
    cliente: Any = None,
    jev: Any = None,
    observador: Any = None,
    lote: int | None = None,
    modo: str = "diario",
    anos: Sequence[int] | None = None,
    origens: Sequence[str] | None = None,
    orcamento_usd: float | None = None,
) -> ResultadoJuiza:
    """Julga os pares pendentes, pagina a pagina. `modo` e "diario" (producao) ou "backlog".

    Teto de itens: no diario, `limite_itens` ou `juiza_max_itens_dia`; no backlog,
    `limite_itens` (None = sem teto). A regra barata nao conta para o teto.
    Teto de US$: `teto_usd(modo, orcamento_usd)`, conferido antes de cada lote pago.
    Jev: o `jev` injetado, ou um JevClient real se `jev_enabled` e `jev_api_key`. So roda com
    `jev_enabled`. LLM: `cliente` injetado ou o do `juiza_provedor`, se houver chave.
    Sem cliente pago (nem Jev nem LLM), so a primeira pagina de regra roda.
    `anos` e `origens` filtram os pares (ver pendentes); a carga historica usa `origens` para
    julgar WordPress e IPE antes do Google.
    Levanta ValueError se `juiza_modelo` for de outro provedor (ver avisos_config).
    """
    if modo not in ("diario", "backlog"):
        raise ValueError(f"modo desconhecido: {modo!r}")
    settings = get_settings()
    avisos = avisos_config(settings)
    for aviso in avisos:
        logger.warning("juiza: configuracao: %s", aviso)
    if "modelo_incompativel" in avisos:
        raise ValueError(
            f"JUIZA_MODELO={settings.juiza_modelo!r} com JUIZA_PROVEDOR="
            f"{settings.juiza_provedor}: remova JUIZA_MODELO do .env"
        )
    tamanho = lote or settings.juiza_lote
    backlog = modo == "backlog"
    finalidade = "backlog" if backlog else "producao"
    ordem = "asc" if backlog else "desc"
    if backlog:
        teto_itens = limite_itens
    else:
        teto_itens = settings.juiza_max_itens_dia if limite_itens is None else limite_itens
    universo = set(universo_coleta())
    nomes = nomes_por_ticker(NOMES_EMPRESA, APELIDOS)
    res = ResultadoJuiza(pendentes=0, regra=0, jev=0, llm=0, erros=0, runs=0,
                         tokens_entrada=0, tokens_saida=0, provedor=settings.juiza_provedor,
                         avisos=avisos)
    jev_proprio = None
    if jev is None and settings.jev_enabled and settings.jev_api_key:
        from tcc_scraper.llm.jev_client import JevClient  # lazy: so existe com o Jev ligado
        jev = jev_proprio = JevClient(settings.jev_api_key, settings.jev_modelo,
                                      settings.jev_timeout_s,
                                      concorrencia=settings.jev_concorrencia,
                                      trecho_max=settings.jev_trecho)
    jev_ativo = settings.jev_enabled and jev is not None
    bloqueia_llm = "jev_sem_chave" in avisos
    llm_ok = not bloqueia_llm and (cliente is not None or bool(settings.juiza_chave_llm))
    pago_ok = jev_ativo or llm_ok
    # O Jev recebe o trecho configurado; sem Jev, o trecho curto de sempre.
    trecho_pedido = max(_TRECHO, settings.jev_trecho) if jev_ativo else _TRECHO
    teto: float | None = None  # calculado na primeira vez que um lote pago for necessario
    ultimo_custo = 0.0
    # Pares que gastaram LLM. Com Jev, o par conta no lote do Jev e nao de novo na LLM.
    pagos = 0
    erros_api_seguidos = 0
    erros_jev_seguidos = 0
    cliente_proprio = None
    cursor: tuple | None = None

    try:
        while True:
            itens = pendentes(VERSAO, settings.juiza_pagina, ordem=ordem, apos=cursor, anos=anos,
                              origens=origens, max_tentativas=settings.juiza_max_tentativas,
                              trecho=trecho_pedido)
            res.pendentes += len(itens)

            # 1) Regra barata: resolve o obvio sem gastar token da LLM.
            agora = dt.datetime.now(dt.UTC)
            restantes: list[dict] = []
            regras: list[dict] = []
            for item in itens:
                julgamento = regra_barata(item, item["ticker"], nomes)
                if julgamento is None:
                    restantes.append(item)
                else:
                    regras.append(_linha_staging(item, julgamento, modelo="regra", run_id=None,
                                                 universo=universo, agora=agora))
            if regras:
                with session_scope() as s:
                    _gravar_staging(s, regras)
            res.regra += len(regras)

            # 2) Jev e LLM. O teto de itens corta a pagina antes de qualquer chamada paga.
            restantes_llm = restantes
            if restantes and bloqueia_llm:
                res.parada = "jev_sem_chave"
                break
            if restantes and pago_ok:
                if teto_itens is not None:
                    restante_itens = teto_itens - pagos
                    if restante_itens <= 0:
                        res.parada = "teto_itens"
                        break
                    restantes_llm = restantes[:restante_itens]
                if teto is None:
                    teto = teto_usd(modo, orcamento_usd, settings)

                # 2a) Jev: o que ele nao decide segue para a LLM.
                if jev_ativo:
                    pendentes_jev, restantes_llm = restantes_llm, []
                    for lote_itens in _fatias(pendentes_jev, settings.jev_lote):
                        if _deve_parar(observador):
                            res.interrompido, res.parada = 1, "cancelado"
                            break
                        if res.custo_usd + ultimo_custo >= teto:
                            res.parada = "orcamento_usd"
                            break
                        pagos += len(lote_itens)
                        lote_jev = _julgar_lote_jev(
                            lote_itens, jev, modo=settings.jev_modo_efetivo, settings=settings,
                            universo=universo, finalidade=finalidade,
                        )
                        ultimo_custo = lote_jev.custo
                        res.custo_usd += lote_jev.custo
                        res.runs += 1
                        res.jev += len(lote_jev.linhas)
                        if lote_jev.status == "erro_api":
                            # Lote inteiro falhou: fica pendente, sem tentativa e sem LLM.
                            res.erros += 1
                            erros_jev_seguidos += 1
                            if lote_jev.permanente or erros_jev_seguidos >= 2:
                                res.parada = "erro_jev"
                                break
                        else:
                            erros_jev_seguidos = 0
                        restantes_llm.extend(lote_jev.sobra)
                    if res.parada:
                        break

                # 2b) LLM.
                if restantes_llm and llm_ok:
                    if cliente is None:
                        cliente = _criar_cliente_llm(settings)
                        cliente_proprio = cliente
                    lotes = _fatias(restantes_llm, tamanho)
                    if observador is not None:
                        observador.plano(len(lotes))
                    for i, lote_itens in enumerate(lotes, start=1):
                        if _deve_parar(observador):
                            res.interrompido, res.parada = 1, "cancelado"
                            break
                        if res.custo_usd + ultimo_custo >= teto:
                            res.parada = "orcamento_usd"
                            break
                        if not jev_ativo:
                            pagos += len(lote_itens)
                        lote_res = _julgar_lote_llm(lote_itens, cliente,
                                                    modelo=settings.juiza_modelo_efetivo,
                                                    universo=universo, finalidade=finalidade,
                                                    settings=settings)
                        ultimo_custo = lote_res.custo_usd
                        res.custo_usd += lote_res.custo_usd
                        res.runs += 1
                        res.llm += lote_res.aceitos
                        res.invalidos += lote_res.invalidos
                        res.tokens_entrada += lote_res.tokens_entrada or 0
                        res.tokens_saida += lote_res.tokens_saida or 0
                        res.tokens_cache += lote_res.tokens_cache or 0
                        if lote_res.sem_credito:
                            res.erros += 1
                            res.parada = "sem_credito"
                            logger.warning("juiza: %s sem credito; execucao parada: %s",
                                           settings.juiza_provedor, lote_res.erro)
                            break
                        erros_api_seguidos = (erros_api_seguidos + 1
                                              if lote_res.status == "erro_api" else 0)
                        if lote_res.status != "ok":
                            res.erros += 1
                            logger.warning("juiza: lote %d falhou (%s): %s", i, lote_res.status,
                                           lote_res.erro)
                        if observador is not None:
                            observador.unidade(f"lote|{i}", ok=lote_res.status == "ok",
                                               itens=len(lote_itens), saturada=False,
                                               erro=lote_res.erro)
                        if erros_api_seguidos >= 2:
                            res.parada = "erro_api"
                            break

                if not res.parada and teto_itens is not None and pagos >= teto_itens:
                    res.parada = "teto_itens"

            if restantes_llm and not llm_ok:
                logger.warning("juiza: %s sem chave: %d itens ficam pendentes",
                               settings.juiza_provedor, len(restantes_llm))
            if res.parada:
                break
            if not pago_ok or len(itens) < settings.juiza_pagina:
                break
            ultimo = itens[-1]
            cursor = (ultimo["published_at"], ultimo["id"], ultimo["ticker"])
    finally:
        if jev_proprio is not None:
            jev_proprio.close()
        if cliente_proprio is not None and hasattr(cliente_proprio, "close"):
            cliente_proprio.close()
    return res


# --------------------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------------------

def _fatias(seq: Sequence, tamanho: int) -> list[list]:
    n = max(1, tamanho)
    return [list(seq[i:i + n]) for i in range(0, len(seq), n)]


def _deve_parar(observador: Any) -> bool:
    return observador is not None and bool(observador.deve_parar())


def _criar_cliente_llm(settings: Any) -> Any:
    """Cliente da LLM do `juiza_provedor`. O Gemini so e importado quando e usado (extra llm)."""
    if settings.juiza_provedor == "anthropic":
        return AnthropicClient(
            settings.anthropic_api_key, settings.juiza_modelo_efetivo,
            settings.anthropic_timeout_s, settings.anthropic_max_attempts,
            effort=settings.anthropic_effort,
            max_output_tokens=settings.anthropic_max_output_tokens,
        )
    if settings.juiza_provedor == "openai":
        return OpenAIClient(
            settings.openai_api_key, settings.juiza_modelo_efetivo, settings.openai_timeout_s,
            settings.openai_max_attempts, reasoning_effort=settings.openai_reasoning_effort,
            max_output_tokens=settings.openai_max_output_tokens,
            base_url=settings.openai_base_url,
        )
    from tcc_scraper.llm.gemini_client import GeminiClient  # lazy: so existe com o extra llm
    return GeminiClient(settings.gemini_api_key, settings.juiza_modelo_efetivo,
                        settings.llm_timeout_s, settings.llm_max_attempts)


def _payload(item: dict) -> dict:
    """Item como o template da juiza espera: id, ticker, empresa, titulo, fonte, trecho."""
    return {
        "id": str(item["id"]),
        "ticker": item["ticker"],
        "empresa": NOMES_EMPRESA.get(item["ticker"], item["ticker"]),
        "titulo": item.get("title") or "",
        "fonte": item.get("source_domain") or "",
        "trecho": (item.get("trecho") or "")[:_TRECHO],
    }


def _linha_staging(item: dict, julgamento: dict, *, modelo: str, run_id: int | None,
                   universo: set[str], agora: dt.datetime) -> dict:
    adicionais = [t for t in dict.fromkeys(julgamento.get("tickers_adicionais", []))
                  if t in universo]
    return {
        "article_id": item["id"],
        "ticker": item["ticker"],
        "prompt_versao": VERSAO,
        "run_id": run_id,
        "modelo": modelo,
        "papel": julgamento["papel"],
        "tipo_conteudo": julgamento["tipo_conteudo"],
        "evento": julgamento["evento"],
        "tickers_adicionais": adicionais,
        "qualidade": float(julgamento["qualidade"]),
        "confianca": float(julgamento["confianca"]),
        "julgado_em": agora,
    }


def _gravar_staging(session, linhas: list[dict]) -> None:
    if linhas:
        bulk_upsert(session, StagingNoticiaJulgamento.__table__, linhas,
                    index_elements=_CHAVE_STAGING, update_columns=None)


def _gravar_run(s, **campos) -> int:
    """Grava uma run da juiza (LLM ou Jev), append-only, e devolve o id."""
    run = RawLlmJuizaRun(**campos)
    s.add(run)
    s.flush()
    return run.id


def _registrar_tentativas(session, itens: list[dict], *, status: str, erro: str | None) -> None:
    """Soma uma tentativa a cada par pedido que nao foi aceito. `tentativas` vem do pendentes()."""
    agora = dt.datetime.now(dt.UTC)
    linhas = [{
        "article_id": item["id"],
        "ticker": item["ticker"],
        "prompt_versao": VERSAO,
        "tentativas": int(item.get("tentativas") or 0) + 1,
        "ultimo_status": status,
        "ultimo_erro": erro,
        "atualizado_em": agora,
    } for item in itens]
    if linhas:
        bulk_upsert(session, StagingJuizaTentativa.__table__, linhas,
                    index_elements=_CHAVE_TENTATIVAS,
                    update_columns=["tentativas", "ultimo_status", "ultimo_erro",
                                    "atualizado_em"])


def _validar_itens(texto: str, chaves: set[tuple[str, str]]
                   ) -> tuple[list[JulgamentoItem], list[str]]:
    """Valida item a item. Devolve (aceitos, erros).

    JSON invalido levanta JSONDecodeError e lista ausente levanta ValueError (o lote inteiro
    falha). Item invalido vira erro e nao derruba o lote. Item fora do pedido ou repetido e
    descartado em silencio.
    """
    bruto = json.loads(_CERCA.sub("", texto))
    itens = bruto.get("itens") if isinstance(bruto, dict) else None
    if not isinstance(itens, list):
        raise ValueError("resposta sem lista 'itens'")
    aceitos: list[JulgamentoItem] = []
    erros: list[str] = []
    vistos: set[tuple[str, str]] = set()
    for item in itens:
        try:
            julgamento = JulgamentoItem.model_validate(item)
        except ValidationError as exc:
            erros.append(str(exc)[:500])
            continue
        chave = (julgamento.id, julgamento.ticker)
        if chave in chaves and chave not in vistos:
            vistos.add(chave)
            aceitos.append(julgamento)
    return aceitos, erros


def _aceita_jev(bruto: dict, julgamento: JulgamentoItem, origem: str, *, modo: str,
                settings: Any) -> float | None:
    """Confianca com que o julgamento do Jev entra na staging, ou None se ele vai para a LLM.

    entrega: decide sozinho quando P(entra) ou P(nao entra) passa de `juiza_jev_limiar_entrega`
    e concorda com a regra de entrega; a confianca gravada e essa probabilidade.
    decide: aceita se confianca >= `juiza_confianca_minima`.
    triagem: so descarte (o que `aprovado_para_entrega` reprova) com confianca >=
    `juiza_confianca_triagem`.
    """
    if modo == "entrega":
        decisao = decisao_entrega(bruto, settings.juiza_jev_limiar_entrega)
        if decisao is None:
            return None
        return float(bruto["p_entra"] if decisao else bruto["p_nao_entra"])
    if modo == "decide":
        if julgamento.confianca < settings.juiza_confianca_minima:
            return None
        return julgamento.confianca
    if julgamento.confianca < settings.juiza_confianca_triagem:
        return None
    dados = julgamento.model_dump(mode="json")
    par = Julgamento(
        papel=dados["papel"], tipo_conteudo=dados["tipo_conteudo"], evento=dados["evento"],
        qualidade=dados["qualidade"], tickers_adicionais=tuple(dados["tickers_adicionais"]),
    )
    if aprovado_para_entrega(par, origem, exigir_juiza=True):
        return None
    return julgamento.confianca


def _julgar_lote_jev(lote_itens: list[dict], jev: Any, *, modo: str, settings: Any,
                     universo: set[str], finalidade: str) -> _LoteJev:
    """Um lote no Jev. Grava a run e devolve o aceito (staging), a sobra para a LLM e o custo.

    A run e gravada com o rotulo de `rotulo_modelo_jev()`, quando a chamada voltou (ok ou
    parcial) ou falhou (erro_api). Item que o Jev nao resolve vai para a sobra, sem tentativa.
    Excecao do cliente (lote inteiro falho) deixa o lote pendente: sobra vazia, sem LLM e sem
    tentativa. `permanente` vem de JevFalhaLote.
    """
    modelo = rotulo_modelo_jev(settings.jev_modelo, settings.jev_trecho)
    pedidos = {(str(i["id"]), i["ticker"]): i for i in lote_itens}
    with session_scope() as s:
        prompt_id = garantir_prompt(s, prompt_jev())
    iniciado = dt.datetime.now(dt.UTC)
    inicio = time.monotonic()
    try:
        resposta = jev.classificar_lote(lote_itens)
    except Exception as exc:  # noqa: BLE001 -- Jev falho nao derruba a execucao: o lote fica pendente
        permanente = exc.permanente if isinstance(exc, JevFalhaLote) else False
        logger.warning("juiza: Jev falhou, lote fica pendente: %s", exc)
        with session_scope() as s:
            _gravar_run(
                s, prompt_id=prompt_id, modelo=modelo, n_itens=len(lote_itens),
                tokens_entrada=None, tokens_saida=None, custo_usd=0.0,
                latencia_ms=int((time.monotonic() - inicio) * 1000), status="erro_api",
                finalidade=finalidade, erro=f"{type(exc).__name__}: {exc}"[:4000],
                resposta_bruta=None, iniciado_em=iniciado,
                finalizado_em=dt.datetime.now(dt.UTC),
            )
        return _LoteJev(status="erro_api", permanente=permanente, linhas=[], sobra=[], custo=0.0)

    finalizado = dt.datetime.now(dt.UTC)
    custo = custo_jev(resposta.tokens_entrada, settings)
    aceitos: list[tuple[dict, dict]] = []
    vistos: set[tuple[str, str]] = set()
    for bruto in resposta.itens:
        if not isinstance(bruto, dict):
            continue
        try:
            julgamento = JulgamentoItem.model_validate({**bruto, "id": str(bruto.get("id"))})
        except ValidationError:
            continue
        chave = (julgamento.id, julgamento.ticker)
        if chave not in pedidos or chave in vistos:
            continue
        origem = pedidos[chave].get("origem") or ""
        conf = _aceita_jev(bruto, julgamento, origem, modo=modo, settings=settings)
        if conf is None:
            continue
        vistos.add(chave)
        aceitos.append((pedidos[chave], {**julgamento.model_dump(mode="json"), "confianca": conf}))
    sobra = [i for i in lote_itens if (str(i["id"]), i["ticker"]) not in vistos]
    erro = "; ".join(f"{e['id']}/{e['ticker']}: {e['erro']}"
                     for e in resposta.erros)[:4000] or None
    bruta = json.dumps({"bruto": resposta.bruto, "erros": resposta.erros}, ensure_ascii=False)
    with session_scope() as s:
        run_id = _gravar_run(
            s, prompt_id=prompt_id, modelo=modelo, n_itens=len(lote_itens),
            tokens_entrada=resposta.tokens_entrada, tokens_saida=None, custo_usd=custo,
            latencia_ms=resposta.latencia_ms, status="parcial" if resposta.erros else "ok",
            finalidade=finalidade, erro=erro, resposta_bruta=bruta,
            iniciado_em=iniciado, finalizado_em=finalizado,
        )
        linhas = [
            _linha_staging(item, dados, modelo=modelo, run_id=run_id, universo=universo,
                           agora=finalizado)
            for item, dados in aceitos
        ]
        _gravar_staging(s, linhas)
    return _LoteJev(status="parcial" if resposta.erros else "ok", permanente=False,
                    linhas=linhas, sobra=sobra, custo=custo)


def _julgar_lote_llm(lote_itens: list[dict], cliente: Any, *, modelo: str,
                     universo: set[str], finalidade: str, settings: Any) -> _LoteLlm:
    """Uma chamada a LLM para o lote. Run, julgamentos e tentativas vao na mesma transacao.

    Status: ok (todos julgados), parcial (alguns faltam ou sao invalidos), erro_validacao
    (nenhum aceito), erro_json, erro_api. Em erro_api nao ha tentativa: a falha foi da API,
    nao do par. `sem_credito` (SemCreditoLLM ou HTTP 402) tambem nao gera tentativa e para a
    execucao.
    """
    prompt = renderizar_prompt_juiza([_payload(i) for i in lote_itens],
                                     provedor=settings.juiza_provedor)
    with session_scope() as s:
        prompt_id = garantir_prompt(s, prompt)

    pedidos = {(str(i["id"]), i["ticker"]): i for i in lote_itens}
    iniciado = dt.datetime.now(dt.UTC)
    inicio = time.monotonic()
    resposta, status, erro = None, "ok", None
    sem_credito = False
    julgados: list[JulgamentoItem] = []
    try:
        resposta = cliente.classificar(prompt.texto, prompt.schema)
    except Exception as exc:  # noqa: BLE001 -- falha da API vira run erro_api; o lote segue pendente
        status, erro = "erro_api", f"{type(exc).__name__}: {exc}"[:4000]
        sem_credito = (isinstance(exc, SemCreditoLLM) or getattr(exc, "code", None) == 402
                       or getattr(exc, "status_code", None) == 402)
    faltam = len(pedidos) if resposta is not None else 0
    if resposta is not None:
        try:
            julgados, erros = _validar_itens(resposta.texto, set(pedidos))
        except json.JSONDecodeError as exc:
            status, erro = "erro_json", str(exc)[:4000]
        except ValueError as exc:
            status, erro = "erro_validacao", str(exc)[:4000]
        else:
            faltam = len(pedidos) - len(julgados)
            if not julgados:
                status = "erro_validacao"
            elif faltam or erros:
                status = "parcial"
            motivo = "; ".join(erros) or (f"{faltam} item(ns) sem julgamento" if faltam else None)
            erro = motivo[:4000] if motivo else None
    finalizado = dt.datetime.now(dt.UTC)
    custo = 0.0
    if resposta is not None:
        custo = custo_resposta(resposta, settings)

    linhas: list[dict] = []
    with session_scope() as s:
        run_id = _gravar_run(
            s,
            prompt_id=prompt_id,
            modelo=resposta.modelo if resposta else modelo,
            n_itens=len(lote_itens),
            tokens_entrada=resposta.tokens_entrada if resposta else None,
            tokens_saida=resposta.tokens_saida if resposta else None,
            custo_usd=custo,
            latencia_ms=int((time.monotonic() - inicio) * 1000),
            status=status,
            finalidade=finalidade,
            erro=erro,
            resposta_bruta=resposta.texto if resposta else None,
            iniciado_em=iniciado,
            finalizado_em=finalizado,
        )
        if julgados:
            linhas = [
                _linha_staging(pedidos[(j.id, j.ticker)], j.model_dump(mode="json"),
                               modelo=modelo, run_id=run_id, universo=universo, agora=finalizado)
                for j in julgados
            ]
            _gravar_staging(s, linhas)
        if status != "erro_api":
            aceitos_chaves = {(j.id, j.ticker) for j in julgados}
            nao_aceitos = [i for chave, i in pedidos.items() if chave not in aceitos_chaves]
            _registrar_tentativas(s, nao_aceitos, status=status, erro=erro)
    return _LoteLlm(
        status=status, erro=erro, aceitos=len(linhas), invalidos=faltam,
        tokens_entrada=resposta.tokens_entrada if resposta else None,
        tokens_saida=resposta.tokens_saida if resposta else None,
        custo_usd=custo,
        sem_credito=sem_credito,
        tokens_cache=getattr(resposta, "tokens_cache", None) if resposta is not None else None,
    )
