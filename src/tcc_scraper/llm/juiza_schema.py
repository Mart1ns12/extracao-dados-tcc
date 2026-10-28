"""Contrato da resposta estruturada da juiza (revisor de noticias ja coletadas, sem busca na web).

Mesmo padrao de llm/schema.py: nomes pt-BR snake_case, extra="ignore" e enum invalido FALHA
de proposito -- e o sinal de que prompt ou modelo mudou.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from tcc_scraper.llm.schema import _Item, schema_para_gemini, schema_para_openai


class Papel(StrEnum):
    protagonista = "protagonista"
    citada_com_fato = "citada_com_fato"
    mencao = "mencao"
    errada = "errada"
    matriz_estrangeira = "matriz_estrangeira"


class TipoConteudo(StrEnum):
    reportagem = "reportagem"
    opiniao = "opiniao"
    lista = "lista"
    agenda = "agenda"
    resumo_mercado = "resumo_mercado"
    podcast_video = "podcast_video"
    newsletter = "newsletter"
    patrocinado = "patrocinado"
    outro = "outro"


class Evento(StrEnum):
    resultado = "resultado"
    dividendo_jcp = "dividendo_jcp"
    fato_relevante = "fato_relevante"
    fusao_aquisicao = "fusao_aquisicao"
    guidance = "guidance"
    recomendacao = "recomendacao"
    regulatorio = "regulatorio"
    operacional = "operacional"
    governanca = "governanca"
    juridico = "juridico"
    mudanca_ticker = "mudanca_ticker"
    macro = "macro"
    outro = "outro"


class JulgamentoItem(_Item):
    id: str
    ticker: str
    papel: Papel
    tipo_conteudo: TipoConteudo
    evento: Evento
    tickers_adicionais: list[str] = Field(default_factory=list)
    qualidade: float = Field(ge=0, le=1)
    confianca: float = Field(ge=0, le=1)


class LoteJulgamento(_Item):
    itens: list[JulgamentoItem]


# Limites numericos que o structured output da Anthropic recusa (400 "For 'number' type,
# properties maximum, minimum are not supported", medido em 2026-10-28). A faixa continua
# validada pelo Pydantic na resposta.
_SEM_SUPORTE_ANTHROPIC = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"})


def _sem_limites(no: object) -> object:
    """Copia do schema sem as chaves de _SEM_SUPORTE_ANTHROPIC, em qualquer profundidade."""
    if isinstance(no, dict):
        return {k: _sem_limites(v) for k, v in no.items() if k not in _SEM_SUPORTE_ANTHROPIC}
    if isinstance(no, list):
        return [_sem_limites(v) for v in no]
    return no


def schema_juiza(provedor: str = "gemini") -> dict:
    """JSON schema do lote da juiza, ajustado ao subconjunto aceito pelo provedor.

    gemini: subconjunto aceito pelo Gemini. openai: formato estrito do Structured Outputs.
    anthropic: o mesmo do openai, so o objeto (sem envelope) e sem minimum/maximum.
    """
    if provedor == "anthropic":
        return _sem_limites(schema_para_openai(LoteJulgamento))
    if provedor == "openai":
        return schema_para_openai(LoteJulgamento)
    return schema_para_gemini(LoteJulgamento)
