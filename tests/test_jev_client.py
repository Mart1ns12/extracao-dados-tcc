"""Testes do JevClient sem rede: mapeamento, perguntas e HTTP falso com respx.

A resposta real do spike (tests/fixtures/jev_systemone_resposta.json) cobre papel e qualidade.
Tipo_conteudo e evento sao montados aqui no mesmo formato.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from tcc_scraper.config import APELIDOS
from tcc_scraper.llm import jev_client as jc
from tcc_scraper.llm.jev_client import (
    JevClient,
    estado_jev,
    mapear_resposta,
    perguntas_jev,
    prompt_jev,
)
from tcc_scraper.llm.juiza_schema import Evento, JulgamentoItem, Papel, TipoConteudo

FIXTURE = Path(__file__).parent / "fixtures" / "jev_systemone_resposta.json"
URL = "https://api.typesafe.ai/v1/systemone"

ITEM = {
    "id": 42,
    "ticker": "PETR4",
    "title": "Petrobras lucra no trimestre",
    "source_domain": "g1.globo.com",
    "trecho": "A Petrobras divulgou lucro líquido de R$ 10 bilhões no trimestre.",
    "origem": "google_news",
}
ITEM_VALE = {**ITEM, "id": 43, "ticker": "VALE3", "title": "Vale anuncia dividendos"}


def _choice(rotulo: str, confianca: float) -> dict:
    return {
        "type": "choice",
        "choice": rotulo,
        "confidence": confianca,
        "probabilities": {rotulo: 1.0},
    }


def _resposta_real() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _resposta_completa() -> dict:
    resposta = _resposta_real()
    resposta["answers"]["tipo_conteudo"] = _choice("reportagem", 0.9)
    resposta["answers"]["evento"] = _choice("resultado", 0.7)
    return resposta


def test_mapear_resposta_real_papel_e_qualidade():
    julgamento = mapear_resposta(ITEM, _resposta_completa())
    assert julgamento["papel"] == "protagonista"
    assert julgamento["qualidade"] == 0.2625  # score 1.05 / 4
    assert julgamento["id"] == "42"
    assert julgamento["ticker"] == "PETR4"
    assert julgamento["tickers_adicionais"] == []
    JulgamentoItem.model_validate(julgamento)  # contrato da juiza


def test_confianca_e_o_minimo_entre_papel_e_tipo():
    menor_tipo = _resposta_completa()
    menor_tipo["answers"]["tipo_conteudo"]["confidence"] = 0.5
    assert mapear_resposta(ITEM, menor_tipo)["confianca"] == 0.5

    menor_papel = _resposta_completa()
    menor_papel["answers"]["tipo_conteudo"]["confidence"] = 0.95
    assert mapear_resposta(ITEM, menor_papel)["confianca"] == 0.84  # confidence do papel


@pytest.mark.parametrize(
    ("nome", "enum_"),
    [("papel", Papel), ("tipo_conteudo", TipoConteudo), ("evento", Evento)],
)
def test_todo_rotulo_do_enum_e_aceito_pelo_texto(nome, enum_):
    for valor in enum_:
        resposta = _resposta_completa()
        resposta["answers"][nome] = _choice(valor.value, 0.8)
        assert mapear_resposta(ITEM, resposta)[nome] == valor.value


def test_rotulo_fora_do_enum_vira_erro():
    resposta = _resposta_completa()
    resposta["answers"]["papel"]["choice"] = "mencionada"  # rotulo do spike, nao do enum
    with pytest.raises(ValueError, match="rótulo desconhecido"):
        mapear_resposta(ITEM, resposta)


def test_indice_no_lugar_do_rotulo_vira_erro():
    resposta = _resposta_completa()
    resposta["answers"]["papel"]["choice"] = 0
    with pytest.raises(ValueError, match="rótulo desconhecido"):
        mapear_resposta(ITEM, resposta)


@pytest.mark.parametrize("bruto", [{}, {"answers": []}, {"answers": {"papel": {}}}])
def test_resposta_sem_answers_validas_vira_erro(bruto):
    with pytest.raises(ValueError):
        mapear_resposta(ITEM, bruto)


def test_tipo_de_resposta_errado_vira_erro():
    resposta = _resposta_completa()
    resposta["answers"]["qualidade"]["type"] = "choice"
    with pytest.raises(ValueError, match="qualidade"):
        mapear_resposta(ITEM, resposta)


@pytest.mark.parametrize(
    ("score", "esperado"),
    [(0.0, 0.0), (2.0, 0.5), (4.0, 1.0), (5.0, 1.0), (-1.0, 0.0)],
)
def test_qualidade_limitada_a_zero_um(score, esperado):
    resposta = _resposta_completa()
    resposta["answers"]["qualidade"]["score"] = score
    assert mapear_resposta(ITEM, resposta)["qualidade"] == esperado


def test_estado_traz_empresa_apelidos_fonte_titulo_e_trecho():
    estado = estado_jev(ITEM)
    assert "PETR4" in estado
    for apelido in APELIDOS["PETR4"]:
        assert apelido in estado
    assert "g1.globo.com" in estado
    assert ITEM["title"] in estado
    assert ITEM["trecho"] in estado


def test_perguntas_seguem_a_ordem_e_os_enums():
    perguntas = perguntas_jev(ITEM)
    assert list(perguntas) == ["papel", "tipo_conteudo", "evento", "qualidade"]
    for nome, enum_ in [("papel", Papel), ("tipo_conteudo", TipoConteudo), ("evento", Evento)]:
        assert perguntas[nome]["type"] == "choice"
        assert list(perguntas[nome]["criteria"]) == [e.value for e in enum_]
        assert all(perguntas[nome]["criteria"].values())
    assert perguntas["qualidade"]["type"] == "score"
    assert len(perguntas["qualidade"]["criteria"]) == 5


def test_instrucoes_citam_empresa_e_ticker():
    assert "PETR4" in perguntas_jev(ITEM)["papel"]["instructions"]
    assert "Petrobras" in perguntas_jev(ITEM)["evento"]["instructions"]


def test_perguntas_sao_copias_independentes():
    perguntas_jev(ITEM)["papel"]["criteria"].clear()
    assert len(perguntas_jev(ITEM)["papel"]["criteria"]) == len(Papel)


def test_prompt_jev_e_estavel():
    prompt = prompt_jev()
    assert prompt.nome == "juiza_jev"
    assert prompt.versao == jc.JEV_PERGUNTAS_VERSAO
    assert prompt.sha256 == prompt_jev().sha256
    assert len(prompt.sha256) == 64


@pytest.fixture
def sem_espera(monkeypatch):
    monkeypatch.setattr(jc, "_BACKOFF", lambda estado: 0.0)


@respx.mock
def test_chamar_jev_forma_da_requisicao():
    rota = respx.post(URL).mock(return_value=httpx.Response(200, json=_resposta_real()))
    cliente = JevClient("chave-teste")
    bruto = jc._chamar_jev(cliente._cliente, cliente.modelo, ITEM)
    assert bruto["answers"]["papel"]["choice"] == "protagonista"
    requisicao = rota.calls.last.request
    assert requisicao.headers["Authorization"] == "Bearer chave-teste"
    corpo = json.loads(requisicao.content)
    assert corpo["model"] == "jev-1.13.0"
    assert corpo["state"] == estado_jev(ITEM)
    assert set(corpo["questions"]) == {"papel", "tipo_conteudo", "evento", "qualidade"}


@respx.mock
def test_classificar_lote_soma_tokens_de_entrada():
    respx.post(URL).mock(return_value=httpx.Response(200, json=_resposta_completa()))
    resposta = JevClient("x").classificar_lote([ITEM, ITEM_VALE])
    assert len(resposta.itens) == 2
    assert resposta.erros == []
    assert len(resposta.bruto) == 2
    assert resposta.tokens_entrada == 2 * 453  # usage.input_tokens do spike


def test_classificar_lote_vazio_nao_chama_a_api():
    resposta = JevClient("x").classificar_lote([])
    assert resposta.itens == [] and resposta.erros == [] and resposta.tokens_entrada == 0


@pytest.mark.parametrize("codigo", [400, 401, 403, 404, 422])
@respx.mock
def test_erro_sem_retry_falha_o_lote(codigo):
    rota = respx.post(URL).mock(return_value=httpx.Response(codigo, json={"error": "x"}))
    with pytest.raises(RuntimeError, match=f"HTTP {codigo}"):
        JevClient("x").classificar_lote([ITEM])
    assert rota.call_count == 1


@respx.mock
def test_429_repete_e_respeita_retry_after():
    rota = respx.post(URL).mock(side_effect=[
        httpx.Response(429, headers={"retry-after": "0"}, json={}),
        httpx.Response(200, json=_resposta_completa()),
    ])
    resposta = JevClient("x").classificar_lote([ITEM])
    assert rota.call_count == 2
    assert len(resposta.itens) == 1
    assert resposta.erros == []


@respx.mock
def test_529_esgota_tentativas_e_o_item_vai_para_erros(sem_espera):
    def responder(request: httpx.Request) -> httpx.Response:
        if "(PETR4)" in json.loads(request.content)["state"]:
            return httpx.Response(529, headers={"retry-after-ms": "0"}, json={})
        return httpx.Response(200, json=_resposta_completa())

    rota = respx.post(URL).mock(side_effect=responder)
    resposta = JevClient("x", max_attempts=3).classificar_lote([ITEM, ITEM_VALE])
    assert len(resposta.itens) == 1
    assert resposta.erros == [{"id": "42", "ticker": "PETR4", "erro": "HTTP 529"}]
    assert rota.call_count == 3 + 1  # PETR4 tres vezes, VALE3 uma


@respx.mock
def test_erro_de_conexao_repete(sem_espera):
    respx.post(URL).mock(side_effect=[
        httpx.ConnectError("queda de conexao"),
        httpx.Response(200, json=_resposta_completa()),
    ])
    resposta = JevClient("x").classificar_lote([ITEM])
    assert len(resposta.itens) == 1
    assert resposta.erros == []


@respx.mock
def test_resposta_invalida_de_um_item_vai_para_erros_sem_derrubar_o_lote():
    def responder(request: httpx.Request) -> httpx.Response:
        if "(PETR4)" in json.loads(request.content)["state"]:
            ruim = _resposta_completa()
            ruim["answers"]["papel"]["choice"] = "mencionada"
            return httpx.Response(200, json=ruim)
        return httpx.Response(200, json=_resposta_completa())

    respx.post(URL).mock(side_effect=responder)
    resposta = JevClient("x").classificar_lote([ITEM, ITEM_VALE])
    assert [i["ticker"] for i in resposta.itens] == ["VALE3"]
    assert len(resposta.erros) == 1
    assert resposta.erros[0]["ticker"] == "PETR4"
    assert resposta.erros[0]["erro"].startswith("resposta inválida")
    assert len(resposta.bruto) == 2


@respx.mock
def test_todos_os_itens_falhando_levanta_runtime_error():
    respx.post(URL).mock(return_value=httpx.Response(422, json={"detail": "campo"}))
    with pytest.raises(RuntimeError, match="todos os 2 itens falharam"):
        JevClient("x").classificar_lote([ITEM, ITEM_VALE])
