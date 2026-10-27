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
from tcc_scraper.entrega.filtros import entra_por_rotulos
from tcc_scraper.llm import jev_client as jc
from tcc_scraper.llm.jev_client import (
    JevClient,
    JevFalhaLote,
    decisao_entrega,
    estado_jev,
    mapear_resposta,
    perguntas_jev,
    prompt_jev,
    rotulo_modelo_jev,
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


@respx.mock
def test_todos_401_levantam_falha_permanente():
    respx.post(URL).mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(JevFalhaLote, match="todos os 2 itens falharam") as exc:
        JevClient("x", max_attempts=1).classificar_lote([ITEM, ITEM_VALE])
    assert exc.value.permanente is True
    assert exc.value.codigos == {401}


@respx.mock
def test_todos_500_levantam_falha_nao_permanente():
    respx.post(URL).mock(return_value=httpx.Response(500, json={}))
    with pytest.raises(JevFalhaLote) as exc:
        JevClient("x", max_attempts=1).classificar_lote([ITEM, ITEM_VALE])
    assert exc.value.permanente is False
    assert exc.value.codigos == {500}


def _choice_com(rotulo: str, probabilidades: dict, confianca: float = 0.8) -> dict:
    return {
        "type": "choice",
        "choice": rotulo,
        "confidence": confianca,
        "probabilities": probabilidades,
    }


def test_probabilidades_no_enum_definem_as_duas_massas():
    resposta = _resposta_completa()
    resposta["answers"]["papel"] = _choice_com(
        "protagonista", {"protagonista": 0.9, "mencao": 0.05, "errada": 0.05},
    )
    resposta["answers"]["tipo_conteudo"] = _choice_com(
        "reportagem", {"reportagem": 0.95, "opiniao": 0.05},
    )
    julgamento = mapear_resposta(ITEM, resposta)
    assert julgamento["p_entra"] == pytest.approx(0.855)  # .9 * .95
    assert julgamento["p_nao_entra"] == pytest.approx(0.145)  # .1 + .05 - .1 * .05
    assert julgamento["prob_fonte"] == "probabilidades"


def test_fixture_real_nao_soma_rotulos_fora_do_enum():
    julgamento = mapear_resposta(ITEM, _resposta_completa())
    # papel real: protagonista .88 + citada_com_fato .12; mencionada e nao_relacionada sao 0.0
    assert julgamento["p_entra"] == pytest.approx(1.0)
    assert julgamento["p_nao_entra"] == pytest.approx(0.0)
    assert julgamento["prob_fonte"] == "probabilidades"


def test_rotulo_fora_do_enum_com_massa_nao_conta_para_nenhum_lado():
    resposta = _resposta_completa()
    # soma 1.0, mas 0.2 esta em "mencionada", que nao esta no enum de papel
    resposta["answers"]["papel"] = _choice_com(
        "protagonista", {"protagonista": 0.6, "citada_com_fato": 0.2, "mencionada": 0.2},
    )
    julgamento = mapear_resposta(ITEM, resposta)
    assert julgamento["p_entra"] == pytest.approx(0.8)
    assert julgamento["p_nao_entra"] == pytest.approx(0.0)


def test_massa_fora_do_enum_de_meio_nao_ajuda_nenhum_lado():
    resposta = _resposta_completa()
    resposta["answers"]["papel"] = _choice_com(
        "protagonista", {"protagonista": 0.5, "nao_relacionada": 0.5},
    )
    julgamento = mapear_resposta(ITEM, resposta)
    assert julgamento["p_entra"] == pytest.approx(0.5)
    assert julgamento["p_nao_entra"] == pytest.approx(0.0)


def test_sem_probabilities_usa_confidence_do_rotulo_escolhido():
    resposta = _resposta_completa()
    resposta["answers"]["papel"] = {"type": "choice", "choice": "protagonista", "confidence": 0.8}
    resposta["answers"]["tipo_conteudo"] = {
        "type": "choice", "choice": "reportagem", "confidence": 0.9,
    }
    julgamento = mapear_resposta(ITEM, resposta)
    assert julgamento["p_entra"] == pytest.approx(0.72)  # .8 * .9
    assert julgamento["p_nao_entra"] == pytest.approx(0.0)
    assert julgamento["prob_fonte"] == "confianca"

    resposta["answers"]["papel"] = {"type": "choice", "choice": "mencao", "confidence": 0.8}
    julgamento = mapear_resposta(ITEM, resposta)
    assert julgamento["p_entra"] == pytest.approx(0.0)
    assert julgamento["p_nao_entra"] == pytest.approx(0.8)  # so o papel, tipo e reportagem


def test_fonte_misto_quando_so_um_dos_dois_tem_probabilities():
    resposta = _resposta_completa()  # tipo_conteudo com probabilities
    resposta["answers"]["papel"] = {"type": "choice", "choice": "protagonista", "confidence": 0.8}
    assert mapear_resposta(ITEM, resposta)["prob_fonte"] == "misto"


def test_probabilidades_invalidas_caem_na_confidence():
    resposta = _resposta_completa()
    resposta["answers"]["papel"] = _choice_com("protagonista", {"protagonista": "0.9"}, 0.8)
    resposta["answers"]["tipo_conteudo"] = _choice_com("reportagem", {"reportagem": 0.5}, 0.9)
    julgamento = mapear_resposta(ITEM, resposta)
    assert julgamento["p_entra"] == pytest.approx(0.72)  # .8 * .9, pela confidence
    assert julgamento["prob_fonte"] == "confianca"
    assert julgamento["confianca"] == 0.8  # a confianca do julgamento nao muda


def test_confidence_invalida_sem_probabilities_vira_erro_de_valor():
    resposta = _resposta_completa()
    resposta["answers"]["papel"] = {"type": "choice", "choice": "protagonista", "confidence": [1]}
    with pytest.raises(ValueError):
        mapear_resposta(ITEM, resposta)


@pytest.mark.parametrize(
    ("item", "esperado"),
    [
        ({"papel": "protagonista", "tipo_conteudo": "reportagem",
          "p_entra": 0.9, "p_nao_entra": 0.05}, True),
        ({"papel": "mencao", "tipo_conteudo": "reportagem",
          "p_entra": 0.05, "p_nao_entra": 0.9}, False),
        # p_entra alto, mas o rotulo argmax (mencao) contradiz
        ({"papel": "mencao", "tipo_conteudo": "reportagem",
          "p_entra": 0.9, "p_nao_entra": 0.05}, None),
        # p_nao_entra alto, mas o rotulo argmax (protagonista) contradiz
        ({"papel": "protagonista", "tipo_conteudo": "reportagem",
          "p_entra": 0.05, "p_nao_entra": 0.9}, None),
        ({"papel": "protagonista", "tipo_conteudo": "reportagem"}, None),
        ({"papel": "protagonista", "tipo_conteudo": "reportagem", "p_entra": 0.9}, None),
    ],
)
def test_decisao_entrega(item, esperado):
    assert decisao_entrega(item, limiar=0.8) is esperado


def test_estado_jev_corta_o_trecho():
    item = {**ITEM, "trecho": "abcdefghij" * 3}
    assert estado_jev(item, trecho_max=10).endswith("Trecho: abcdefghij")
    assert estado_jev(item).endswith("abcdefghij" * 3)


def test_rotulo_modelo_jev_inclui_modelo_versao_e_trecho():
    assert rotulo_modelo_jev("jev-1.13.0", 1500) == "jev-1.13.0/q1/t1500"


@pytest.mark.parametrize(
    ("papel", "tipo", "esperado"),
    [
        ("protagonista", "reportagem", True),
        ("citada_com_fato", "reportagem", True),
        ("mencao", "reportagem", False),
        ("protagonista", "opiniao", False),
        ("errada", "opiniao", False),
    ],
)
def test_entra_por_rotulos(papel, tipo, esperado):
    assert entra_por_rotulos(papel, tipo) is esperado
