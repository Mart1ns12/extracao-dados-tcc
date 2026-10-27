"""Historias: republicacoes de uma mesma materia entre veiculos, e duplicatas do Google News.

Porte de validar_wp.py (canon, norm_titulo, casar, pares_republicacao, grupos_historias).
Funcoes puras: sem banco e sem rede.

Republicacao (WordPress x WordPress, veiculos diferentes): titulo normalizado igual, ou
SequenceMatcher >= 0.95, com |data| <= 1 dia. Os pares se unem por union-find: cada componente
e uma historia. O representante e o post de date_gmt mais antigo.

Duplicata do Google (origem google_news, dominio WordPress): casa com um post do MESMO veiculo
pela URL canonica ou por titulo igual / fuzzy >= 0.90, com |data| <= 2 dias.
"""

from __future__ import annotations

import datetime as dt
import difflib
import html
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from tcc_scraper.config import WP_SITES
from tcc_scraper.db.models import canonical_url

TOL_DIAS_REPUBLICACAO = 1
TOL_DIAS_GOOGLE = 2
LIMIAR_REPUBLICACAO = 0.95
LIMIAR_GOOGLE = 0.90

_ORDEM_SITE = {s.nome: i for i, s in enumerate(WP_SITES)}


@dataclass(frozen=True)
class Ref:
    """Post WordPress ou artigo do Google, so com o necessario para casar.

    site: chave de WP_SITES (raw_wp_posts.site). No Google, o veiculo do dominio.
    ident: wp_id (WordPress) ou article_id (Google). Com site, identifica o item.
    tnorm: titulo normalizado (norm_titulo). canon: URL canonica, ou None.
    pub: instante em UTC (date_gmt ou published_at). As janelas usam o dia UTC.
    """

    site: str
    ident: int
    tnorm: str
    canon: str | None
    pub: dt.datetime

    @property
    def chave(self) -> tuple[str, int]:
        return (self.site, self.ident)

    @property
    def dia(self) -> dt.date:
        return self.pub.date()


