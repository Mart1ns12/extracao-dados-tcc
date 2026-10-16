"""Schema pydantic da analise diaria e funcoes de parse (sem rede)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from tcc_scraper.llm.parse import (
    filtrar_universo,
    item_hash,
    normalizar_url,
    parse_resposta,
    resolver_redirects,
)
from tcc_scraper.llm.schema import AcaoInfluenciada, AnaliseDiaria, NoticiaEmpresa

FIXTURE = Path(__file__).parent / "fixtures" / "gemini_analise_diaria.json"


def _fixture_texto() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _fixture_dict() -> dict:
    return json.loads(_fixture_texto())


def test_fixture_valida_no_schema():
    analise = AnaliseDiaria.model_validate(_fixture_dict())
    assert len(analise.noticias_empresas) == 3
    assert len(analise.noticias_macro) == 2
    assert len(analise.acoes_influenciadas) == 3
    assert len(analise.mercados_em_alta) == 2


def test_ticker_fora_do_universo_e_descartado(caplog):
    analise = parse_resposta(_fixture_texto())
    with caplog.at_level(logging.WARNING):
        filtrada = filtrar_universo(analise)
    assert [n.ticker for n in filtrada.noticias_empresas] == ["PETR4", "VALE3"]
    assert [a.ticker for a in filtrada.acoes_influenciadas] == ["PETR4", "VALE3"]
    assert "XXXX9" in caplog.text


def test_enum_invalido_falha_com_setor():
    dados = _fixture_dict()
    dados["noticias_empresas"][0]["setor"] = "cripto"
    with pytest.raises(ValidationError, match="setor"):
        AnaliseDiaria.model_validate(dados)


def test_ticker_e_normalizado():
    n = NoticiaEmpresa(
        ticker=" petr4.sa ", titulo="t", resumo="r",
        setor="petroleo_gas", sentimento="neutro", relevancia=0.5,
    )
    assert n.ticker == "PETR4"
    a = AcaoInfluenciada(
        ticker=" vale3.SA ", direcao_esperada="baixa", confianca=0.5,
        motivo="m", setor="mineracao_siderurgia",
    )
    assert a.ticker == "VALE3"


def test_data_invalida_vira_none():
    n = NoticiaEmpresa(
        ticker="PETR4", titulo="t", resumo="r", publicado_em="ontem",
        setor="petroleo_gas", sentimento="neutro", relevancia=0.5,
    )
    assert n.publicado_em is None
    vazia = NoticiaEmpresa(
        ticker="PETR4", titulo="t", resumo="r", publicado_em="",
        setor="petroleo_gas", sentimento="neutro", relevancia=0.5,
    )
    assert vazia.publicado_em is None


def test_normalizar_url():
    assert normalizar_url(" HTTPS://Exemplo.com/a/?utm_source=x ") == "https://exemplo.com/a"
    assert normalizar_url("exemplo.com/a") is None
    assert normalizar_url(None) is None


def test_item_hash_combina_url_e_titulo():
    assert item_hash("https://x.com/a?utm_source=1", "T") == item_hash(
        "https://x.com/a?utm_source=2", " t "
    )
    # mesmo artigo citado para dois fatos distintos: chaves diferentes
    assert item_hash("https://x.com/a", "fato 1") != item_hash("https://x.com/a", "fato 2")
    assert item_hash(None, "  Título   X ") == item_hash(None, "título x")
    assert item_hash(None, "titulo a") != item_hash(None, "titulo b")


def test_parse_resposta_remove_cerca_json():
    texto = "```json\n" + _fixture_texto() + "\n```"
    analise = parse_resposta(texto)
    assert analise.data_referencia.isoformat() == "2026-10-14"


VERTEX = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc123"
REAL = "https://br.advfn.com/jornal/2026/10/materia"


def _analise_com_vertex() -> AnaliseDiaria:
    dados = _fixture_dict()
    dados["noticias_empresas"][0]["url"] = VERTEX
    dados["noticias_macro"][0]["url"] = VERTEX
    return AnaliseDiaria.model_validate(dados)


def test_resolver_redirects_troca_url_vertexaisearch():
    resolvidas = []

    def falso(url):
        resolvidas.append(url)
        return REAL

    out = resolver_redirects(_analise_com_vertex(), falso)
    assert out.noticias_empresas[0].url == REAL
    assert out.noticias_macro[0].url == REAL
    assert resolvidas == [VERTEX]


def test_resolver_redirects_mantem_url_normal_sem_chamar_resolvedor():
    dados = _fixture_dict()
    dados["noticias_empresas"][1]["url"] = "https://exemplo.com/materia"
    analise = AnaliseDiaria.model_validate(dados)
    resolvidas = []

    out = resolver_redirects(analise, lambda url: resolvidas.append(url) or REAL)
    assert out.noticias_empresas[1].url == "https://exemplo.com/materia"
    assert resolvidas == []


def test_resolver_redirects_mantem_original_quando_resolvedor_devolve_none():
    out = resolver_redirects(_analise_com_vertex(), lambda url: None)
    assert out.noticias_empresas[0].url == VERTEX
    assert out.noticias_macro[0].url == VERTEX


def test_resolver_redirects_chama_uma_vez_por_url_repetida():
    dados = _fixture_dict()
    dados["noticias_empresas"][0]["url"] = VERTEX
    dados["noticias_empresas"][1]["url"] = VERTEX
    dados["noticias_macro"][0]["url"] = VERTEX
    analise = AnaliseDiaria.model_validate(dados)
    resolvidas = []

    def falso(url):
        resolvidas.append(url)
        return REAL

    out = resolver_redirects(analise, falso)
    assert resolvidas == [VERTEX]
    assert out.noticias_empresas[0].url == REAL
    assert out.noticias_empresas[1].url == REAL
    assert out.noticias_macro[0].url == REAL


def test_url_de_imagem_ou_redirect_nao_resolvido_vira_none():
    assert normalizar_url("https://site.com/wp-content/uploads/2026/10/foto.JPG") is None
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/x"
    assert normalizar_url(redirect) is None
    assert normalizar_url("https://site.com/noticia/petrobras") == "https://site.com/noticia/petrobras"
    assert item_hash("https://site.com/a.png", "Titulo") == item_hash(None, "titulo")
