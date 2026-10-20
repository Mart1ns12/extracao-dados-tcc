"""Schema e prompt da juiza (sem rede)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tcc_scraper.config import universo_coleta
from tcc_scraper.llm.juiza_prompt import VERSAO, renderizar_prompt_juiza
from tcc_scraper.llm.juiza_schema import JulgamentoItem, schema_juiza

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
