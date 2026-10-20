"""Contrato da resposta estruturada da juiza (revisor de noticias ja coletadas, sem busca na web).

Mesmo padrao de llm/schema.py: nomes pt-BR snake_case, extra="ignore" e enum invalido FALHA
de proposito -- e o sinal de que prompt ou modelo mudou.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from tcc_scraper.llm.schema import _Item, schema_para_gemini


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


def schema_juiza() -> dict:
    """JSON schema do lote da juiza, ajustado ao subconjunto aceito pelo Gemini."""
    return schema_para_gemini(LoteJulgamento)
