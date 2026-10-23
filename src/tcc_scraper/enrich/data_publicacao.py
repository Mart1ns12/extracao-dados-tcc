"""Hora real da publicacao, lida do HTML da propria materia.

Usada quando o RSS do Google News so trouxe a data (published_precision='date'). A pagina
do veiculo costuma trazer o horario exato em metadados padronizados, o que e mais confiavel
que qualquer palpite sobre o fuso. Funcoes puras: recebem HTML ou datas, sem banco nem rede.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable, Iterator
from zoneinfo import ZoneInfo

from lxml import etree
from lxml import html as lxml_html

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")

# Nomes de <meta property/name/itemprop> com a data de publicacao. Em ordem de preferencia:
# o article: vem antes do og:, e o og: antes das variantes.
_META_ARTIGO = ("article:published_time",)
_META_OG = (
    "og:published_time",
    "og:article:published_time",
    "article:published",
    "pubdate",
    "publishdate",
    "publish_date",
)

# So aceita valor com horario: "2026-10-20" sozinho nao traz hora para refinar.
_TEM_HORARIO = re.compile(r"\d{1,2}:\d{2}")


def _datetime_com_horario(valor: object) -> dt.datetime | None:
    """Parseia um ISO 8601 que tenha hora E fuso. Sem fuso devolve None (nao adivinha)."""
    if not isinstance(valor, str) or not valor.strip():
        return None
    texto = valor.strip()
    if not _TEM_HORARIO.search(texto):
        return None
    try:
        dado = dt.datetime.fromisoformat(texto)
    except ValueError:
        return None
    if dado.tzinfo is None:
        return None
    return dado


def _metas(raiz: lxml_html.HtmlElement, nomes: Iterable[str]) -> Iterator[str]:
    """Conteudos de <meta> cujo property ou name esta em `nomes`, na ordem do HTML."""
    for el in raiz.iter("meta"):
        chaves = {(el.get(atributo) or "").strip().lower() for atributo in ("property", "name")}
        if chaves & set(nomes) and el.get("content"):
            yield el.get("content")


def _itemprop_date_published(raiz: lxml_html.HtmlElement) -> Iterator[str]:
    """Valor de qualquer elemento com itemprop=datePublished (content, datetime ou texto)."""
    for el in raiz.iter():
        if not isinstance(el.tag, str):  # comentarios e instrucoes de processamento
            continue
        if (el.get("itemprop") or "").strip().lower() == "datepublished":
            yield el.get("content") or el.get("datetime") or el.text_content()


def _date_published_em(objeto: object) -> Iterator[object]:
    """datePublished em qualquer nivel do JSON-LD: objeto, lista ou @graph."""
    if isinstance(objeto, list):
        for item in objeto:
            yield from _date_published_em(item)
    elif isinstance(objeto, dict):
        if "datePublished" in objeto:
            yield objeto["datePublished"]
        for valor in objeto.values():
            if isinstance(valor, (list, dict)):
                yield from _date_published_em(valor)


def _datas_json_ld(raiz: lxml_html.HtmlElement) -> Iterator[object]:
    """datePublished de todos os blocos <script type="application/ld+json">."""
    for script in raiz.iter("script"):
        if "ld+json" not in (script.get("type") or "").lower():
            continue
        try:
            conteudo = json.loads(script.text or "")
        except json.JSONDecodeError:
            continue
        yield from _date_published_em(conteudo)


def extrair_data_publicacao(html: str) -> dt.datetime | None:
    """Hora de publicacao do HTML da materia, em UTC (aware), ou None.

    Ordem: <meta article:published_time>, metas og: e variantes, itemprop=datePublished,
    JSON-LD datePublished e <time datetime>. O primeiro valor com hora e fuso vence.
    Valor sem horario ou sem fuso e ignorado, e a busca segue para a proxima fonte.
    """
    if not html or not html.strip():
        return None
    try:
        raiz = lxml_html.document_fromstring(html)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return None

    fontes = (
        _metas(raiz, _META_ARTIGO),
        _metas(raiz, _META_OG),
        _itemprop_date_published(raiz),
        _datas_json_ld(raiz),
        (el.get("datetime") for el in raiz.iter("time")),
    )
    for fonte in fontes:
        for valor in fonte:
            dado = _datetime_com_horario(valor)
            if dado is not None:
                return dado.astimezone(dt.UTC)
    return None


def refinar_publicacao(publicado_em: dt.datetime, extraida: dt.datetime) -> dt.datetime | None:
    """Hora da pagina, se a data dela cair no mesmo dia (SP) ou no dia vizinho da gravada.

    A tolerancia de 1 dia cobre a diferenca entre o fuso do Pacifico, de onde vem a data
    gravada, e o fuso em que a pagina publica. Fora disso a hora da pagina nao e confiavel
    e a data gravada prevalece. Devolve a hora em UTC ou None.
    """
    dia_gravado = publicado_em.astimezone(FUSO_BRASIL).date()
    dia_pagina = extraida.astimezone(FUSO_BRASIL).date()
    if abs((dia_pagina - dia_gravado).days) > 1:
        return None
    return extraida.astimezone(dt.UTC)
