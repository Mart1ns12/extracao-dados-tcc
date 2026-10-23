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


def custo_gemini(tokens_entrada: int | None, tokens_saida: int | None,
                 tokens_pensamento: int | None, s) -> float:
    """Custo de uma chamada ao Gemini. O pensamento e cobrado como saida; None conta zero."""
    saida = (tokens_saida or 0) + (tokens_pensamento or 0)
    entrada_usd = (tokens_entrada or 0) * s.juiza_preco_entrada_usd_mtok / _MILHAO
    saida_usd = saida * s.juiza_preco_saida_usd_mtok / _MILHAO
    return entrada_usd + saida_usd


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
