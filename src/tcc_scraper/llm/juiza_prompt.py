"""Renderiza o prompt da juiza a partir do template versionado (juiza_v{N}.txt).

Mesmo contrato de llm/prompt.py: sha256 = template + schema, estavel por versao. Mudou o texto?
Suba VERSAO e crie juiza_v{N+1}.txt -- nunca edite a versao antiga no lugar.
"""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from string import Template

from tcc_scraper.config import APELIDOS, IBOV_SECTORS, NOMES_EMPRESA, universo_coleta
from tcc_scraper.llm.juiza_schema import Evento, Papel, TipoConteudo, schema_juiza
from tcc_scraper.llm.prompt import PromptRenderizado

NOME = "juiza"
VERSAO = 1


def _carregar_template(versao: int = VERSAO) -> str:
    arquivo = files("tcc_scraper.llm.prompts").joinpath(f"{NOME}_v{versao}.txt")
    return arquivo.read_text(encoding="utf-8")


def universo_juiza() -> str:
    """Uma linha por ticker do universo de coleta, em ordem fixa.

    Formato: 'TICKER: Nome (setor) — apelidos: a, b'. Sem setor cadastrado (ticker deslistado)
    entra como 'outro'.
    """
    linhas = []
    for ticker in universo_coleta():
        linha = f"{ticker}: {NOMES_EMPRESA[ticker]} ({IBOV_SECTORS.get(ticker, 'outro')})"
        if APELIDOS.get(ticker):
            linha += f" — apelidos: {', '.join(APELIDOS[ticker])}"
        linhas.append(linha)
    return "\n".join(linhas)


def renderizar_prompt_juiza(itens: list[dict]) -> PromptRenderizado:
    """itens: dicts com id, ticker, empresa, titulo, fonte e trecho (serializados como estao)."""
    template = _carregar_template()
    schema = schema_juiza()
    texto = Template(template).substitute(
        universo=universo_juiza(),
        itens=json.dumps(itens, ensure_ascii=False, indent=2),
        papeis=", ".join(p.value for p in Papel),
        tipos=", ".join(t.value for t in TipoConteudo),
        eventos=", ".join(e.value for e in Evento),
    )
    return PromptRenderizado(
        nome=NOME,
        versao=VERSAO,
        texto=texto,
        template=template,
        # Hash do template E do schema: mudar qualquer um dos dois exige subir VERSAO.
        sha256=hashlib.sha256(
            (template + json.dumps(schema, sort_keys=True)).encode("utf-8")
        ).hexdigest(),
        schema=schema,
    )
