"""Testes da extracao da hora de publicacao a partir do HTML da materia.

Sem rede: as paginas sao fixtures pequenas, inline. Cada teste cobre uma das fontes da
ordem de preferencia, mais os casos em que a hora nao pode ser usada.
"""

from __future__ import annotations

import datetime as dt

from tcc_scraper.enrich.data_publicacao import extrair_data_publicacao, refinar_publicacao


def test_meta_article_published_time_vence_e_vai_para_utc() -> None:
    html = (
        '<html><head><meta property="article:published_time" '
        'content="2026-10-20T10:15:00-03:00"></head></html>'
    )
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 13, 15, tzinfo=dt.UTC)


def test_og_published_time_quando_nao_ha_article() -> None:
    html = '<meta property="og:published_time" content="2026-10-20T10:00:00Z">'
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 10, 0, tzinfo=dt.UTC)


def test_meta_article_tem_preferencia_sobre_json_ld() -> None:
    html = (
        '<meta property="article:published_time" content="2026-10-20T09:00:00+00:00">'
        '<script type="application/ld+json">'
        '{"datePublished": "2026-10-20T12:00:00+00:00"}</script>'
    )
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 9, 0, tzinfo=dt.UTC)


def test_json_ld_com_graph_e_lista() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@graph": [{"@type": "WebPage"}, '
        '{"@type": "NewsArticle", "datePublished": "2026-10-20T10:00:00-03:00"}]}'
        "</script>"
    )
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 13, 0, tzinfo=dt.UTC)


def test_json_ld_em_lista_de_objetos() -> None:
    html = (
        '<script type="application/ld+json">'
        '[{"@type": "Organization"}, {"datePublished": "2026-10-20T07:30:00Z"}]'
        "</script>"
    )
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 7, 30, tzinfo=dt.UTC)


def test_json_ld_invalido_e_ignorado() -> None:
    html = (
        '<script type="application/ld+json">{nao e json</script>'
        '<time datetime="2026-10-20T08:00:00+00:00"></time>'
    )
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 8, 0, tzinfo=dt.UTC)


def test_itemprop_date_published_le_o_atributo_content() -> None:
    html = '<meta itemprop="datePublished" content="2026-10-20T11:45:00-03:00">'
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 14, 45, tzinfo=dt.UTC)


def test_time_datetime_com_hora_e_fuso() -> None:
    html = '<p>Publicado em <time datetime="2026-10-20T16:20:00+00:00">20/10</time></p>'
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 16, 20, tzinfo=dt.UTC)


def test_so_data_nao_serve() -> None:
    """Sem horario nao ha o que refinar: a data gravada continua valendo."""
    html = '<time datetime="2026-10-20">20 de outubro</time>'
    assert extrair_data_publicacao(html) is None


def test_sem_fuso_devolve_none_em_vez_de_adivinhar() -> None:
    html = '<meta property="article:published_time" content="2026-10-20T10:00:00">'
    assert extrair_data_publicacao(html) is None


def test_valor_sem_fuso_e_ignorado_e_a_busca_segue() -> None:
    html = (
        '<meta property="article:published_time" content="2026-10-20T10:00:00">'
        '<time datetime="2026-10-20T12:00:00-03:00"></time>'
    )
    assert extrair_data_publicacao(html) == dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)


def test_pagina_sem_data_devolve_none() -> None:
    assert extrair_data_publicacao("<html><body><p>so texto</p></body></html>") is None


def test_html_vazio_devolve_none() -> None:
    assert extrair_data_publicacao("") is None
    assert extrair_data_publicacao("   ") is None


def test_refinar_aceita_mesmo_dia_e_dia_vizinho() -> None:
    gravada = dt.datetime(2026, 10, 19, 3, 0, tzinfo=dt.UTC)  # 00:00 de SP de 19/10
    mesmo_dia = dt.datetime(2026, 10, 19, 14, 30, tzinfo=dt.UTC)
    dia_vizinho = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)  # 09:00 de SP de 20/10

    assert refinar_publicacao(gravada, mesmo_dia) == mesmo_dia
    assert refinar_publicacao(gravada, dia_vizinho) == dia_vizinho


def test_refinar_recusa_hora_de_outro_dia() -> None:
    gravada = dt.datetime(2026, 10, 19, 3, 0, tzinfo=dt.UTC)
    dois_dias_depois = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)
    assert refinar_publicacao(gravada, dois_dias_depois) is None
