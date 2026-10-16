"""Do texto da API as linhas das tabelas staging.

Tres etapas separadas, cada uma com seu status em raw_llm_runs: json.loads (erro_json)
-> pydantic (erro_validacao) -> filtro de universo (so descarta e loga: um ticker
inventado nao invalida a analise inteira).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from tcc_scraper.config import IBOV_TICKERS
from tcc_scraper.db.models import canonical_url
from tcc_scraper.llm.schema import AnaliseDiaria

logger = logging.getLogger(__name__)
_CERCA = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_HOST_REDIRECT = "vertexaisearch.cloud.google.com"


def parse_resposta(texto: str) -> AnaliseDiaria:
    """json.loads + pydantic. Levanta json.JSONDecodeError ou pydantic.ValidationError."""
    return AnaliseDiaria.model_validate(json.loads(_CERCA.sub("", texto)))


def filtrar_universo(analise: AnaliseDiaria) -> AnaliseDiaria:
    def _ok(ticker: str, onde: str) -> bool:
        if ticker in IBOV_TICKERS:
            return True
        logger.warning("llm: ticker fora do universo descartado em %s: %s", onde, ticker)
        return False

    return analise.model_copy(update={
        "noticias_empresas": [
            n for n in analise.noticias_empresas if _ok(n.ticker, "noticias_empresas")
        ],
        "acoes_influenciadas": [
            a for a in analise.acoes_influenciadas if _ok(a.ticker, "acoes_influenciadas")
        ],
    })


def resolver_redirects(analise: AnaliseDiaria,
                       resolver: Callable[[str], str | None]) -> AnaliseDiaria:
    """Troca URLs vertexaisearch pela URL real. Resolvedor None mantem a original.

    Cache por URL: a mesma materia costuma aparecer em mais de um item e o resolvedor
    faz requisicao de rede.
    """
    cache: dict[str, str | None] = {}

    def _trocar(url: str | None) -> str | None:
        if not url or urlsplit(url).hostname != _HOST_REDIRECT:
            return url
        if url not in cache:
            cache[url] = resolver(url)
        return cache[url] or url

    return analise.model_copy(update={
        "noticias_empresas": [
            n.model_copy(update={"url": _trocar(n.url)}) for n in analise.noticias_empresas
        ],
        "noticias_macro": [
            m.model_copy(update={"url": _trocar(m.url)}) for m in analise.noticias_macro
        ],
    })


_EXT_IMAGEM = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif")


def normalizar_url(url: str | None) -> str | None:
    """canonical_url do projeto; devolve None quando a URL nao serve como fonte.

    None para: sem esquema http(s); link do Google que nao resolveu (expira); e URL de
    imagem. Medido em 2026-10-15: varios redirects do grounding apontavam para a foto da
    materia (wp-content/uploads/...jpg), as vezes de OUTRA materia, e duas noticias macro
    colidiam na mesma chave. Sem URL, item_hash cai para o titulo e nada se perde.
    """
    if not url or not url.strip().lower().startswith(("http://", "https://")):
        return None
    partes = urlsplit(url.strip())
    if partes.hostname == _HOST_REDIRECT or partes.path.lower().endswith(_EXT_IMAGEM):
        return None
    return canonical_url(url)


def item_hash(url: str | None, titulo: str) -> str:
    """Chave do item: sha256 de (URL canonica, titulo normalizado).

    So a URL nao basta: medido em 2026-10-15, o modelo cita o MESMO artigo para dois fatos
    macro distintos, e com chave so por URL um deles era sobrescrito. A staging e
    substituida por dia, entao a chave precisa ser unica dentro de uma resposta, nao
    estavel entre execucoes. Tracking na URL e caixa/espacos no titulo nao mudam a chave.
    """
    u = normalizar_url(url) or ""
    t = " ".join(titulo.lower().split())
    return hashlib.sha256(f"{u}|{t}".encode()).hexdigest()


@dataclass
class LinhasStaging:
    noticias_empresas: list[dict] = field(default_factory=list)
    noticias_macro: list[dict] = field(default_factory=list)
    acoes_sinais: list[dict] = field(default_factory=list)
    mercados: list[dict] = field(default_factory=list)

    def contagem(self) -> dict[str, int]:
        return {k: len(v) for k, v in vars(self).items()}


def mapear_linhas(analise: AnaliseDiaria, *, run_id: int, data_referencia: date,
                  pregao_referencia: date | None) -> LinhasStaging:
    base = {
        "run_id": run_id,
        "data_referencia": data_referencia,
        "pregao_referencia": pregao_referencia,
    }
    out = LinhasStaging()
    for n in analise.noticias_empresas:
        out.noticias_empresas.append({
            **base,
            "ticker": n.ticker,
            "item_hash": item_hash(n.url, n.titulo),
            "titulo": n.titulo,
            "resumo": n.resumo,
            "fonte": n.fonte,
            "url": normalizar_url(n.url),
            "publicado_em": n.publicado_em,
            "setor": n.setor.value,
            "sentimento": n.sentimento.value,
            "relevancia": n.relevancia,
        })
    for m in analise.noticias_macro:
        out.noticias_macro.append({
            **base,
            "item_hash": item_hash(m.url, m.titulo),
            "escopo": m.escopo.value,
            "titulo": m.titulo,
            "resumo": m.resumo,
            "fonte": m.fonte,
            "url": normalizar_url(m.url),
            "publicado_em": m.publicado_em,
            "commodities_afetadas": [c.value for c in m.commodities_afetadas],
            "direcao_commodity": m.direcao_commodity.value,
            "setores_afetados": [s.value for s in m.setores_afetados],
        })
    for a in analise.acoes_influenciadas:
        out.acoes_sinais.append({
            **base,
            "ticker": a.ticker,
            "direcao_esperada": a.direcao_esperada.value,
            "confianca": a.confianca,
            "motivo": a.motivo,
            "setor": a.setor.value,
            "noticias_relacionadas": a.noticias_relacionadas,
        })
    for k in analise.mercados_em_alta:
        out.mercados.append({
            **base,
            "nome": k.nome.strip()[:64],
            "tipo": k.tipo.value,
            "variacao_pct": k.variacao_pct,
            "motivo": k.motivo,
        })
    return out
