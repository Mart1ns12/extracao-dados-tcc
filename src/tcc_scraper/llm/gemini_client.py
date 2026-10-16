"""Wrapper fino sobre o SDK google-genai.

Toda a dependencia na FORMA da API fica em _chamar_api(): confirmada por chamada real
no spike (docs/ADR.md) e unico ponto a tocar se o SDK mudar. Usage, buscas e citacoes
sao extraidos de forma tolerante: o formato dos steps/anotacoes nao e contrato estavel.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# Chaves de `usage`. As primeiras (total_*) sao as que a Interactions API devolveu no
# spike de 2026-10-15; as demais cobrem a forma antiga (generate_content).
_CHAVES_USAGE: dict[str, tuple[str, ...]] = {
    "entrada": ("total_input_tokens", "input_tokens", "prompt_token_count"),
    "saida": ("total_output_tokens", "output_tokens", "candidates_token_count"),
    "pensamento": ("total_thought_tokens", "thoughts_tokens", "thoughts_token_count"),
}
_CODIGOS_TRANSITORIOS = {408, 429, 500, 502, 503, 504}


@dataclass
class RespostaGemini:
    texto: str
    modelo: str
    latencia_ms: int
    tokens_entrada: int | None = None
    tokens_saida: int | None = None
    tokens_pensamento: int | None = None
    # queries executadas pelo google_search_call
    buscas: list[str] = field(default_factory=list)
    # [{url, title}]
    citacoes: list[dict[str, str | None]] = field(default_factory=list)


def _transitorio(exc: BaseException) -> bool:
    """Repete so em 429/5xx/timeout. 400 (schema) ou 401 (chave) nao melhoram repetindo."""
    codigo = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(codigo, int):
        return codigo in _CODIGOS_TRANSITORIOS
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


class GeminiClient:
    def __init__(self, api_key: str, modelo: str, timeout_s: int = 180, max_attempts: int = 3):
        if not api_key:
            raise ValueError("GEMINI_API_KEY ausente: defina no .env ou nas variaveis do Railway")
        from google import genai  # lazy: extra `llm` nao esta no scraper local
        from google.genai import types

        self.modelo, self.max_attempts = modelo, max_attempts
        # timeout em ms; confirmar a unidade no spike
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_s * 1000),
        )

    def analisar(self, prompt: str, schema: dict) -> RespostaGemini:
        chamada = retry(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            retry=retry_if_exception(_transitorio), reraise=True,
            before_sleep=lambda s: logger.warning("gemini: tentativa %d falhou (%s)",
                                                  s.attempt_number, s.outcome.exception()),
        )(_chamar_api)
        inicio = time.monotonic()
        bruto = chamada(self._client, self.modelo, prompt, schema)
        return montar_resposta(bruto, self.modelo, int((time.monotonic() - inicio) * 1000))


def _chamar_api(client: Any, modelo: str, prompt: str, schema: dict) -> Any:
    """UNICO ponto que conhece a forma da chamada (Interactions API, confirmada no spike)."""
    return client.interactions.create(
        model=modelo, input=prompt, tools=[{"type": "google_search"}],
        response_format={"type": "text", "mime_type": "application/json", "schema": schema},
    )
    # Fallback SO se o spike provar que a forma acima nao aceita tools + schema:
    # from google.genai import types
    # return client.models.generate_content(model=modelo, contents=prompt,
    #     config=types.GenerateContentConfig(response_mime_type="application/json",
    #         response_json_schema=schema, tools=[types.Tool(google_search=types.GoogleSearch())]))


def _como_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else {}


def _primeiro(d: dict, chaves: tuple[str, ...]) -> int | None:
    for k in chaves:
        if isinstance(d.get(k), int):
            return d[k]
    return None


def _coletar(node: Any, tipos: set[str], achados: list[dict] | None = None) -> list[dict]:
    """Todos os dicts com `type` em `tipos`, em qualquer profundidade."""
    achados = [] if achados is None else achados
    if isinstance(node, dict):
        if node.get("type") in tipos:
            achados.append(node)
        for v in node.values():
            _coletar(v, tipos, achados)
    elif isinstance(node, list):
        for v in node:
            _coletar(v, tipos, achados)
    return achados


def extrair_buscas_e_citacoes(bruto: Any) -> tuple[list[str], list[dict[str, str | None]]]:
    """Queries executadas e {url, title} das anotacoes url_citation. Ausente -> lista vazia.

    Caminha a resposta inteira em vez de fixar steps[].content[].annotations[]: o formato
    da Interactions API pode variar entre versoes do SDK. Cobre tambem a forma antiga
    (candidates[].grounding_metadata) por seguranca.
    """
    d = _como_dict(bruto)
    # Spike de 2026-10-15: as queries ficam em google_search_call.arguments.queries.
    buscas = [q for no in _coletar(d, {"google_search_call"})
              for q in ((no.get("arguments") or {}).get("queries") or no.get("queries") or [])
              if isinstance(q, str)]
    citacoes: list[dict[str, str | None]] = []
    vistos: set[str] = set()
    for no in _coletar(d, {"url_citation"}):
        url = no.get("url")
        if url and url not in vistos:
            vistos.add(url)
            citacoes.append({"url": url, "title": no.get("title")})
    for cand in d.get("candidates") or []:
        gm = cand.get("grounding_metadata") or {}
        buscas += [q for q in (gm.get("web_search_queries") or []) if isinstance(q, str)]
        for ch in gm.get("grounding_chunks") or []:
            web = ch.get("web") or {}
            if web.get("uri") and web["uri"] not in vistos:
                vistos.add(web["uri"])
                citacoes.append({"url": web["uri"], "title": web.get("title")})
    return buscas, citacoes


def montar_resposta(bruto: Any, modelo: str, latencia_ms: int) -> RespostaGemini:
    d = _como_dict(bruto)
    texto = getattr(bruto, "output_text", None) or getattr(bruto, "text", None) \
        or d.get("output_text") or d.get("text") or ""
    usage = d.get("usage") or d.get("usage_metadata") or {}
    buscas, citacoes = extrair_buscas_e_citacoes(d)
    return RespostaGemini(
        texto=texto, modelo=modelo, latencia_ms=latencia_ms,
        tokens_entrada=_primeiro(usage, _CHAVES_USAGE["entrada"]),
        tokens_saida=_primeiro(usage, _CHAVES_USAGE["saida"]),
        tokens_pensamento=_primeiro(usage, _CHAVES_USAGE["pensamento"]),
        buscas=buscas, citacoes=citacoes,
    )
