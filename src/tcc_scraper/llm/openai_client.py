"""Adaptador da OpenAI para a juiza: julga lotes pela Responses API (gpt-5.6-luna).

Uma requisicao por lote, sem temperature. Reasoning effort "none" (padrao). _chamar_openai e o
unico ponto que conhece HTTP. montar_resposta e o unico que conhece a forma da resposta.
Duck typing igual ao GeminiClient: classificar(prompt, schema) devolve RespostaOpenAI.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from tcc_scraper.llm.jev_client import _espera

logger = logging.getLogger(__name__)

MODELO_PADRAO = "gpt-5.6-luna"
# Chave de cache do prompt fixo da juiza. Mudou o prompt: suba a versao aqui.
PROMPT_CACHE_KEY = "juiza-v1"
_CAMINHO = "/responses"
_NOME_SCHEMA = "lote_julgamento"


class SemCreditoLLM(RuntimeError):
    """Conta OpenAI sem credito (insufficient_quota). Nao adianta repetir."""


@dataclass
class RespostaOpenAI:
    texto: str
    modelo: str
    latencia_ms: int
    tokens_entrada: int | None
    tokens_saida: int | None
    tokens_cache: int | None
    tokens_raciocinio: int | None
    # sempre None: a OpenAI ja inclui o raciocinio em output_tokens
    tokens_pensamento: int | None = None
    status: str = "completed"


def corpo_requisicao(
    modelo: str,
    prompt: str,
    schema: dict,
    effort: str,
    max_output_tokens: int,
) -> dict:
    """Corpo do POST /responses com saida JSON estrita. Sem temperature."""
    return {
        "model": modelo,
        "input": prompt,
        "reasoning": {"effort": effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": _NOME_SCHEMA,
                "strict": True,
                "schema": schema,
            },
        },
        "max_output_tokens": max_output_tokens,
        "store": False,
        "prompt_cache_key": PROMPT_CACHE_KEY,
    }


def _chamar_openai(cliente: httpx.Client, corpo: dict) -> dict:
    """UNICO ponto que conhece HTTP: POST /responses com o corpo montado."""
    resposta = cliente.post(_CAMINHO, json=corpo)
    resposta.raise_for_status()
    return resposta.json()


def _sem_credito(exc: BaseException) -> bool:
    """429 com error.code == insufficient_quota. Corpo que nao e JSON nao conta."""
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 429:
        return False
    try:
        corpo = exc.response.json()
    except ValueError:
        return False
    erro = corpo.get("error") if isinstance(corpo, dict) else None
    return isinstance(erro, dict) and erro.get("code") == "insufficient_quota"


def _transitorio(exc: BaseException) -> bool:
    """Repete em 408, 409, 429, 5xx e erro de conexao. Sem credito nao repete."""
    if _sem_credito(exc):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        codigo = exc.response.status_code
        return codigo in (408, 409, 429) or codigo >= 500
    return isinstance(exc, httpx.TransportError)


def montar_resposta(bruto: dict, modelo: str, latencia_ms: int) -> RespostaOpenAI:
    """Resposta crua da Responses API -> RespostaOpenAI. Itens refusal nao entram no texto."""
    texto = "".join(
        c.get("text") or ""
        for o in bruto.get("output") or []
        if o.get("type") == "message"
        for c in o.get("content") or []
        if c.get("type") == "output_text"
    )
    usage = bruto.get("usage") or {}
    detalhes_entrada = usage.get("input_tokens_details") or {}
    detalhes_saida = usage.get("output_tokens_details") or {}
    return RespostaOpenAI(
        texto=texto,
        modelo=bruto.get("model") or modelo,
        latencia_ms=latencia_ms,
        tokens_entrada=usage.get("input_tokens"),
        tokens_saida=usage.get("output_tokens"),
        tokens_cache=detalhes_entrada.get("cached_tokens"),
        tokens_raciocinio=detalhes_saida.get("reasoning_tokens"),
        status=bruto.get("status") or "completed",
    )


class OpenAIClient:
    """Adaptador da OpenAI (Responses API). Uma requisicao por lote, sem temperature."""

    def __init__(
        self,
        api_key: str,
        modelo: str = MODELO_PADRAO,
        timeout_s: float = 120.0,
        max_attempts: int = 3,
        reasoning_effort: str = "none",
        max_output_tokens: int = 16000,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY ausente: defina no .env")
        self.modelo, self.max_attempts = modelo, max_attempts
        self.reasoning_effort, self.max_output_tokens = reasoning_effort, max_output_tokens
        self._cliente = httpx.Client(
            base_url=base_url, timeout=timeout_s, headers={"Authorization": f"Bearer {api_key}"},
        )

    def classificar(self, prompt: str, schema: dict) -> RespostaOpenAI:
        """Envia o prompt com o schema do lote. Falha sem credito vira SemCreditoLLM."""
        corpo = corpo_requisicao(
            self.modelo, prompt, schema, self.reasoning_effort, self.max_output_tokens,
        )
        inicio = time.monotonic()
        tentativas = Retrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=_espera,
            retry=retry_if_exception(_transitorio),
            reraise=True,
            before_sleep=lambda s: logger.warning(
                "openai: tentativa %d falhou (%s)", s.attempt_number, s.outcome.exception(),
            ),
        )
        try:
            bruto = tentativas(_chamar_openai, self._cliente, corpo)
        except Exception as exc:
            if _sem_credito(exc):
                raise SemCreditoLLM(
                    "OpenAI sem crédito (insufficient_quota): recarregue a conta ou "
                    "troque a chave",
                ) from exc
            raise
        latencia_ms = int((time.monotonic() - inicio) * 1000)
        return montar_resposta(bruto, self.modelo, latencia_ms)

    def close(self) -> None:
        """Fecha o cliente HTTP. A juiza chama ao fim da execucao quando foi ela que criou."""
        self._cliente.close()
