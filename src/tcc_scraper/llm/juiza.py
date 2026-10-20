"""Juiza de noticias: julga pares (artigo, ticker) ja coletados, sem busca na web.

Ordem por par pendente: regra barata (sem custo) -> Jev (opcional, desligado) -> Gemini.
Grava em `raw_llm_juiza_runs` cada chamada ao Gemini, inclusive as que falharam, e em
`staging_noticias_julgamento` so o julgamento aceito. Retomavel: par sem linha de julgamento
para a versao do prompt volta como pendente na proxima execucao.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, select

from tcc_scraper.config import APELIDOS, NOMES_EMPRESA, get_settings, universo_coleta
from tcc_scraper.db.models import Article, ArticleBody, ArticleTicker
from tcc_scraper.db.models_fontes import RawLlmJuizaRun, StagingNoticiaJulgamento
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.llm.job import garantir_prompt
from tcc_scraper.llm.juiza_prompt import VERSAO, renderizar_prompt_juiza
from tcc_scraper.llm.juiza_regras import nomes_por_ticker, regra_barata
from tcc_scraper.llm.juiza_schema import JulgamentoItem, LoteJulgamento, Papel

logger = logging.getLogger(__name__)

_TRECHO = 300
_CERCA = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_CHAVE_STAGING = ["article_id", "ticker", "prompt_versao"]


@dataclass
class ResultadoJuiza:
    pendentes: int
    regra: int
    jev: int
    gemini: int
    erros: int
    runs: int
    tokens_entrada: int
    tokens_saida: int
    interrompido: int = 0


@dataclass
class _LoteGemini:
    status: str
    erro: str | None
    aceitos: int
    tokens_entrada: int | None
    tokens_saida: int | None


def pendentes(prompt_versao: int, limite: int | None = None) -> list[dict]:
    """Pares (artigo, ticker) com vinculo existente e sem julgamento para esta versao."""
    trecho = func.substr(ArticleBody.body_text, 1, _TRECHO)
    consulta = (
        select(
            Article.id, ArticleTicker.ticker, Article.title, Article.source_domain,
            Article.origem, Article.lang, trecho.label("trecho"),
        )
        .select_from(Article)
        .join(ArticleTicker, ArticleTicker.article_id == Article.id)
        .outerjoin(ArticleBody, ArticleBody.article_id == Article.id)
        .outerjoin(StagingNoticiaJulgamento, and_(
            StagingNoticiaJulgamento.article_id == ArticleTicker.article_id,
            StagingNoticiaJulgamento.ticker == ArticleTicker.ticker,
            StagingNoticiaJulgamento.prompt_versao == prompt_versao,
        ))
        .where(StagingNoticiaJulgamento.article_id.is_(None))
        .order_by(Article.published_at.desc(), Article.id, ArticleTicker.ticker)
    )
    if limite is not None:
        consulta = consulta.limit(limite)
    with session_scope() as s:
        linhas = s.execute(consulta).mappings().all()
    return [{**dict(linha), "trecho": linha["trecho"] or ""} for linha in linhas]


def julgar_pendentes(
    *,
    limite_itens: int | None = None,
    cliente: Any = None,
    jev: Any = None,
    observador: Any = None,
    lote: int | None = None,
) -> ResultadoJuiza:
    settings = get_settings()
    tamanho = lote or settings.juiza_lote
    limite = settings.juiza_max_itens_dia if limite_itens is None else limite_itens
    itens = pendentes(VERSAO, limite)
    res = ResultadoJuiza(pendentes=len(itens), regra=0, jev=0, gemini=0, erros=0, runs=0,
                         tokens_entrada=0, tokens_saida=0)
    universo = set(universo_coleta())
    nomes = nomes_por_ticker(NOMES_EMPRESA, APELIDOS)

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
    res.regra = len(regras)

    # 2) Jev: so se ligado e injetado. O que ele nao decide com confianca segue para o Gemini.
    restantes_llm = restantes
    if settings.jev_enabled and jev is not None:
        restantes_llm = []
        for lote_itens in _fatias(restantes, tamanho):
            if _deve_parar(observador):
                res.interrompido = 1
                return res
            aceitos, sobra = _etapa_jev(lote_itens, jev, minima=settings.juiza_confianca_minima,
                                        modelo=settings.jev_modelo, universo=universo)
            if aceitos:
                with session_scope() as s:
                    _gravar_staging(s, aceitos)
            res.jev += len(aceitos)
            restantes_llm.extend(sobra)

    if not restantes_llm:
        return res

    # 3) Gemini. Sem chave e sem cliente injetado, os restantes ficam pendentes.
    if cliente is None:
        if not settings.gemini_api_key:
            logger.warning("juiza: GEMINI_API_KEY ausente; %d itens seguem pendentes",
                           len(restantes_llm))
            return res
        from tcc_scraper.llm.gemini_client import GeminiClient  # lazy: extra `llm`
        cliente = GeminiClient(settings.gemini_api_key, settings.juiza_modelo,
                               settings.llm_timeout_s, settings.llm_max_attempts)

    lotes = _fatias(restantes_llm, tamanho)
    if observador is not None:
        observador.plano(len(lotes))
    for i, lote_itens in enumerate(lotes, start=1):
        if _deve_parar(observador):
            res.interrompido = 1
            break
        lote_res = _julgar_lote_gemini(lote_itens, cliente, modelo=settings.juiza_modelo,
                                       universo=universo)
        res.runs += 1
        res.gemini += lote_res.aceitos
        res.tokens_entrada += lote_res.tokens_entrada or 0
        res.tokens_saida += lote_res.tokens_saida or 0
        if lote_res.status != "ok":
            res.erros += 1
            logger.warning("juiza: lote %d falhou (%s): %s", i, lote_res.status, lote_res.erro)
        if observador is not None:
            observador.unidade(f"lote|{i}", ok=lote_res.status == "ok",
                               itens=len(lote_itens), saturada=False, erro=lote_res.erro)
    return res


def avaliar_conjunto_ouro(caminho_csv: str | Path) -> dict:
    """Precisao e revocacao por papel sobre o conjunto ouro, sem gravar nada.

    Colunas do CSV: article_id, ticker, titulo, papel, tipo_conteudo. So o titulo e
    conhecido aqui: a regra nao ve origem nem dominio, e o Gemini nao ve o trecho.
    Ambos sao limitacoes da avaliacao, nao do juiz em producao.
    """
    with Path(caminho_csv).open(newline="", encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    settings = get_settings()
    nomes = nomes_por_ticker(NOMES_EMPRESA, APELIDOS)
    gold = [Papel(ln["papel"]).value for ln in linhas]  # enum invalido falha: gabarito errado
    previsto = ["sem_resposta"] * len(linhas)
    indice: dict[tuple[str, str], int] = {}
    pendentes_llm: list[dict] = []
    for idx, ln in enumerate(linhas):
        item = {"id": ln["article_id"], "ticker": ln["ticker"], "title": ln["titulo"],
                "source_domain": "", "origem": "", "trecho": ""}
        julgamento = regra_barata(item, ln["ticker"], nomes)
        if julgamento is not None:
            previsto[idx] = julgamento["papel"]
        else:
            pendentes_llm.append(item)
            indice[(item["id"], item["ticker"])] = idx
    if pendentes_llm:
        from tcc_scraper.llm.gemini_client import GeminiClient  # lazy: extra `llm`
        cliente = GeminiClient(settings.gemini_api_key, settings.juiza_modelo,
                               settings.llm_timeout_s, settings.llm_max_attempts)
        for lote_itens in _fatias(pendentes_llm, settings.juiza_lote):
            prompt = renderizar_prompt_juiza([_payload(i) for i in lote_itens])
            try:
                resposta = cliente.classificar(prompt.texto, prompt.schema)
                julgados = _validar_lote(resposta.texto, set(indice))
            except Exception:  # noqa: BLE001 -- sem resposta valida conta como erro na metrica
                logger.warning("juiza: lote do conjunto ouro sem resposta valida")
                continue
            for julgado in julgados:
                previsto[indice[(julgado.id, julgado.ticker)]] = julgado.papel.value

    por_classe: dict[str, dict[str, float | int | None]] = {}
    for classe in (p.value for p in Papel):
        acertos = sum(p == classe and g == classe for p, g in zip(previsto, gold, strict=True))
        n_previsto = sum(p == classe for p in previsto)
        n_gold = sum(g == classe for g in gold)
        por_classe[classe] = {
            "precisao": acertos / n_previsto if n_previsto else None,
            "revocacao": acertos / n_gold if n_gold else None,
            "suporte": n_gold,
        }
    total = len(linhas)
    acertos_total = sum(p == g for p, g in zip(previsto, gold, strict=True))
    return {
        "n": total,
        "n_regra": total - len(pendentes_llm),
        "n_llm": len(pendentes_llm),
        "sem_resposta": previsto.count("sem_resposta"),
        "acuracia": acertos_total / total if total else None,
        "por_classe": por_classe,
    }


# --------------------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------------------

def _fatias(seq: Sequence, tamanho: int) -> list[list]:
    n = max(1, tamanho)
    return [list(seq[i:i + n]) for i in range(0, len(seq), n)]


def _deve_parar(observador: Any) -> bool:
    return observador is not None and bool(observador.deve_parar())


def _payload(item: dict) -> dict:
    """Item como o template da juiza espera: id, ticker, empresa, titulo, fonte, trecho."""
    return {
        "id": str(item["id"]),
        "ticker": item["ticker"],
        "empresa": NOMES_EMPRESA.get(item["ticker"], item["ticker"]),
        "titulo": item.get("title") or "",
        "fonte": item.get("source_domain") or "",
        "trecho": item.get("trecho") or "",
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


def _validar_lote(texto: str, chaves: set[tuple[str, str]]) -> list[JulgamentoItem]:
    """Valida o lote (levanta JSONDecodeError ou ValidationError) e mantem so os pedidos."""
    lote = LoteJulgamento.model_validate(json.loads(_CERCA.sub("", texto)))
    vistos: set[tuple[str, str]] = set()
    aceitos: list[JulgamentoItem] = []
    for item in lote.itens:
        chave = (item.id, item.ticker)
        if chave in chaves and chave not in vistos:
            vistos.add(chave)
            aceitos.append(item)
    return aceitos


def _etapa_jev(lote_itens: list[dict], jev: Any, *, minima: float, modelo: str,
               universo: set[str]) -> tuple[list[dict], list[dict]]:
    """Julga o lote com o Jev. Devolve (linhas aceitas, itens que seguem para o Gemini)."""
    try:
        brutos = jev.classificar_lote(lote_itens)
    except Exception as exc:  # noqa: BLE001 -- Jev falho nao derruba o lote: vai para o Gemini
        logger.warning("juiza: Jev falhou, lote segue para o Gemini: %s", exc)
        return [], lote_itens
    pedidos = {(str(i["id"]), i["ticker"]): i for i in lote_itens}
    agora = dt.datetime.now(dt.UTC)
    aceitos: list[dict] = []
    vistos: set[tuple[str, str]] = set()
    for bruto in brutos:
        if not isinstance(bruto, dict):
            continue
        try:
            julgamento = JulgamentoItem.model_validate({**bruto, "id": str(bruto.get("id"))})
        except ValidationError:
            continue
        chave = (julgamento.id, julgamento.ticker)
        if chave not in pedidos or chave in vistos or julgamento.confianca < minima:
            continue
        vistos.add(chave)
        aceitos.append(_linha_staging(
            pedidos[chave], julgamento.model_dump(mode="json"), modelo=modelo, run_id=None,
            universo=universo, agora=agora,
        ))
    sobra = [i for i in lote_itens if (str(i["id"]), i["ticker"]) not in vistos]
    return aceitos, sobra


def _julgar_lote_gemini(lote_itens: list[dict], cliente: Any, *, modelo: str,
                        universo: set[str]) -> _LoteGemini:
    """Uma chamada ao Gemini para o lote. Run e julgamentos vao na mesma transacao."""
    prompt = renderizar_prompt_juiza([_payload(i) for i in lote_itens])
    with session_scope() as s:
        prompt_id = garantir_prompt(s, prompt)

    pedidos = {(str(i["id"]), i["ticker"]): i for i in lote_itens}
    iniciado = dt.datetime.now(dt.UTC)
    inicio = time.monotonic()
    resposta, status, erro, julgados = None, "ok", None, []
    try:
        resposta = cliente.classificar(prompt.texto, prompt.schema)
    except Exception as exc:  # noqa: BLE001 -- falha da API vira run erro_api; o lote segue pendente
        status, erro = "erro_api", f"{type(exc).__name__}: {exc}"[:4000]
    if resposta is not None:
        try:
            julgados = _validar_lote(resposta.texto, set(pedidos))
        except json.JSONDecodeError as exc:
            status, erro = "erro_json", str(exc)[:4000]
        except ValidationError as exc:
            status, erro = "erro_validacao", str(exc)[:4000]
    finalizado = dt.datetime.now(dt.UTC)

    linhas: list[dict] = []
    with session_scope() as s:
        run = RawLlmJuizaRun(
            prompt_id=prompt_id,
            modelo=resposta.modelo if resposta else modelo,
            n_itens=len(lote_itens),
            tokens_entrada=resposta.tokens_entrada if resposta else None,
            tokens_saida=resposta.tokens_saida if resposta else None,
            latencia_ms=int((time.monotonic() - inicio) * 1000),
            status=status,
            erro=erro,
            resposta_bruta=resposta.texto if resposta else None,
            iniciado_em=iniciado,
            finalizado_em=finalizado,
        )
        s.add(run)
        s.flush()
        if status == "ok":
            linhas = [
                _linha_staging(pedidos[(j.id, j.ticker)], j.model_dump(mode="json"),
                               modelo=modelo, run_id=run.id, universo=universo, agora=finalizado)
                for j in julgados
            ]
            _gravar_staging(s, linhas)
    return _LoteGemini(
        status=status, erro=erro, aceitos=len(linhas),
        tokens_entrada=resposta.tokens_entrada if resposta else None,
        tokens_saida=resposta.tokens_saida if resposta else None,
    )
