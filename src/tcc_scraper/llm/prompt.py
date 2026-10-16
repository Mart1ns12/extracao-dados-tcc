"""Renderiza o prompt da analise diaria a partir do template versionado.

O template vive em codigo e e gravado em raw_llm_prompts por (nome, versao): cada run
referencia o prompt exato que recebeu. Mudou o texto? Suba VERSAO -- o job recusa um
template cujo sha256 difere do que ja esta no banco para a mesma versao.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from string import Template

from tcc_scraper.config import IBOV_SECTORS, IBOV_TICKERS
from tcc_scraper.llm.schema import AnaliseDiaria, Commodity, Setor, schema_para_gemini

NOME = "analise_diaria"
VERSAO = 3


@dataclass(frozen=True)
class PromptRenderizado:
    nome: str
    versao: int
    texto: str        # o que vai para a API
    template: str     # arquivo sem substituir
    sha256: str       # template + schema: estavel por versao
    schema: dict      # JSON schema enviado junto


def _carregar_template(versao: int = VERSAO) -> str:
    arquivo = files("tcc_scraper.llm.prompts").joinpath(f"{NOME}_v{versao}.txt")
    return arquivo.read_text(encoding="utf-8")


def tickers_por_setor() -> str:
    """Bloco '- setor: TICKER (Nome), ...' em ordem fixa, para o prompt ser estavel."""
    grupos: dict[str, list[str]] = {}
    for ticker, setor in IBOV_SECTORS.items():
        grupos.setdefault(setor, []).append(f"{ticker} ({IBOV_TICKERS[ticker]})")
    return "\n".join(f"- {s}: {', '.join(sorted(v))}" for s, v in sorted(grupos.items()))


def renderizar_prompt(data_referencia: date) -> PromptRenderizado:
    template = _carregar_template()
    schema = schema_para_gemini(AnaliseDiaria)
    texto = Template(template).substitute(
        data_referencia=data_referencia.isoformat(),
        tickers_por_setor=tickers_por_setor(),
        setores=", ".join(s.value for s in Setor),
        commodities=", ".join(c.value for c in Commodity),
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