def _sem_acento(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def canon(url: str | None) -> str | None:
    """canonical_url do projeto + http->https, sem www., sem /amp, sem barra final."""
    if not url:
        return None
    u = canonical_url(url.strip())
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    p = urlsplit(u)
    host = p.netloc[4:] if p.netloc.startswith("www.") else p.netloc
    caminho = p.path[:-4] if p.path.endswith("/amp") else p.path
    caminho = caminho.rstrip("/") or "/"
    return urlunsplit((p.scheme, host, caminho, p.query, ""))


def norm_titulo(s: str | None, cortar_sufixo: bool = False) -> str:
    """Titulo sem tags, sem acento, em minusculas e so com [a-z0-9] separados por espaco.

    cortar_sufixo tira o " - Veiculo" que o Google News acrescenta (so se a cauda for curta).
    """
    if not s:
        return ""
    t = html.unescape(re.sub(r"<[^>]+>", " ", s))
    if cortar_sufixo and " - " in t:
        cabeca, _, cauda = t.rpartition(" - ")
        if len(cauda) < 40:
            t = cabeca
    t = _sem_acento(t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def ordem_site(site: str) -> int:
    """Posicao do veiculo em WP_SITES. Desempate do representante."""
    return _ORDEM_SITE.get(site, len(_ORDEM_SITE))


def pares_republicacao(refs: Sequence[Ref]) -> list[tuple[float, Ref, Ref]]:
    """Pares (similaridade, p, q) de veiculos diferentes, |dia| <= 1, titulo igual ou >= 0.95."""
    por_dia: dict[dt.date, list[Ref]] = defaultdict(list)
    for r in refs:
        por_dia[r.dia].append(r)
    pares: list[tuple[float, Ref, Ref]] = []
    for p in refs:
        if not p.tnorm:
            continue
        for k in (-TOL_DIAS_REPUBLICACAO, 0, TOL_DIAS_REPUBLICACAO):
            for q in por_dia.get(p.dia + dt.timedelta(days=k), []):
                if q.site == p.site or not q.tnorm:
                    continue
                if p.chave >= q.chave:  # cada par uma vez, do lado de menor chave
                    continue
                if p.tnorm == q.tnorm:
                    pares.append((1.0, p, q))
                    continue
                sm = difflib.SequenceMatcher(None, p.tnorm, q.tnorm)
                if (sm.real_quick_ratio() < LIMIAR_REPUBLICACAO
                        or sm.quick_ratio() < LIMIAR_REPUBLICACAO):
                    continue
                r = sm.ratio()
                if r >= LIMIAR_REPUBLICACAO:
                    pares.append((r, p, q))
    return pares


def grupos_historias(
    refs: Sequence[Ref], pares: Iterable[tuple[float, Ref, Ref]]
) -> list[list[Ref]]:
    """Componentes do union-find sobre os pares. Cada grupo vem ordenado por chave."""
    pai = {r.chave: r.chave for r in refs}

    def acha(x: tuple[str, int]) -> tuple[str, int]:
        while pai[x] != x:
            pai[x] = pai[pai[x]]
            x = pai[x]
        return x

    for _, p, q in pares:
        ra, rb = acha(p.chave), acha(q.chave)
        if ra != rb:
            pai[max(ra, rb)] = min(ra, rb)  # a raiz fica sempre a menor chave
    grupos: dict[tuple[str, int], list[Ref]] = defaultdict(list)
    for r in refs:
        grupos[acha(r.chave)].append(r)
    return [sorted(g, key=lambda r: r.chave) for _, g in sorted(grupos.items())]


def escolhe_representante(grupo: Sequence[Ref]) -> Ref:
    """Post de date_gmt mais antigo. Empate: ordem de WP_SITES, depois wp_id."""
    return min(grupo, key=lambda r: (r.pub, ordem_site(r.site), r.ident))


class IndiceWP:
    """Posts WordPress indexados por veiculo, por URL canonica e por dia (para casar o Google)."""

    def __init__(self, refs: Iterable[Ref]) -> None:
        self._canon: dict[tuple[str, str], list[Ref]] = defaultdict(list)
        self._dia: dict[tuple[str, dt.date], list[Ref]] = defaultdict(list)
        for r in sorted(refs, key=lambda r: r.chave):
            if r.canon:
                self._canon[(r.site, r.canon)].append(r)
            self._dia[(r.site, r.dia)].append(r)

    def por_canon(self, site: str, url: str) -> list[Ref]:
        return self._canon.get((site, url), [])

    def por_dia(self, site: str, dia: dt.date) -> list[Ref]:
        return self._dia.get((site, dia), [])


def casar(google: Ref, idx: IndiceWP) -> tuple[str | None, Ref | None]:
    """Post WordPress do mesmo veiculo que a materia do Google repete, e o metodo do casamento.

    Metodos (valor de staging_historias.metodo): google_url, google_titulo, google_fuzzy.
    Sem casamento: (None, None).
    """
    if google.canon:
        achados = idx.por_canon(google.site, google.canon)
        if achados:
            return "google_url", achados[0]
    if not google.tnorm:
        return None, None
    cands: list[Ref] = []
    for k in range(-TOL_DIAS_GOOGLE, TOL_DIAS_GOOGLE + 1):
        cands.extend(idx.por_dia(google.site, google.dia + dt.timedelta(days=k)))
    for p in cands:
        if p.tnorm == google.tnorm:
            return "google_titulo", p
    melhor: Ref | None = None
    melhor_r = 0.0
    for p in cands:
        if not p.tnorm:
            continue
        sm = difflib.SequenceMatcher(None, google.tnorm, p.tnorm)
        if sm.real_quick_ratio() < LIMIAR_GOOGLE or sm.quick_ratio() < LIMIAR_GOOGLE:
            continue  # filtro barato antes do ratio completo
        r = sm.ratio()
        if r >= LIMIAR_GOOGLE and r > melhor_r:
            melhor, melhor_r = p, r
    if melhor is not None:
        return "google_fuzzy", melhor
    return None, None
