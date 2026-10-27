"""Limpeza do HTML dos posts do WordPress: sobram so os blocos de texto da materia.

O corpo da API do WordPress traz de tudo (cards de "leia tambem", embeds, figuras, listas de
links). Este modulo tira esse ruido em quatro regras e devolve os paragrafos restantes, que
alimentam o casador de tickers e o boilerplate. Funcoes puras: sem banco nem rede.

Porte da v3 do validar_wp.py. As regras R1-R4 sao aplicadas nesta ordem:
  R1 remove elementos inteiros (midia, embeds, tabelas, formularios);
  R2 remove <a> que contem bloco (cards de materia relacionada);
  R3 remove p/li cujo texto comeca com um dos PREFIXOS_RELACIONADOS;
  R4 remove p/li/ul/ol em que quase todo o texto e link.
"""

from __future__ import annotations

import unicodedata

from lxml import html as lxml_html

R1_TAGS = (
    "script", "style", "iframe", "figure", "figcaption", "img", "video", "audio",
    "noscript", "table", "form", "button", "svg",
)
R1_CLASSES_BLOCKQUOTE = ("twitter-tweet", "instagram-media", "tiktok-embed")
PREFIXOS_RELACIONADOS = (
    "leia tambem", "leia mais", "veja tambem", "veja mais", "confira tambem",
    "confira nossas analises", "confira mais", "saiba mais", "mais lidas",
    "assine", "inscreva-se", "baixe o app",
)
LIMITE_LINK = 0.8  # R4: fracao do texto dentro de <a> a partir da qual o bloco e descartado


def _sem_acento(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _txt(el: lxml_html.HtmlElement) -> str:
    # text_content() junta o texto de todos os filhos sem espaco extra: "Azu</strong>l" vira "Azul"
    return " ".join(el.text_content().split())


def _remove(el: lxml_html.HtmlElement) -> None:
    if el.getparent() is not None:
        el.drop_tree()  # remove o elemento e o conteudo, preserva o tail


def limpar_html(html_str: str | None) -> list[str]:
    """Blocos de texto do corpo, na ordem do documento, depois de R1-R4."""
    if not html_str or not html_str.strip():
        return []
    raiz = lxml_html.fragment_fromstring(html_str, create_parent="div")
    # R1: elementos inteiros
    for el in list(raiz.iter(*R1_TAGS)):
        _remove(el)
    for el in list(raiz.iter("blockquote")):
        if any(c in (el.get("class") or "") for c in R1_CLASSES_BLOCKQUOTE):
            _remove(el)
    # R2: <a> que envolve bloco (card de materia relacionada)
    for el in list(raiz.iter("a")):
        if el.getparent() is not None and el.xpath(
            ".//div|.//h1|.//h2|.//h3|.//h4|.//h5|.//h6|.//p|.//img|.//figure"
        ):
            _remove(el)
    # R3: chamadas de relacionados e de assinatura
    for el in list(raiz.iter("p", "li")):
        if el.getparent() is not None and _sem_acento(_txt(el)).strip().startswith(
            PREFIXOS_RELACIONADOS
        ):
            _remove(el)
    # R4: bloco que e quase todo link
    for el in list(raiz.iter("p", "li", "ul", "ol")):
        if el.getparent() is None:
            continue
        total = _txt(el)
        if not total:
            continue
        link = sum(len(_txt(a)) for a in el.iter("a"))
        if link / len(total) >= LIMITE_LINK:
            _remove(el)
    # blocos: p, li, h2-h6 e blockquote que nao estao dentro de outro desse conjunto
    conjunto = list(raiz.iter("p", "li", "h2", "h3", "h4", "h5", "h6", "blockquote"))
    marcados = set(conjunto)
    blocos = []
    for el in conjunto:
        if any(anc in marcados for anc in el.iterancestors()):
            continue
        txt = _txt(el)
        if txt:
            blocos.append(txt)
    if not blocos:
        txt_raiz = _txt(raiz)
        if txt_raiz:
            blocos = [txt_raiz]
    return blocos


def titulo_texto(titulo_html: str | None) -> str:
    """Titulo do WordPress (title.rendered) em texto puro, sem tags e com entidades decodificadas.

    text_content() do lxml ja decodifica as entidades (&#8220; vira aspas), por isso nao ha
    html.unescape depois: decodificar duas vezes trocaria "&amp;quot;" por aspas.
    """
    if not titulo_html or not titulo_html.strip():
        return ""
    raiz = lxml_html.fragment_fromstring(titulo_html, create_parent="div")
    return " ".join(raiz.text_content().split())
