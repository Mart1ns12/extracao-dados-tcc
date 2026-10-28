"""Schema e prompt da juiza (sem rede)."""
from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from tcc_scraper.config import universo_coleta
from tcc_scraper.llm.juiza_prompt import (
    NOME_ANTHROPIC,
    NOME_OPENAI,
    VERSAO,
    renderizar_prompt_juiza,
)
from tcc_scraper.llm.juiza_schema import JulgamentoItem, schema_juiza

# Hashes capturados ANTES da inclusao do provedor openai: provam que o prompt e o schema
# Gemini nao mudaram. Mudar qualquer um deles exige subir VERSAO.
SHA_PROMPT_GEMINI = "de667ecb84351fbfa4d608e8ae5cee3ddf9cd35c4c4c26a58c05201595f460a5"
SHA_SCHEMA_GEMINI = "6ab1cf6a9401865506270828bb6b04b6bfc0f4468d87e183e003410671144c25"

ITENS_A = [
    {
        "id": "a1",
        "ticker": "PETR4",
        "empresa": "Petrobras",
        "titulo": "Petrobras lucra no trimestre",
        "fonte": "InfoMoney",
        "trecho": "A estatal informou lucro maior que o esperado.",
    }
]
ITENS_B = [
    {
        "id": "b9",
        "ticker": "VALE3",
        "empresa": "Vale",
        "titulo": "Ibovespa fecha em alta",
        "fonte": "Valor",
        "trecho": "Panorama do pregao.",
    }
]


def _sem_title_nem_null(node) -> bool:
    if isinstance(node, dict):
        if "title" in node:
            return False
        if node.get("type") == "null":
            return False
        return all(_sem_title_nem_null(v) for v in node.values())
    if isinstance(node, list):
        return all(_sem_title_nem_null(v) for v in node)
    return True


def test_prompt_tem_universo_e_itens():
    r = renderizar_prompt_juiza(ITENS_A)
    for ticker in universo_coleta():
        assert ticker in r.texto, ticker
    assert '"id": "a1"' in r.texto
    assert "Petrobras lucra no trimestre" in r.texto
    assert "$" not in r.texto


def test_sha256_estavel_entre_itens_e_versao_1():
    r1 = renderizar_prompt_juiza(ITENS_A)
    r2 = renderizar_prompt_juiza(ITENS_B)
    assert r1.texto != r2.texto
    assert r1.sha256 == r2.sha256
    assert len(r1.sha256) == 64
    assert r1.nome == "juiza"
    assert r1.versao == VERSAO == 1


def test_schema_sem_title_nem_null():
    assert _sem_title_nem_null(schema_juiza())


def test_schema_required_cobre_todos_os_campos_do_item():
    schema = schema_juiza()
    definicao = schema["$defs"]["JulgamentoItem"]
    assert set(definicao["required"]) == set(JulgamentoItem.model_fields)


def test_papel_invalido_falha_na_validacao():
    with pytest.raises(ValidationError):
        JulgamentoItem(
            id="x",
            ticker="PETR4",
            papel="xyz",
            tipo_conteudo="reportagem",
            evento="resultado",
            qualidade=0.5,
            confianca=0.5,
        )


def _nos(node):
    """Todo no do schema (dicts e listas), em qualquer profundidade."""
    yield node
    if isinstance(node, dict):
        filhos = node.values()
    elif isinstance(node, list):
        filhos = node
    else:
        filhos = []
    for filho in filhos:
        yield from _nos(filho)


def test_sha256_gemini_nao_mudou():
    assert renderizar_prompt_juiza(ITENS_A).sha256 == SHA_PROMPT_GEMINI
    assert renderizar_prompt_juiza(ITENS_A, provedor="gemini").sha256 == SHA_PROMPT_GEMINI


def test_schema_gemini_intacto():
    schema = schema_juiza()
    assert schema == schema_juiza("gemini")
    hash_schema = hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8"))
    assert hash_schema.hexdigest() == SHA_SCHEMA_GEMINI


def test_schema_openai_estrito():
    schema = schema_juiza("openai")
    objetos = [n for n in _nos(schema) if isinstance(n, dict) and "properties" in n]
    assert len(objetos) >= 2  # raiz (LoteJulgamento) e JulgamentoItem em $defs
    for objeto in objetos:
        assert objeto["additionalProperties"] is False
        assert set(objeto["required"]) == set(objeto["properties"])
    for no in _nos(schema):
        if isinstance(no, dict):
            assert "default" not in no
            assert "title" not in no


def test_prompt_anthropic_nome_proprio_e_schema_estrito_da_openai():
    gemini = renderizar_prompt_juiza(ITENS_A)
    anthropic_ = renderizar_prompt_juiza(ITENS_A, provedor="anthropic")
    assert anthropic_.nome == NOME_ANTHROPIC == "juiza_anthropic"
    assert anthropic_.versao == VERSAO
    assert anthropic_.template == gemini.template
    # JSON Schema estrito da OpenAI, sem envelope (name/strict) e sem limites numericos
    assert anthropic_.schema == schema_juiza("anthropic")
    assert "name" not in anthropic_.schema and "strict" not in anthropic_.schema
    assert anthropic_.schema != gemini.schema


def test_schema_anthropic_sem_minimum_maximum():
    """A Anthropic recusa minimum/maximum em number (400); o resto e o schema da OpenAI."""
    openai = json.dumps(schema_juiza("openai"))
    anthropic_ = json.dumps(schema_juiza("anthropic"))
    assert '"minimum"' in openai and '"maximum"' in openai
    assert '"minimum"' not in anthropic_ and '"maximum"' not in anthropic_
    assert '"additionalProperties": false' in anthropic_


def test_prompt_openai_nome_versao_e_schema_proprio():
    gemini = renderizar_prompt_juiza(ITENS_A)
    openai = renderizar_prompt_juiza(ITENS_A, provedor="openai")
    assert openai.nome == NOME_OPENAI == "juiza_openai"
    assert openai.versao == VERSAO
    assert openai.sha256 != gemini.sha256
    assert openai.template == gemini.template
    assert openai.schema == schema_juiza("openai")
    assert openai.schema != gemini.schema
