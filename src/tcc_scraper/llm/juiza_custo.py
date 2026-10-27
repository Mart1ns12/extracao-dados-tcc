"""Custo em US$ da juiza e tetos de gasto.

Cada run paga grava `custo_usd` e `finalidade` em `v1-raw_llm_juiza_runs`. O teto e
calculado a partir dessas linhas: backlog soma tudo com finalidade `backlog`; a juiza diaria
soma `producao` desde o dia 1 do mes (fuso `daily_tz`). `concordancia` nao entra em nenhum teto.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from tcc_scraper.db.models_fontes import RawLlmJuizaRun
from tcc_scraper.db.session import session_scope

_MILHAO = 1_000_000

# US$/MTok do provedor da juiza: (entrada, cache, saida)
_PRECOS_PADRAO = {"openai": (0.20, 0.02, 1.20), "gemini": (0.25, 0.25, 1.50)}


def precos_llm(s) -> tuple[float, float, float]:
    """US$/MTok do provedor da juiza; campo do .env (nao None) sobrepoe a tabela."""
    pe, pc, ps = _PRECOS_PADRAO[getattr(s, "juiza_provedor", "gemini")]

    def _ou(campo, padrao):
        valor = getattr(s, campo, None)
        return padrao if valor is None else valor

    return (_ou("juiza_preco_entrada_usd_mtok", pe), _ou("juiza_preco_cache_usd_mtok", pc),
            _ou("juiza_preco_saida_usd_mtok", ps))


def custo_llm(tokens_entrada: int | None, tokens_saida: int | None, *,
              tokens_cache: int | None = None, tokens_pensamento: int | None = None,
              s) -> float:
    """Custo de uma chamada da LLM da juiza em US$.

    Cache e cobrado a parte (sai da entrada). Pensamento so existe no Gemini, onde vem fora
    da saida; a OpenAI ja inclui o raciocinio em output_tokens (nao passe reasoning_tokens
    aqui). None conta zero.
    """
    entrada = tokens_entrada or 0
    cache = min(tokens_cache or 0, entrada)
    saida = (tokens_saida or 0) + (tokens_pensamento or 0)
    pe, pc, ps = precos_llm(s)
    return ((entrada - cache) * pe + cache * pc + saida * ps) / _MILHAO


def custo_resposta(resposta, s) -> float:
    """Custo de uma resposta da LLM, lendo cache e pensamento quando o objeto os tiver."""
    return custo_llm(resposta.tokens_entrada, resposta.tokens_saida,
                     tokens_cache=getattr(resposta, "tokens_cache", None),
                     tokens_pensamento=getattr(resposta, "tokens_pensamento", None), s=s)


def custo_jev(tokens_entrada: int | None, s) -> float:
    """Custo de uma chamada ao Jev. So a entrada tem preco; None conta zero."""
    return (tokens_entrada or 0) * s.jev_preco_entrada_usd_mtok / _MILHAO


def inicio_mes_utc(agora: dt.datetime, tz: str) -> dt.datetime:
    """Primeiro instante do mes de `agora`, no fuso `tz`, devolvido em UTC."""
    local = agora.astimezone(ZoneInfo(tz))
    inicio = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return inicio.astimezone(dt.UTC)


def gasto_usd(finalidade: str, desde: dt.datetime | None = None) -> float:
    """Soma de `custo_usd` das runs com a finalidade dada (a partir de `desde`, se houver)."""
    consulta = select(func.coalesce(func.sum(RawLlmJuizaRun.custo_usd), 0.0)).where(
        RawLlmJuizaRun.finalidade == finalidade)
    if desde is not None:
        consulta = consulta.where(RawLlmJuizaRun.iniciado_em >= desde)
    with session_scope() as sessao:
        return float(sessao.execute(consulta).scalar() or 0.0)


def teto_usd(modo: str, orcamento_usd: float | None, s) -> float:
    """Quanto ainda pode ser gasto nesta execucao, em US$ (nunca negativo).

    backlog: verba do backlog menos o que ja foi gasto com finalidade `backlog`.
    diario: teto mensal menos o gasto de `producao` desde o dia 1 do mes.
    Com `orcamento_usd`, vale o menor dos dois.
    """
    if modo == "backlog":
        restante = s.juiza_backlog_verba_usd - gasto_usd("backlog")
    elif modo == "diario":
        desde = inicio_mes_utc(dt.datetime.now(dt.UTC), s.daily_tz)
        restante = s.juiza_orcamento_mensal_usd - gasto_usd("producao", desde)
    else:
        raise ValueError(f"modo de teto desconhecido: {modo!r}")
    if orcamento_usd is not None:
        restante = min(restante, orcamento_usd)
    return max(0.0, restante)
