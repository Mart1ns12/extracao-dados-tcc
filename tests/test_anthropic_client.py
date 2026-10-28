"""AnthropicClient da juiza (sem rede): cliente falso injetado, erros montados com httpx2.

O SDK 2.3.0 monta os erros sobre httpx2.Response (nao httpx), por isso os erros falsos
usam httpx2 tambem.
"""
from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from tcc_scraper.llm import juiza
from tcc_scraper.llm.anthropic_client import (
    MODELO_PADRAO,
    AnthropicClient,
    corpo_requisicao,
)
from tcc_scraper.llm.openai_client import SemCreditoLLM

SCHEMA = {
    "type": "object",
    "properties": {"itens": {"type": "array"}},
    "required": ["itens"],
    "additionalProperties": False,
}
TEXTO_JSON = '{"itens": []}'


class _Mensagens:
    def __init__(self, resposta=None, erro=None):
        self.resposta, self.erro, self.kwargs = resposta, erro, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.erro is not None:
            raise self.erro
        return self.resposta


class _ClienteFalso:
    def __init__(self, resposta=None, erro=None):
        self.messages = _Mensagens(resposta, erro)
        self.fechado = False

    def close(self):
        self.fechado = True


def _usage(**campos):
    base = {"input_tokens": 1000, "output_tokens": 200,
            "cache_read_input_tokens": 300, "cache_creation_input_tokens": 0}
    return SimpleNamespace(**{**base, **campos})


_PADRAO = object()


def _mensagem(blocos, *, stop="end_turn", usage=_PADRAO, modelo="claude-haiku-5-5"):
    # usage=None simula o campo ausente de verdade (sem cair no usage padrao).
    return SimpleNamespace(content=blocos, stop_reason=stop, model=modelo,
                           usage=_usage() if usage is _PADRAO else usage)


def _texto(texto):
    return SimpleNamespace(type="text", text=texto)


def _pensamento(texto):
    return SimpleNamespace(type="thinking", thinking=texto, signature="assinatura")


def _erro(classe, status, mensagem):
    resposta = httpx2.Response(
        status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    corpo = {"type": "error", "error": {"type": "erro", "message": mensagem}}
    return classe(mensagem, response=resposta, body=corpo)


def _cliente(resposta=None, erro=None):
    return AnthropicClient("chave-de-teste", cliente=_ClienteFalso(resposta, erro))


def test_sucesso_devolve_texto_modelo_e_usage_com_cache():
    cliente = _cliente(_mensagem([_texto(TEXTO_JSON)]))

    r = cliente.classificar("prompt", SCHEMA)

    assert r.texto == TEXTO_JSON
    assert r.modelo == "claude-haiku-5-5"
    assert r.status == "completed"
    # entrada = input + cache de leitura + cache de criacao; cache sai separado
    assert r.tokens_entrada == 1300
    assert r.tokens_cache == 300
    assert r.tokens_saida == 200
    # o pensamento ja esta em output_tokens: nao ha campo separado
    assert r.tokens_pensamento is None
    assert r.tokens_raciocinio is None
    assert r.latencia_ms >= 0


def test_bloco_thinking_antes_do_texto_nao_vai_para_o_texto():
    cliente = _cliente(_mensagem([_pensamento("raciocinio longo"), _texto(TEXTO_JSON)]))

    assert cliente.classificar("prompt", SCHEMA).texto == TEXTO_JSON


def test_corpo_enviado_tem_effort_e_schema_e_nada_que_o_haiku_rejeita():
    cliente = _cliente(_mensagem([_texto(TEXTO_JSON)]))

    cliente.classificar("meu prompt", SCHEMA)

    kwargs = cliente._cliente.messages.kwargs
    assert set(kwargs) == {"model", "max_tokens", "output_config", "messages"}
    assert kwargs["model"] == "claude-haiku-5-5" == MODELO_PADRAO
    assert kwargs["max_tokens"] == 16000
    assert kwargs["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert kwargs["messages"] == [{"role": "user", "content": "meu prompt"}]


def test_corpo_requisicao_respeita_effort_e_limite_configurados():
    corpo = corpo_requisicao("claude-haiku-5-5", "p", SCHEMA, "high", 4000)

    assert corpo["output_config"]["effort"] == "high"
    assert corpo["max_tokens"] == 4000
    for proibido in ("thinking", "temperature", "top_p", "top_k", "fallbacks"):
        assert proibido not in corpo


def test_recusa_vira_texto_vazio_e_status_refusal():
    cliente = _cliente(_mensagem([_texto("texto que nao deve aparecer")], stop="refusal"))

    r = cliente.classificar("prompt", SCHEMA)

    assert r.texto == ""
    assert r.status == "refusal"


def test_max_tokens_vira_status_incomplete():
    cliente = _cliente(_mensagem([_texto('{"itens": [')], stop="max_tokens"))

    r = cliente.classificar("prompt", SCHEMA)

    assert r.status == "incomplete"
    assert r.texto == '{"itens": ['


def test_usage_ausente_deixa_os_tokens_none():
    cliente = _cliente(_mensagem([_texto(TEXTO_JSON)], usage=None))

    r = cliente.classificar("prompt", SCHEMA)

    assert r.texto == TEXTO_JSON
    assert r.tokens_entrada is None
    assert r.tokens_saida is None
    assert r.tokens_cache is None


def test_chave_vazia_dah_value_error():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicClient("")


def test_sem_credito_402_vira_sem_credito_llm():
    erro = _erro(anthropic.APIStatusError, 402, "billing error")
    cliente = _cliente(erro=erro)

    with pytest.raises(SemCreditoLLM):
        cliente.classificar("prompt", SCHEMA)


def test_sem_credito_400_com_credit_balance_vira_sem_credito_llm():
    erro = _erro(anthropic.BadRequestError, 400, "Your credit balance is too low")
    cliente = _cliente(erro=erro)

    with pytest.raises(SemCreditoLLM) as info:
        cliente.classificar("prompt", SCHEMA)
    assert info.value.__cause__ is erro


def test_400_de_outro_motivo_e_relevantado():
    erro = _erro(anthropic.BadRequestError, 400, "max_tokens is too large")
    cliente = _cliente(erro=erro)

    with pytest.raises(anthropic.BadRequestError):
        cliente.classificar("prompt", SCHEMA)


def test_close_fecha_o_cliente_do_sdk():
    falso = _ClienteFalso()
    AnthropicClient("chave-de-teste", cliente=falso).close()

    assert falso.fechado is True


def test_cliente_real_repete_o_que_o_sdk_repete_sem_tenacity():
    # Sem rede: so checa a configuracao montada. max_attempts=3 vira 2 retries no SDK.
    cliente = AnthropicClient("chave-de-teste", max_attempts=3, timeout_s=45.0)

    assert cliente._cliente.max_retries == 2
    assert cliente._cliente.timeout == 45.0
    cliente.close()


def test_criar_cliente_llm_despacha_para_anthropic():
    settings = SimpleNamespace(
        juiza_provedor="anthropic", anthropic_api_key="chave-de-teste",
        juiza_modelo_efetivo="claude-haiku-5-5", anthropic_timeout_s=30.0,
        anthropic_max_attempts=1, anthropic_effort="low",
        anthropic_max_output_tokens=900,
    )

    cliente = juiza._criar_cliente_llm(settings)

    assert isinstance(cliente, AnthropicClient)
    assert cliente.effort == "low"
    assert cliente.max_output_tokens == 900
    assert cliente._cliente.max_retries == 0
    cliente.close()
