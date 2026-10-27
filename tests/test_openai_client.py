"""Testes do OpenAIClient sem rede: corpo, montagem da resposta e HTTP falso com respx."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from tcc_scraper.llm import openai_client as oc
from tcc_scraper.llm.openai_client import (
    OpenAIClient,
    SemCreditoLLM,
    corpo_requisicao,
    montar_resposta,
)

URL = "https://api.openai.com/v1/responses"
SCHEMA = {"type": "object", "properties": {"itens": {"type": "array"}}, "required": ["itens"]}
JULGAMENTO = {"itens": [{"id": "42", "papel": "protagonista"}]}


def _resposta_ok(texto: str | None = None, status: str = "completed") -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "model": "gpt-5.6-luna",
        "status": status,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": texto or json.dumps(JULGAMENTO)},
                ],
            },
        ],
        "usage": {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 800},
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 50},
            "total_tokens": 1500,
        },
    }


def _erro(codigo: int, corpo: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(codigo, json=corpo, headers=headers or {})


@respx.mock
def test_sucesso_monta_texto_modelo_e_usage():
    respx.post(URL).mock(return_value=httpx.Response(200, json=_resposta_ok()))
    resposta = OpenAIClient("chave-teste").classificar("prompt", SCHEMA)
    assert json.loads(resposta.texto) == JULGAMENTO
    assert resposta.modelo == "gpt-5.6-luna"
    assert resposta.status == "completed"
    assert resposta.tokens_entrada == 1200
    assert resposta.tokens_saida == 300
    assert resposta.tokens_cache == 800
    assert resposta.tokens_raciocinio == 50
    assert resposta.tokens_pensamento is None
    assert isinstance(resposta.latencia_ms, int) and resposta.latencia_ms >= 0


@respx.mock
def test_corpo_enviado_e_cabecalho_de_autorizacao():
    rota = respx.post(URL).mock(return_value=httpx.Response(200, json=_resposta_ok()))
    OpenAIClient("chave-teste").classificar("o prompt", SCHEMA)
    requisicao = rota.calls.last.request
    assert requisicao.headers["Authorization"] == "Bearer chave-teste"
    corpo = json.loads(requisicao.content)
    assert corpo["input"] == "o prompt"
    assert corpo["reasoning"]["effort"] == "none"
    assert corpo["text"]["format"]["type"] == "json_schema"
    assert corpo["text"]["format"]["strict"] is True
    assert corpo["text"]["format"]["schema"] == SCHEMA
    assert corpo["store"] is False
    assert corpo["prompt_cache_key"] == "juiza-v1"
    assert "temperature" not in corpo


@respx.mock
def test_sem_credito_levanta_erro_sem_repetir():
    corpo = {"error": {"code": "insufficient_quota", "message": "sem saldo"}}
    rota = respx.post(URL).mock(return_value=_erro(429, corpo))
    with pytest.raises(SemCreditoLLM):
        OpenAIClient("x", max_attempts=3).classificar("p", SCHEMA)
    assert rota.call_count == 1


@respx.mock
def test_rate_limit_repete_e_respeita_retry_after_ms():
    corpo = {"error": {"code": "rate_limit_exceeded", "message": "devagar"}}
    rota = respx.post(URL).mock(side_effect=[
        _erro(429, corpo, {"retry-after-ms": "0"}),
        httpx.Response(200, json=_resposta_ok()),
    ])
    resposta = OpenAIClient("x", max_attempts=2).classificar("p", SCHEMA)
    assert rota.call_count == 2
    assert json.loads(resposta.texto) == JULGAMENTO


@respx.mock
def test_erro_400_nao_repete():
    rota = respx.post(URL).mock(
        return_value=_erro(400, {"error": {"code": "invalid_request_error"}}),
    )
    with pytest.raises(httpx.HTTPStatusError):
        OpenAIClient("x").classificar("p", SCHEMA)
    assert rota.call_count == 1


@respx.mock
def test_status_incomplete_devolve_texto_parcial():
    parcial = '{"itens": [{"id": "4'
    respx.post(URL).mock(
        return_value=httpx.Response(200, json=_resposta_ok(parcial, status="incomplete")),
    )
    resposta = OpenAIClient("x").classificar("p", SCHEMA)
    assert resposta.texto == parcial
    assert resposta.status == "incomplete"


@respx.mock
def test_item_refusal_resulta_em_texto_vazio():
    bruto = _resposta_ok()
    bruto["output"] = [
        {"type": "message", "content": [{"type": "refusal", "refusal": "nao posso"}]},
    ]
    respx.post(URL).mock(return_value=httpx.Response(200, json=bruto))
    resposta = OpenAIClient("x").classificar("p", SCHEMA)
    assert resposta.texto == ""


def test_chave_vazia_levanta_value_error():
    with pytest.raises(ValueError):
        OpenAIClient("")


def test_corpo_requisicao_e_pura():
    corpo = corpo_requisicao("gpt-5.6-luna", "p", SCHEMA, "none", 16000)
    assert corpo == {
        "model": "gpt-5.6-luna",
        "input": "p",
        "reasoning": {"effort": "none"},
        "text": {"format": {
            "type": "json_schema", "name": "lote_julgamento", "strict": True, "schema": SCHEMA,
        }},
        "max_output_tokens": 16000,
        "store": False,
        "prompt_cache_key": "juiza-v1",
    }
    assert corpo_requisicao("gpt-5.6-luna", "p", SCHEMA, "none", 16000) == corpo


def test_montar_resposta_tolera_usage_ausente():
    resposta = montar_resposta({"output": []}, "modelo-padrao", 7)
    assert resposta.texto == ""
    assert resposta.modelo == "modelo-padrao"
    assert resposta.latencia_ms == 7
    assert resposta.status == "completed"
    assert resposta.tokens_entrada is None
    assert resposta.tokens_saida is None
    assert resposta.tokens_cache is None
    assert resposta.tokens_raciocinio is None


def test_sem_credito_tolera_corpo_nao_json():
    resposta = httpx.Response(429, content=b"<html>muitas requisicoes</html>")
    erro = httpx.HTTPStatusError("429", request=httpx.Request("POST", URL), response=resposta)
    assert oc._sem_credito(erro) is False
    assert oc._transitorio(erro) is True
