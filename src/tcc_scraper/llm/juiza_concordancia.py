"""Concordancia da juiza com o Gemini numa amostra reprodutivel (`judge-news --concordancia N`).

Mede, sem gravar julgamento: sorteia N pares (artigo, ticker) por estrato (ano de publicacao x
fonte; as `TOP_FONTES` mais frequentes e o balde "outras"), descarta o que a regra barata
resolve e julga o resto no Jev e no Gemini. O Gemini e a referencia. Cada chamada paga vira uma
run com `finalidade='concordancia'` e `custo_usd` preenchido; a run-resumo (`modelo='concordancia'`,
`custo_usd` nulo) leva em `resposta_bruta` o JSON {semente, alocacao, modelos, metricas, pares}.

Nada vai para staging nem para tentativas, e nenhum teto de custo e consultado. O comando so
imprime a decisao: quem liga `JUIZA_JEV_DECIDE` e o dono.

A amostra e reprodutivel: a ordem dentro de cada estrato e crc32(concat(id,'|',ticker,'|',semente)).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, tuple_

from tcc_scraper.config import APELIDOS, NOMES_EMPRESA, get_settings
from tcc_scraper.db.models import Article, ArticleBody, ArticleTicker
from tcc_scraper.db.session import session_scope
from tcc_scraper.entrega.filtros import Julgamento, aprovado_para_entrega
from tcc_scraper.llm.jev_client import JEV_PERGUNTAS_VERSAO, prompt_jev
from tcc_scraper.llm.job import garantir_prompt
from tcc_scraper.llm.juiza import _TRECHO, _fatias, _gravar_run, _payload, _validar_itens
from tcc_scraper.llm.juiza_custo import custo_gemini, custo_jev
from tcc_scraper.llm.juiza_prompt import renderizar_prompt_juiza
from tcc_scraper.llm.juiza_regras import nomes_por_ticker, regra_barata
from tcc_scraper.llm.juiza_schema import JulgamentoItem

FINALIDADE = "concordancia"
LIMIAR_DECISAO = 0.85
TOP_FONTES = 10
_OUTRAS = "outras"
_ORIGEM_IPE = "cvm_ipe"
_LOTE_AMOSTRA = 500


@dataclass
class ResultadoConcordancia:
    semente: int
    pedidos: int
    amostra: int
    descartados_regra: int
    alocacao: dict[tuple[int, str], int]
    metricas: dict[str, Any]
    pares: list[dict]
    custo_usd: float
    chamadas: int

    @property
    def decisao(self) -> str:
        return self.metricas["decisao"]

    def resumo(self) -> dict[str, object]:
        """Linhas da tabela impressa pela CLI."""
        m = self.metricas
        linhas: dict[str, object] = {
            "semente": self.semente,
            "pedidos": self.pedidos,
            "estratos": len(self.alocacao),
            "amostra": self.amostra,
            "descartados_regra": self.descartados_regra,
            "chamadas": self.chamadas,
            "custo_usd": round(self.custo_usd, 4),
            "pares_com_ambos": m["ambos"],
            "cobertura_jev": _fmt(m["cobertura_jev"]),
        }
        for nome in ("papel", "tipo_conteudo", "entrega"):
            bloco = m[nome]
            linhas[f"{nome}: concordancia"] = _fmt(bloco["concordancia"])
            linhas[f"{nome}: kappa"] = _fmt(bloco["kappa"])
            linhas[f"{nome}: n"] = bloco["n"]
            linhas[f"{nome}: concordancia (jev confiante)"] = _fmt(
                bloco["concordancia_confiantes"])
        linhas["limiar_decisao"] = m["limiar"]
        linhas["decisao"] = m["decisao"]
        return linhas


def _fmt(valor: object) -> object:
    return round(valor, 4) if isinstance(valor, float) else ("-" if valor is None else valor)


# --------------------------------------------------------------------------------------
# Alocacao por estrato
# --------------------------------------------------------------------------------------

def _top_fontes(contagens: dict[tuple[int, str], int], top: int) -> set[str]:
    """As `top` fontes com mais pares, somando todos os anos (empate: ordem alfabetica)."""
    totais: Counter = Counter()
    for (_, fonte), c in contagens.items():
        totais[fonte] += c
    ordenadas = sorted(totais.items(), key=lambda kv: (-kv[1], kv[0]))
    return {fonte for fonte, _ in ordenadas[:top]}


def alocar(contagens: dict[tuple[int, str], int], n: int,
           top_fontes: int = TOP_FONTES) -> dict[tuple[int, str], int]:
    """Quantos pares sortear em cada estrato (ano, fonte).

    `contagens` vem como {(ano, dominio): pares}. Fora das `top_fontes` mais frequentes, o
    dominio cai no estrato (ano, "outras"). A alocacao e proporcional ao tamanho do estrato,
    com minimo 1 por estrato nao vazio, ajustada pelo maior resto. Nunca passa do tamanho do
    estrato. Se n >= total, devolve tudo. Se ha mais estratos que n, ficam os n maiores com 1.
    """
    top = _top_fontes(contagens, top_fontes)
    estratos: Counter = Counter()
    for (ano, fonte), c in contagens.items():
        if c > 0:
            estratos[(ano, fonte if fonte in top else _OUTRAS)] += c
    total = sum(estratos.values())
    if n <= 0 or total == 0:
        return {}
    if n >= total:
        return dict(sorted(estratos.items()))
    chaves = sorted(estratos, key=lambda k: (-estratos[k], k))
    if len(chaves) > n:
        return {k: 1 for k in sorted(chaves[:n])}

    ideal = {k: n * estratos[k] / total for k in chaves}
    base = {k: min(estratos[k], max(1, math.floor(ideal[k]))) for k in chaves}
    # O minimo 1 pode estourar n: tira de quem tem mais que 1, o maior primeiro.
    while sum(base.values()) > n:
        candidatos = [k for k in chaves if base[k] > 1]
        k = max(candidatos, key=lambda c: (base[c], c))
        base[k] -= 1
    # Sobra: quem ficou mais abaixo do ideal ganha o proximo par, sem passar do estrato.
    while sum(base.values()) < n:
        candidatos = [k for k in chaves if base[k] < estratos[k]]
        if not candidatos:
            break
        k = max(candidatos, key=lambda c: (ideal[c] - base[c], estratos[c], c))
        base[k] += 1
    return {k: base[k] for k in chaves if base[k] > 0}


# --------------------------------------------------------------------------------------
# Metricas
# --------------------------------------------------------------------------------------

def kappa_cohen(a: list, b: list) -> float | None:
    """Kappa de Cohen entre dois rotuladores. None sem pares."""
    if len(a) != len(b):
        raise ValueError("listas de tamanhos diferentes")
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if pe == 1:
        return 1.0 if po == 1 else 0.0
    return (po - pe) / (1 - pe)


def _entrega(dados: dict, origem: str) -> bool:
    par = Julgamento(
        papel=dados["papel"], tipo_conteudo=dados["tipo_conteudo"], evento=dados["evento"],
        qualidade=dados["qualidade"], tickers_adicionais=tuple(dados["tickers_adicionais"]),
    )
    return aprovado_para_entrega(par, origem, exigir_juiza=True)


_EXTRATORES = {
    "papel": lambda p: (p["jev"]["papel"], p["gemini"]["papel"]),
    "tipo_conteudo": lambda p: (p["jev"]["tipo_conteudo"], p["gemini"]["tipo_conteudo"]),
    "entrega": lambda p: (_entrega(p["jev"], p["origem"]), _entrega(p["gemini"], p["origem"])),
}


def _rotulos(pares: list[dict], extrator) -> tuple[list, list]:
    extraidos = [extrator(p) for p in pares]
    return [a for a, _ in extraidos], [b for _, b in extraidos]


def _taxa(a: list, b: list) -> float | None:
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a) if a else None


def metricas(pares: list[dict], *, confianca_minima: float) -> dict[str, Any]:
    """Concordancia, kappa e n para papel, tipo_conteudo e entrega, e a decisao.

    Cada par: {"origem", "gemini": JulgamentoItem em dict ou None, "jev": idem}. Os pares com
    os dois julgamentos entram na concordancia. Os "confiantes" sao os em que o Jev tem
    confianca >= `confianca_minima`, ou seja, os que o Jev decidiria. A cobertura e a fracao de
    todos os pares com o Jev confiante. Decisao: "jev_decide" se o minimo entre a concordancia de
    papel e a de tipo_conteudo for >= LIMIAR_DECISAO; senao "gemini_decide".
    """
    ambos = [p for p in pares if p["gemini"] is not None and p["jev"] is not None]
    confiantes = [p for p in ambos if p["jev"]["confianca"] >= confianca_minima]
    cobertura = sum(
        p["jev"] is not None and p["jev"]["confianca"] >= confianca_minima for p in pares
    )
    saida: dict[str, Any] = {
        "pares": len(pares),
        "com_gemini": sum(p["gemini"] is not None for p in pares),
        "com_jev": sum(p["jev"] is not None for p in pares),
        "ambos": len(ambos),
        "cobertura_jev": cobertura / len(pares) if pares else None,
    }
    for nome, extrator in _EXTRATORES.items():
        a, b = _rotulos(ambos, extrator)
        ac, bc = _rotulos(confiantes, extrator)
        saida[nome] = {
            "n": len(a),
            "concordancia": _taxa(a, b),
            "kappa": kappa_cohen(a, b),
            "n_confiantes": len(ac),
            "concordancia_confiantes": _taxa(ac, bc),
            "kappa_confiantes": kappa_cohen(ac, bc),
        }
    conc = [saida[c]["concordancia"] for c in ("papel", "tipo_conteudo")]
    minimo = None if any(c is None for c in conc) else min(conc)
    saida["limiar"] = LIMIAR_DECISAO
    saida["decisao"] = (
        "jev_decide" if minimo is not None and minimo >= LIMIAR_DECISAO else "gemini_decide"
    )
    return saida


# --------------------------------------------------------------------------------------
# SQL: populacao e amostra (reprodutivel por crc32)
# --------------------------------------------------------------------------------------

def _inicio_ano(ano: int) -> dt.datetime:
    return dt.datetime(ano, 1, 1, tzinfo=dt.UTC)


def _consulta_contagens():
    """(ano UTC, dominio) -> pares, fora as cvm_ipe. Mesmo recorte de `_consulta_amostra`."""
    ano = func.year(Article.published_at)
    fonte = func.coalesce(Article.source_domain, "")
    return (
        select(ano.label("ano"), fonte.label("fonte"), func.count().label("n"))
        .select_from(Article)
        .join(ArticleTicker, ArticleTicker.article_id == Article.id)
        .where(Article.origem != _ORIGEM_IPE)
        .group_by(ano, fonte)
    )


def _consulta_amostra(ano: int, fonte: str, top: set[str], semente: int,
                      apos: tuple | None = None):
    """Pares de um estrato, em ordem de crc32 com a semente, a partir do cursor `apos`."""
    h = func.crc32(func.concat(Article.id, "|", ArticleTicker.ticker, "|", semente))
    dominio = func.coalesce(Article.source_domain, "")
    filtro_fonte = dominio.notin_(sorted(top)) if fonte == _OUTRAS else dominio == fonte
    trecho = func.substr(ArticleBody.body_text, 1, _TRECHO)
    consulta = (
        select(
            h.label("h"), Article.id, ArticleTicker.ticker, Article.title,
            Article.source_domain, Article.origem, Article.lang, Article.published_at,
            trecho.label("trecho"),
        )
        .select_from(Article)
        .join(ArticleTicker, ArticleTicker.article_id == Article.id)
        .outerjoin(ArticleBody, ArticleBody.article_id == Article.id)
        .where(
            Article.origem != _ORIGEM_IPE,
            Article.published_at >= _inicio_ano(ano),
            Article.published_at < _inicio_ano(ano + 1),
            filtro_fonte,
        )
    )
    if apos is not None:
        consulta = consulta.where(tuple_(h, Article.id, ArticleTicker.ticker) > tuple(apos))
    return consulta.order_by(h, Article.id, ArticleTicker.ticker).limit(_LOTE_AMOSTRA)


def _populacao() -> dict[tuple[int, str], int]:
    with session_scope() as s:
        linhas = s.execute(_consulta_contagens()).all()
    return {(int(linha.ano), str(linha.fonte)): int(linha.n) for linha in linhas}


def _amostrar_estrato(ano: int, fonte: str, top: set[str], semente: int, k: int,
                      nomes: dict[str, list[str]]) -> tuple[list[dict], int]:
    """Ate `k` pares do estrato que a regra barata nao resolve. Devolve (pares, descartados)."""
    coletados: list[dict] = []
    descartados = 0
    apos: tuple | None = None
    while len(coletados) < k:
        with session_scope() as s:
            linhas = [dict(linha) for linha in s.execute(
                _consulta_amostra(ano, fonte, top, semente, apos)).mappings().all()]
        for linha in linhas:
            if len(coletados) == k:
                break
            item = {chave: valor for chave, valor in linha.items() if chave != "h"}
            item["trecho"] = item["trecho"] or ""
            if regra_barata(item, item["ticker"], nomes) is not None:
                descartados += 1
            else:
                coletados.append(item)
        if len(coletados) >= k or len(linhas) < _LOTE_AMOSTRA:
            break
        ultimo = linhas[-1]
        apos = (ultimo["h"], ultimo["id"], ultimo["ticker"])
    return coletados, descartados


# --------------------------------------------------------------------------------------
# Chamadas pagas (sem staging, sem tentativas)
# --------------------------------------------------------------------------------------

def _julgar_jev(lote: list[dict], jev: Any, settings: Any, pares: dict) -> float:
    """Um lote no Jev. Preenche `pares[chave]["jev"]`. Devolve o custo em US$."""
    modelo = f"{settings.jev_modelo}/q{JEV_PERGUNTAS_VERSAO}"
    with session_scope() as s:
        prompt_id = garantir_prompt(s, prompt_jev())
    iniciado = dt.datetime.now(dt.UTC)
    inicio = time.monotonic()
    try:
        resposta = jev.classificar_lote(lote)
    except Exception as exc:  # noqa: BLE001 -- falha do Jev nao derruba a medicao
        with session_scope() as s:
            _gravar_run(
                s, prompt_id=prompt_id, modelo=modelo, n_itens=len(lote),
                tokens_entrada=None, tokens_saida=None, custo_usd=0.0,
                latencia_ms=int((time.monotonic() - inicio) * 1000), status="erro_api",
                finalidade=FINALIDADE, erro=f"{type(exc).__name__}: {exc}"[:4000],
                resposta_bruta=None, iniciado_em=iniciado,
                finalizado_em=dt.datetime.now(dt.UTC),
            )
        return 0.0

    finalizado = dt.datetime.now(dt.UTC)
    chaves = {(str(i["id"]), i["ticker"]) for i in lote}
    for bruto in resposta.itens:
        if not isinstance(bruto, dict):
            continue
        try:
            julgamento = JulgamentoItem.model_validate({**bruto, "id": str(bruto.get("id"))})
        except ValidationError:
            continue
        chave = (julgamento.id, julgamento.ticker)
        if chave in chaves and pares[chave]["jev"] is None:
            pares[chave]["jev"] = julgamento.model_dump(mode="json")
    erro = "; ".join(f"{e['id']}/{e['ticker']}: {e['erro']}"
                     for e in resposta.erros)[:4000] or None
    bruta = json.dumps({"bruto": resposta.bruto, "erros": resposta.erros}, ensure_ascii=False)
    custo = custo_jev(resposta.tokens_entrada, settings)
    with session_scope() as s:
        _gravar_run(
            s, prompt_id=prompt_id, modelo=modelo, n_itens=len(lote),
            tokens_entrada=resposta.tokens_entrada, tokens_saida=None, custo_usd=custo,
            latencia_ms=resposta.latencia_ms, status="parcial" if resposta.erros else "ok",
            finalidade=FINALIDADE, erro=erro, resposta_bruta=bruta,
            iniciado_em=iniciado, finalizado_em=finalizado,
        )
    return custo


def _julgar_gemini(lote: list[dict], cliente: Any, settings: Any, pares: dict) -> float:
    """Um lote no Gemini (referencia). Preenche `pares[chave]["gemini"]`. Custo em US$."""
    prompt = renderizar_prompt_juiza([_payload(i) for i in lote])
    with session_scope() as s:
        prompt_id = garantir_prompt(s, prompt)
    chaves = {(str(i["id"]), i["ticker"]) for i in lote}
    iniciado = dt.datetime.now(dt.UTC)
    inicio = time.monotonic()
    resposta, status, erro = None, "ok", None
    julgados: list[JulgamentoItem] = []
    try:
        resposta = cliente.classificar(prompt.texto, prompt.schema)
    except Exception as exc:  # noqa: BLE001 -- falha da API vira run erro_api
        status, erro = "erro_api", f"{type(exc).__name__}: {exc}"[:4000]
    if resposta is not None:
        try:
            julgados, erros = _validar_itens(resposta.texto, chaves)
        except json.JSONDecodeError as exc:
            status, erro = "erro_json", str(exc)[:4000]
        except ValueError as exc:
            status, erro = "erro_validacao", str(exc)[:4000]
        else:
            faltam = len(chaves) - len(julgados)
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
    with session_scope() as s:
        _gravar_run(
            s, prompt_id=prompt_id,
            modelo=resposta.modelo if resposta else settings.juiza_modelo,
            n_itens=len(lote),
            tokens_entrada=resposta.tokens_entrada if resposta else None,
            tokens_saida=resposta.tokens_saida if resposta else None,
            custo_usd=custo,
            latencia_ms=int((time.monotonic() - inicio) * 1000),
            status=status, finalidade=FINALIDADE, erro=erro,
            resposta_bruta=resposta.texto if resposta else None,
            iniciado_em=iniciado, finalizado_em=finalizado,
        )
    for julgamento in julgados:
        pares[(julgamento.id, julgamento.ticker)]["gemini"] = julgamento.model_dump(mode="json")
    return custo


def _gravar_resumo(*, semente: int, alocacao: dict, met: dict, pares: list[dict],
                   amostra: int, custo_ms: int, settings: Any, iniciado: dt.datetime) -> None:
    """Run-resumo da concordancia: sem custo, JSON em resposta_bruta."""
    corpo = {
        "semente": semente,
        "alocacao": {f"{ano}|{fonte}": k for (ano, fonte), k in alocacao.items()},
        "modelos": {"gemini": settings.juiza_modelo,
                    "jev": f"{settings.jev_modelo}/q{JEV_PERGUNTAS_VERSAO}"},
        "metricas": met,
        "pares": pares,
    }
    with session_scope() as s:
        prompt_id = garantir_prompt(s, renderizar_prompt_juiza([]))
        _gravar_run(
            s, prompt_id=prompt_id, modelo="concordancia", n_itens=amostra,
            tokens_entrada=None, tokens_saida=None, custo_usd=None, latencia_ms=custo_ms,
            status="ok", finalidade=FINALIDADE, erro=None,
            resposta_bruta=json.dumps(corpo, ensure_ascii=False),
            iniciado_em=iniciado, finalizado_em=dt.datetime.now(dt.UTC),
        )


# --------------------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------------------

def medir_concordancia(n: int, *, semente: int = 42, cliente: Any = None,
                       jev: Any = None) -> ResultadoConcordancia:
    """Julga N pares no Jev e no Gemini e devolve a concordancia e a decisao.

    Exige o Jev (`jev` injetado, ou JEV_ENABLED + JEV_API_KEY) e o Gemini (`cliente`, ou
    GEMINI_API_KEY). Falha antes de qualquer chamada se faltar um deles.
    """
    if n <= 0:
        raise ValueError("N precisa ser positivo")
    settings = get_settings()
    jev_proprio = None
    if jev is None:
        if not (settings.jev_enabled and settings.jev_api_key):
            raise RuntimeError("concordancia exige o Jev: ligue JEV_ENABLED e defina JEV_API_KEY")
        from tcc_scraper.llm.jev_client import JevClient  # lazy: so existe com o Jev ligado
        jev = jev_proprio = JevClient(settings.jev_api_key, settings.jev_modelo,
                                      settings.jev_timeout_s,
                                      concorrencia=settings.jev_concorrencia)
    if cliente is None:
        if not settings.gemini_api_key:
            raise RuntimeError("concordancia exige o Gemini: defina GEMINI_API_KEY")
        from tcc_scraper.llm.gemini_client import GeminiClient  # lazy: extra `llm`
        cliente = GeminiClient(settings.gemini_api_key, settings.juiza_modelo,
                               settings.llm_timeout_s, settings.llm_max_attempts)
    try:
        return _medir(n, semente, settings, cliente, jev)
    finally:
        if jev_proprio is not None:
            jev_proprio.close()


def _medir(n: int, semente: int, settings: Any, cliente: Any, jev: Any) -> ResultadoConcordancia:
    iniciado = dt.datetime.now(dt.UTC)
    inicio = time.monotonic()
    nomes = nomes_por_ticker(NOMES_EMPRESA, APELIDOS)
    contagens = _populacao()
    alocacao = alocar(contagens, n)
    top = _top_fontes(contagens, TOP_FONTES)
    amostra: list[dict] = []
    descartados = 0
    for (ano, fonte), k in alocacao.items():
        itens, desc = _amostrar_estrato(ano, fonte, top, semente, k, nomes)
        amostra.extend(itens)
        descartados += desc

    pares = {
        (str(i["id"]), i["ticker"]): {
            "id": str(i["id"]), "ticker": i["ticker"], "origem": i.get("origem") or "",
            "gemini": None, "jev": None,
        }
        for i in amostra
    }
    custo = 0.0
    chamadas = 0
    for lote in _fatias(amostra, settings.jev_lote):
        custo += _julgar_jev(lote, jev, settings, pares)
        chamadas += 1
    for lote in _fatias(amostra, settings.juiza_lote):
        custo += _julgar_gemini(lote, cliente, settings, pares)
        chamadas += 1

    lista = list(pares.values())
    met = metricas(lista, confianca_minima=settings.juiza_confianca_minima)
    _gravar_resumo(semente=semente, alocacao=alocacao, met=met, pares=lista,
                   amostra=len(amostra), custo_ms=int((time.monotonic() - inicio) * 1000),
                   settings=settings, iniciado=iniciado)
    return ResultadoConcordancia(
        semente=semente, pedidos=n, amostra=len(amostra), descartados_regra=descartados,
        alocacao=alocacao, metricas=met, pares=lista, custo_usd=custo, chamadas=chamadas,
    )
