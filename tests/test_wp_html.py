"""Limpeza do HTML dos posts do WordPress (enrich/wp_html.py). Sem rede e sem banco.

Os posts reais vem de tests/fixtures/wp_posts_amostra.json (copiados do cache de 2026-03).
Cada teste de fixture confere um efeito concreto da limpeza; os sinteticos cobrem R1-R4.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tcc_scraper.enrich.wp_html import (
    PREFIXOS_RELACIONADOS,
    R1_TAGS,
    limpar_html,
    titulo_texto,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _posts() -> dict[int, dict]:
    lista = json.loads((FIXTURES / "wp_posts_amostra.json").read_text(encoding="utf-8"))
    return {p["id"]: p for p in lista}


def _blocos(post_id: int) -> list[str]:
    return limpar_html(_posts()[post_id]["content"]["rendered"])


def _junto(blocos: list[str]) -> str:
    return " ".join(blocos)


# --- efeitos sobre os posts reais -------------------------------------------------------


def test_fixture_tem_os_quatro_posts_citados() -> None:
    assert set(_posts()) == {3254852, 3258372, 1315446, 5671909}


def test_taesa_card_de_relacionadas_sai_do_texto() -> None:
    # ITSA4 so aparece num <ul> de links "relacionados" (R4); o corpo da materia fica
    junto = _junto(_blocos(3254852))
    assert "ITSA4" not in junto
    assert "CSNA3" not in junto
    assert "As ações da Taesa (TAEE11) atravessam" in junto
    assert "R$ 46,11" in junto


def test_greve_card_relacionado_com_titulo_sai_do_texto() -> None:
    # "Goldman vê espaço" vem de um <a> com <h3> dentro (R2), fora dos <p> do corpo
    junto = _junto(_blocos(3258372))
    assert "Goldman vê espaço" not in junto
    assert "Leia também" not in junto
    assert "Na mesma linha, o Goldman Sachs" in junto  # o corpo com o mesmo banco continua


def test_money_times_leia_tambem_em_li_sai() -> None:
    blocos = _blocos(1315446)
    assert not any("LEIA TAMB" in b.upper() for b in blocos)
    assert "Tenha acesso" not in _junto(blocos)
    assert blocos[0].startswith("A Petrobras (PETR4) informou")


def test_exame_link_inline_dentro_de_frase_nao_remove_o_paragrafo() -> None:
    # o unico <a> do post esta no meio de uma frase: R2 e R4 nao podem derrubar o <p>
    blocos = _blocos(5671909)
    assert any(
        b.startswith("Os dados indicam um patamar estável de intenção de voto para Lula,")
        for b in blocos
    )
    # os <li> com os cenarios (sem link) continuam
    assert "Flávio Bolsonaro: 46%" in blocos
    assert "Eduardo Leite: 36%" in blocos


def test_titulo_do_post_real_vira_texto_puro() -> None:
    assert titulo_texto(_posts()[3254852]["title"]["rendered"]) == (
        "Taesa (TAEE11) recua após topo histórico; o que diz a análise técnica?"
    )
    assert titulo_texto(_posts()[5671909]["title"]["rendered"]) == (
        "Lula e Flávio empatam numericamente no 2º turno, diz pesquisa BTG/Nexus"
    )


# --- R1: elementos inteiros -------------------------------------------------------------

_R1_SNIPPETS = {
    "img": '<img src="x.jpg">',
    "figure": "<figure><p>INTRUSO</p></figure>",
    "table": "<table><tr><td><p>INTRUSO</p></td></tr></table>",
    "form": "<form><p>INTRUSO</p></form>",
}


@pytest.mark.parametrize("tag", R1_TAGS)
def test_r1_remove_tag_de_midia_e_embed(tag: str) -> None:
    snippet = _R1_SNIPPETS.get(tag, f"<{tag}>INTRUSO</{tag}>")
    blocos = limpar_html(f"<p>Antes</p>{snippet}<p>Depois</p>")
    assert blocos == ["Antes", "Depois"]


def test_r1_blockquote_de_embed_sai_e_citacao_comum_fica() -> None:
    html = (
        '<blockquote class="twitter-tweet"><p>INTRUSO</p></blockquote>'
        "<blockquote>Citacao comum</blockquote>"
    )
    assert limpar_html(html) == ["Citacao comum"]


# --- R2: <a> que envolve bloco ----------------------------------------------------------


def test_r2_a_com_bloco_dentro_sai_inteiro() -> None:
    html = (
        "<p>Antes</p>"
        '<a href="/relacionada"><h3>Card relacionado</h3><span>resumo</span></a>'
        "<p>Depois</p>"
    )
    assert limpar_html(html) == ["Antes", "Depois"]


def test_r2_link_inline_sem_bloco_continua_no_paragrafo() -> None:
    html = '<p>Veja o <a href="/x">link</a> aqui.</p>'
    assert limpar_html(html) == ["Veja o link aqui."]


# --- R3: chamadas de relacionados e assinatura ------------------------------------------


def test_r3_prefixos_com_e_sem_acento_saem() -> None:
    html = (
        "<p>Corpo do texto.</p>"
        "<p>Confira nossas análises do setor</p>"
        "<p>Assine a newsletter</p>"
        "<p>Veja o gráfico abaixo.</p>"
    )
    assert limpar_html(html) == ["Corpo do texto.", "Veja o gráfico abaixo."]


def test_r3_vale_para_li_e_so_pelo_inicio_do_texto() -> None:
    html = "<ul><li>Saiba mais: outra materia</li><li>Item comum da lista</li></ul>"
    assert limpar_html(html) == ["Item comum da lista"]
    # prefixo no meio do texto nao conta
    assert limpar_html("<p>O leitor pode ler também isto.</p>") == [
        "O leitor pode ler também isto."
    ]


def test_prefixos_relacionados_cobrem_a_lista_do_plano() -> None:
    assert PREFIXOS_RELACIONADOS == (
        "leia tambem", "leia mais", "veja tambem", "veja mais", "confira tambem",
        "confira nossas analises", "confira mais", "saiba mais", "mais lidas",
        "assine", "inscreva-se", "baixe o app",
    )


# --- R4: bloco que e quase todo link ----------------------------------------------------


def test_r4_paragrafo_so_de_links_sai() -> None:
    html = '<p><a href="/a">Materia A</a> <a href="/b">Materia B</a></p><p>Texto normal.</p>'
    assert limpar_html(html) == ["Texto normal."]


def test_r4_lista_so_de_links_sai_inteira() -> None:
    html = '<p>Antes</p><ul><li><a href="/a">Um</a></li><li><a href="/b">Dois</a></li></ul>'
    assert limpar_html(html) == ["Antes"]


def test_r4_paragrafo_com_pouco_link_fica() -> None:
    html = (
        '<p>Leia <a href="/a">este link de sete palavras</a> para entender a regra '
        "do mercado hoje agora.</p>"
    )
    assert limpar_html(html) == [
        "Leia este link de sete palavras para entender a regra do mercado hoje agora."
    ]


# --- blocos, texto e casos de borda -----------------------------------------------------


def test_texto_partido_por_tag_inline_nao_ganha_espaco() -> None:
    # regex <[^>]+> -> espaco transformaria "Azu</strong>l" em "Azu l"
    assert limpar_html("<p>A empresa <strong>Azu</strong>l subiu hoje.</p>") == [
        "A empresa Azul subiu hoje."
    ]


def test_blocos_ignoram_p_dentro_de_blockquote_e_mantem_h2() -> None:
    html = "<h2>Subtitulo</h2><blockquote><p>Citacao</p></blockquote><p>Depois</p>"
    assert limpar_html(html) == ["Subtitulo", "Citacao", "Depois"]


def test_sem_bloco_usa_texto_da_raiz() -> None:
    assert limpar_html("<span>So texto solto</span>") == ["So texto solto"]


def test_vazio_ou_nulo_devolve_lista_vazia() -> None:
    assert limpar_html(None) == []
    assert limpar_html("") == []
    assert limpar_html("   \n ") == []


def test_titulo_texto_decodifica_entidades_e_tira_tags() -> None:
    assert titulo_texto("&#8220;Em cinco anos&#8221;, diz CEO") == "“Em cinco anos”, diz CEO"
    assert titulo_texto("AT&amp;T e <em>Taesa</em> recuam") == "AT&T e Taesa recuam"
    assert titulo_texto("  Espaços   demais  ") == "Espaços demais"
    assert titulo_texto(None) == ""
    assert titulo_texto("") == ""
