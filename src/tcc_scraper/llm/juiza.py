"""Juiza de noticias: julga pares (artigo, ticker) ja coletados, sem busca na web.

Ordem por par pendente: regra barata (sem custo) -> Jev (opcional, `jev_enabled`) -> Gemini.
O Jev decide ou so descarta, conforme `juiza_jev_decide` (ver `_aceita_jev`). Grava em
`v1-raw_llm_juiza_runs` cada chamada paga (Gemini ou Jev), inclusive as que falharam (com
`custo_usd` e `finalidade`), e em `v1-staging_noticias_julgamento` so o julgamento aceito.
Cada par nao aceito pelo Gemini ganha uma tentativa em `v1-staging_juiza_tentativas`; ao
esgotar `juiza_max_tentativas` deixa de ser pendente. Retomavel: par sem julgamento para a
versao do prompt volta como pendente na proxima execucao.

Tetos: itens (cada par que gasta LLM conta uma vez, mesmo que passe pelo Jev e depois pelo
Gemini; a regra e de graca) e US$ (ver `juiza_custo.teto_usd`; o custo do Jev entra no mesmo
teto).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
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
from tcc_scraper.entrega.filtros import Julgamento, aprovado_para_entrega
from tcc_scraper.llm.jev_client import JEV_PERGUNTAS_VERSAO, prompt_jev
from tcc_scraper.llm.job import garantir_prompt
from tcc_scraper.llm.juiza_custo import custo_gemini, custo_jev, teto_usd
from tcc_scraper.llm.juiza_prompt import VERSAO, renderizar_prompt_juiza
from tcc_scraper.llm.juiza_regras import nomes_por_ticker, regra_barata
from tcc_scraper.llm.juiza_schema import JulgamentoItem

logger = logging.getLogger(__name__)

_TRECHO = 300
_CERCA = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_CHAVE_STAGING = ["article_id", "ticker", "prompt_versao"]
_CHAVE_TENTATIVAS = ["article_id", "ticker", "prompt_versao"]


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
    custo_usd: float = 0.0
    # Pares pedidos ao Gemini que voltaram sem julgamento valido (inclui os omitidos).
    invalidos: int = 0
    # Motivo de parada antecipada: orcamento_usd, teto_itens, erro_api ou cancelado.
    parada: str | None = None


@dataclass
class _LoteGemini:
    status: str
    erro: str | None
    aceitos: int
    invalidos: int
    tokens_entrada: int | None
    tokens_saida: int | None
    custo_usd: float


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


def pendentes(
    prompt_versao: int,
    limite: int | None = None,
    *,
    ordem: str = "desc",
    apos: tuple | None = None,
    anos: Sequence[int] | None = None,
    max_tentativas: int | None = None,
) -> list[dict]:
    """Pares (artigo, ticker) com vinculo e sem julgamento para esta versao.

    `apos` e o cursor keyset (published_at, id, ticker) do ultimo par da pagina anterior.
    `anos` filtra pelo ano de publicacao em UTC. `max_tentativas` deixa de fora os pares
    que ja esgotaram as tentativas.
    """
    trecho = func.substr(ArticleBody.body_text, 1, _TRECHO)
    tentativas = func.coalesce(StagingJuizaTentativa.tentativas, 0)
    consulta = _juntar(select(
        Article.id, ArticleTicker.ticker, Article.title, Article.source_domain,
        Article.origem, Article.lang, Article.published_at,
        tentativas.label("tentativas"), trecho.label("trecho"),
    ), prompt_versao).where(StagingNoticiaJulgamento.article_id.is_(None))
    if anos:
        janelas = [and_(
            Article.published_at >= dt.datetime(ano, 1, 1, tzinfo=dt.UTC),
            Article.published_at < dt.datetime(ano + 1, 1, 1, tzinfo=dt.UTC),
        ) for ano in sorted(set(anos))]
        consulta = consulta.where(or_(*janelas))
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
    return [{**dict(linha), "trecho": linha["trecho"] or ""} for linha in linhas]


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


def julgar_pendentes(
    *,
    limite_itens: int | None = None,
    cliente: Any = None,
    jev: Any = None,
    observador: Any = None,
    lote: int | None = None,
    modo: str = "diario",
    anos: Sequence[int] | None = None,
    orcamento_usd: float | None = None,
) -> ResultadoJuiza:
    """Julga os pares pendentes, pagina a pagina. `modo` e "diario" (producao) ou "backlog".

    Teto de itens: no diario, `limite_itens` ou `juiza_max_itens_dia`; no backlog,
    `limite_itens` (None = sem teto). A regra barata nao conta para o teto.
    Teto de US$: `teto_usd(modo, orcamento_usd)`, conferido antes de cada lote pago.
    Jev: o `jev` injetado, ou um JevClient real se `jev_enabled` e `jev_api_key`. So roda com
    `jev_enabled`. Sem cliente pago (nem Jev nem Gemini), so a primeira pagina de regra roda.
    """
    if modo not in ("diario", "backlog"):
        raise ValueError(f"modo desconhecido: {modo!r}")
    settings = get_settings()
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
    res = ResultadoJuiza(pendentes=0, regra=0, jev=0, gemini=0, erros=0, runs=0,
                         tokens_entrada=0, tokens_saida=0)
    jev_proprio = None
    if jev is None and settings.jev_enabled and settings.jev_api_key:
        from tcc_scraper.llm.jev_client import JevClient  # lazy: so existe com o Jev ligado
        jev = jev_proprio = JevClient(settings.jev_api_key, settings.jev_modelo,
                                      settings.jev_timeout_s,
                                      concorrencia=settings.jev_concorrencia)
    jev_ativo = settings.jev_enabled and jev is not None
    gemini_ok = cliente is not None or bool(settings.gemini_api_key)
    pago_ok = jev_ativo or gemini_ok
    teto: float | None = None  # calculado na primeira vez que um lote pago for necessario
    ultimo_custo = 0.0
    # Pares que gastaram LLM. Com Jev, o par conta no lote do Jev e nao de novo no Gemini.
    pagos = 0
    erros_api_seguidos = 0
    cursor: tuple | None = None

    try:
        while True:
            itens = pendentes(VERSAO, settings.juiza_pagina, ordem=ordem, apos=cursor, anos=anos,
                              max_tentativas=settings.juiza_max_tentativas)
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

            # 2) Jev e Gemini. O teto de itens corta a pagina antes de qualquer chamada paga.
            restantes_llm = restantes
            if restantes and pago_ok:
                if teto_itens is not None:
                    restante_itens = teto_itens - pagos
                    if restante_itens <= 0:
                        res.parada = "teto_itens"
                        break
                    restantes_llm = restantes[:restante_itens]
                if teto is None:
                    teto = teto_usd(modo, orcamento_usd, settings)

                # 2a) Jev: o que ele nao aceita segue para o Gemini.
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
                        aceitos, sobra, custo = _julgar_lote_jev(
                            lote_itens, jev, decide=settings.juiza_jev_decide, settings=settings,
                            universo=universo, finalidade=finalidade,
                        )
                        ultimo_custo = custo
                        res.custo_usd += custo
                        res.runs += 1
                        res.jev += len(aceitos)
                        restantes_llm.extend(sobra)
                    if res.parada:
                        break

                # 2b) Gemini.
                if restantes_llm and gemini_ok:
                    if cliente is None:
                        from tcc_scraper.llm.gemini_client import GeminiClient  # lazy: extra `llm`
                        cliente = GeminiClient(settings.gemini_api_key, settings.juiza_modelo,
                                               settings.llm_timeout_s, settings.llm_max_attempts)
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
                        lote_res = _julgar_lote_gemini(lote_itens, cliente,
                                                       modelo=settings.juiza_modelo,
                                                       universo=universo, finalidade=finalidade,
                                                       settings=settings)
                        ultimo_custo = lote_res.custo_usd
                        res.custo_usd += lote_res.custo_usd
                        res.runs += 1
                        res.gemini += lote_res.aceitos
                        res.invalidos += lote_res.invalidos
                        res.tokens_entrada += lote_res.tokens_entrada or 0
                        res.tokens_saida += lote_res.tokens_saida or 0
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

            if restantes_llm and not gemini_ok:
                logger.warning("juiza: GEMINI_API_KEY ausente; %d itens seguem pendentes",
                               len(restantes_llm))
            if res.parada:
                break
            if not pago_ok or len(itens) < settings.juiza_pagina:
                break
            ultimo = itens[-1]
            cursor = (ultimo["published_at"], ultimo["id"], ultimo["ticker"])
    finally:
        if jev_proprio is not None:
            jev_proprio.close()
    return res


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


def _gravar_run(s, **campos) -> int:
    """Grava uma run da juiza (Gemini ou Jev), append-only, e devolve o id."""
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


def _aceita_jev(julgamento: JulgamentoItem, origem: str, *, decide: bool, settings: Any) -> bool:
    """Criterio de aceite do julgamento do Jev.

    Decidindo: confianca >= `juiza_confianca_minima`. Triagem: so descarte (o que
    `aprovado_para_entrega` reprova com o filtro da juiza) com confianca >=
    `juiza_confianca_triagem`. O resto vai ao Gemini.
    """
    if decide:
        return julgamento.confianca >= settings.juiza_confianca_minima
    if julgamento.confianca < settings.juiza_confianca_triagem:
        return False
    dados = julgamento.model_dump(mode="json")
    par = Julgamento(
        papel=dados["papel"], tipo_conteudo=dados["tipo_conteudo"], evento=dados["evento"],
        qualidade=dados["qualidade"], tickers_adicionais=tuple(dados["tickers_adicionais"]),
    )
    return not aprovado_para_entrega(par, origem, exigir_juiza=True)


def _julgar_lote_jev(lote_itens: list[dict], jev: Any, *, decide: bool, settings: Any,
                     universo: set[str], finalidade: str
                     ) -> tuple[list[dict], list[dict], float]:
    """Um lote no Jev. Devolve (linhas de staging aceitas, sobra para o Gemini, custo em US$).

    A run e gravada com o prompt de `prompt_jev()`, quando a chamada voltou (ok ou parcial) ou
    falhou (erro_api). Falha de um item vai para a sobra, sem tentativa. Excecao do cliente
    (lote inteiro falho) manda o lote todo ao Gemini, tambem sem tentativa.
    """
    modelo = f"{settings.jev_modelo}/q{JEV_PERGUNTAS_VERSAO}"
    pedidos = {(str(i["id"]), i["ticker"]): i for i in lote_itens}
    with session_scope() as s:
        prompt_id = garantir_prompt(s, prompt_jev())
    iniciado = dt.datetime.now(dt.UTC)
    inicio = time.monotonic()
    try:
        resposta = jev.classificar_lote(lote_itens)
    except Exception as exc:  # noqa: BLE001 -- Jev falho nao derruba a execucao: vai ao Gemini
        logger.warning("juiza: Jev falhou, lote segue para o Gemini: %s", exc)
        with session_scope() as s:
            _gravar_run(
                s, prompt_id=prompt_id, modelo=modelo, n_itens=len(lote_itens),
                tokens_entrada=None, tokens_saida=None, custo_usd=0.0,
                latencia_ms=int((time.monotonic() - inicio) * 1000), status="erro_api",
                finalidade=finalidade, erro=f"{type(exc).__name__}: {exc}"[:4000],
                resposta_bruta=None, iniciado_em=iniciado,
                finalizado_em=dt.datetime.now(dt.UTC),
            )
        return [], list(lote_itens), 0.0

    finalizado = dt.datetime.now(dt.UTC)
    custo = custo_jev(resposta.tokens_entrada, settings)
    aceitos: list[tuple[dict, JulgamentoItem]] = []
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
        if not _aceita_jev(julgamento, origem, decide=decide, settings=settings):
            continue
        vistos.add(chave)
        aceitos.append((pedidos[chave], julgamento))
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
            _linha_staging(item, julgamento.model_dump(mode="json"), modelo=modelo,
                           run_id=run_id, universo=universo, agora=finalizado)
            for item, julgamento in aceitos
        ]
        _gravar_staging(s, linhas)
    return linhas, sobra, custo


def _julgar_lote_gemini(lote_itens: list[dict], cliente: Any, *, modelo: str,
                        universo: set[str], finalidade: str, settings: Any) -> _LoteGemini:
    """Uma chamada ao Gemini para o lote. Run, julgamentos e tentativas vao na mesma transacao.

    Status: ok (todos julgados), parcial (alguns faltam ou sao invalidos), erro_validacao
    (nenhum aceito), erro_json, erro_api. Em erro_api nao ha tentativa: a falha foi da API,
    nao do par.
    """
    prompt = renderizar_prompt_juiza([_payload(i) for i in lote_itens])
    with session_scope() as s:
        prompt_id = garantir_prompt(s, prompt)

    pedidos = {(str(i["id"]), i["ticker"]): i for i in lote_itens}
    iniciado = dt.datetime.now(dt.UTC)
    inicio = time.monotonic()
    resposta, status, erro = None, "ok", None
    julgados: list[JulgamentoItem] = []
    try:
        resposta = cliente.classificar(prompt.texto, prompt.schema)
    except Exception as exc:  # noqa: BLE001 -- falha da API vira run erro_api; o lote segue pendente
        status, erro = "erro_api", f"{type(exc).__name__}: {exc}"[:4000]
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
        custo = custo_gemini(resposta.tokens_entrada, resposta.tokens_saida,
                             getattr(resposta, "tokens_pensamento", None), settings)

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
    return _LoteGemini(
        status=status, erro=erro, aceitos=len(linhas), invalidos=faltam,
        tokens_entrada=resposta.tokens_entrada if resposta else None,
        tokens_saida=resposta.tokens_saida if resposta else None,
        custo_usd=custo,
    )
