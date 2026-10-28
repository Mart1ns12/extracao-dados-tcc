"""Adaptador da Anthropic para a juiza: julga lotes pela Messages API (claude-haiku-5-5).

Uma requisicao por lote. O SDK oficial ja repete 408/409/429/5xx e falha de conexao
(`max_retries`), por isso nao ha tenacity aqui. Sem temperature, top_p, top_k, thinking
explicito nem prefill: o Haiku 5.5 rejeita esses campos (400), e o pensamento adaptativo
fica no padrao. `_chamar_anthropic` e o unico ponto que chama a API; `montar_resposta` e o
unico que conhece a forma da resposta. Duck typing igual aos outros clientes:
classificar(prompt, schema) devolve RespostaAnthropic.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import anthropic

from tcc_scraper.llm.openai_client import SemCreditoLLM

logger = logging.getLogger(__name__)

MODELO_PADRAO = "claude-haiku-5-5"


@dataclass
class RespostaAnthropic:
    texto: str
    modelo: str
    latencia_ms: int
    tokens_entrada: int | None
    tokens_saida: int | None
    tokens_cache: int | None
    # sempre None: o pensamento ja esta dentro de output_tokens
    tokens_raciocinio: int | None = None
    tokens_pensamento: int | None = None
    status: str = "completed"


def corpo_requisicao(
    modelo: str,
    prompt: str,
    schema: dict,
    effort: str,
    max_output_tokens: int,
) -> dict:
    """Argumentos de messages.create com saida JSON estrita, sem temperature nem thinking."""
    return {
        "model": modelo,
        "max_tokens": max_output_tokens,
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        "messages": [{"role": "user", "content": prompt}],
    }


def _chamar_anthropic(cliente: Any, corpo: dict) -> Any:
    """UNICO ponto que conhece a API: messages.create com o corpo montado."""
    return cliente.messages.create(**corpo)


def _sem_credito(exc: anthropic.APIStatusError) -> bool:
    """402 (billing_error) ou 400 com 'credit balance' na mensagem: conta sem saldo."""
    if exc.status_code == 402:
        return True
    if exc.status_code == 400:
        return "credit balance" in f"{exc} {exc.body}".lower()
    return False


def montar_resposta(bruto: Any, modelo: str, latencia_ms: int) -> RespostaAnthropic:
    """Message crua -> RespostaAnthropic. Le os blocos por `type`: thinking pode vir antes do texto.

    Recusa (refusal) vira texto vazio, e a juiza trata texto invalido como falha do item.
    max_tokens vira status incomplete. Sem `usage`, os tokens ficam None.
    """
    motivo = getattr(bruto, "stop_reason", None)
    if motivo == "refusal":
        texto, status = "", "refusal"
    else:
        texto = "".join(
            getattr(b, "text", None) or ""
            for b in (getattr(bruto, "content", None) or [])
            if getattr(b, "type", None) == "text"
        )
        status = "incomplete" if motivo == "max_tokens" else "completed"
    uso = getattr(bruto, "usage", None)
    if uso is None:
        entrada = saida = cache = None
    else:
        # Cache de leitura e de criacao entram na entrada (custo_llm cobra cache a parte).
        # Sem cache_control o SDK nao cria cache, entao a criacao costuma ser zero.
        leitura = getattr(uso, "cache_read_input_tokens", None) or 0
        criacao = getattr(uso, "cache_creation_input_tokens", None) or 0
        entrada = (getattr(uso, "input_tokens", None) or 0) + leitura + criacao
        saida = getattr(uso, "output_tokens", None)
        cache = leitura
    return RespostaAnthropic(
        texto=texto,
        modelo=getattr(bruto, "model", None) or modelo,
        latencia_ms=latencia_ms,
        tokens_entrada=entrada,
        tokens_saida=saida,
        tokens_cache=cache,
        status=status,
    )


class AnthropicClient:
    """Adaptador da Anthropic (Messages API). Uma requisicao por lote, sem temperature."""

    def __init__(
        self,
        api_key: str,
        modelo: str = MODELO_PADRAO,
        timeout_s: float = 120.0,
        max_attempts: int = 3,
        effort: str = "medium",
        max_output_tokens: int = 16000,
        cliente: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY ausente: defina no .env")
        self.modelo, self.max_attempts = modelo, max_attempts
        self.effort, self.max_output_tokens = effort, max_output_tokens
        # `cliente` permite injetar um falso nos testes. max_retries = tentativas extras do SDK.
        self._cliente = cliente or anthropic.Anthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max(0, max_attempts - 1),
        )

    def classificar(self, prompt: str, schema: dict) -> RespostaAnthropic:
        """Envia o prompt com o schema do lote. Credito esgotado vira SemCreditoLLM."""
        corpo = corpo_requisicao(
            self.modelo, prompt, schema, self.effort, self.max_output_tokens,
        )
        inicio = time.monotonic()
        try:
            bruto = _chamar_anthropic(self._cliente, corpo)
        except anthropic.APIStatusError as exc:
            if _sem_credito(exc):
                raise SemCreditoLLM(
                    "Anthropic sem crédito (billing/credit balance): recarregue a conta ou "
                    "troque a chave",
                ) from exc
            raise
        latencia_ms = int((time.monotonic() - inicio) * 1000)
        return montar_resposta(bruto, self.modelo, latencia_ms)

    def close(self) -> None:
        """Fecha o cliente HTTP. A juiza chama ao fim da execucao quando foi ela que criou."""
        fechar = getattr(self._cliente, "close", None)
        if fechar is not None:
            fechar()
