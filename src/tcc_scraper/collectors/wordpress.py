"""Coleta de posts dos veiculos com API WordPress (InfoMoney, Money Times, Exame).

Por que esta fonte: a API REST do WordPress entrega TODOS os posts do periodo, com data
exata (date_gmt) e o HTML do corpo. O Google News nao traz o corpo completo e perde parte
das materias desses veiculos. Medido em 2026-10: 216 requisicoes sem erro com throttle de 1 s.

Este modulo so baixa e grava. O casamento de tickers, o agrupamento em historias e a
classificacao ficam em enrich/processar_wp.py. Por isso o pre-filtro de armazenamento e
BRUTO: guarda todo post que possa mencionar um ticker e deixa o casador decidir depois.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Iterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from lxml import html as lxml_html

from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.config import WP_SITES, SiteWP
from tcc_scraper.db.models_fontes import RawWpPost
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.enrich.casador import construir_regras, tem_ocorrencia_bruta
from tcc_scraper.enrich.wp_html import titulo_texto

logger = logging.getLogger(__name__)

PER_PAGE = 100
# Campos pedidos a API: o resto do JSON do post nao e usado e so pesaria na rede.
FIELDS = "id,date_gmt,link,title,content,type"
# Um mes cujo fim ficou ha mais de DIAS_FECHADO dias e considerado fechado: o total dele
# nao deve mudar mais, entao divergencia vira erro. No mes corrente o total muda enquanto o
# site publica, por isso so ha aviso.
DIAS_FECHADO = 2


def _cabecalho_inteiro(resposta, nome: str) -> int | None:
    """Valor inteiro de um cabecalho HTTP, sem diferenciar maiusculas. Ausente vira None."""
    alvo = nome.lower()
    for chave, valor in resposta.headers.items():
        if chave.lower() == alvo:
            return int(valor) if valor.strip() else None
    return None


def meses_do_intervalo(ini: dt.date, fim: dt.date) -> Iterator[tuple[dt.date, dt.date]]:
    """Divide [ini, fim) nas fronteiras de mes. Cada pedaco termina no proximo dia 1."""
    cursor = ini
    while cursor < fim:
        proximo = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield cursor, min(proximo, fim)
        cursor = proximo


def _data_gmt(item: dict) -> dt.datetime:
    """date_gmt da API vem naive, em UTC. Vira aware para comparar com os limites."""
    return dt.datetime.fromisoformat(item["date_gmt"]).replace(tzinfo=dt.UTC)


def texto_bruto(html_str: str | None) -> str:
    """Texto do corpo SEM limpeza, para o pre-filtro de armazenamento.

    Une dois textos: text_content() da raiz (junta "Azu</strong>l" em "Azul") e o itertext
    separado por espaco (nao funde a ultima palavra de um paragrafo com a primeira do
    seguinte). A uniao so acrescenta texto, entao nunca perde um ticker que o outro veria.
    """
    if not html_str or not html_str.strip():
        return ""
    raiz = lxml_html.fragment_fromstring(html_str, create_parent="div")
    return raiz.text_content() + "\n" + " ".join(raiz.itertext())


def _linha_raw(site: SiteWP, item: dict, data: dt.datetime, agora: dt.datetime) -> dict:
    return {
        "site": site.nome,
        "wp_id": item["id"],
        "link": item["link"],
        "date_gmt": data,
        "wp_type": item["type"],
        "titulo_html": item["title"]["rendered"],
        "content_html": item["content"]["rendered"],
        "fetched_at": agora,
    }


class WordPressCollector(Collector):
    """Coleta os posts de cada veiculo WordPress, por (site x mes)."""

    name = "wordpress"

    def __init__(
        self,
        desde: dt.date | None = None,
        ate: dt.date | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if (desde is None) != (ate is None):
            raise ValueError("desde e ate devem ser informados juntos")
        if desde is None:
            # inicio_noticias ja e data_start - 31 d: e a margem que aquece o boilerplate
            # causal (30 d) com 1 d de folga. Subtrair 31 de novo puxaria 62 d antes.
            # ate e exclusivo: fim_coleta + 1 d inclui o dia inteiro de fim_coleta.
            desde = self.settings.inicio_noticias
            ate = self.settings.fim_coleta + dt.timedelta(days=1)
        # desde/ate: recorte manual (wordpress --desde/--ate). ate e exclusivo.
        self.desde = desde
        self.ate = ate
        for site in WP_SITES:
            host = urlsplit(site.endpoint).netloc.lower()
            self.throttle.configure(host, self.settings.throttle_wordpress)
        self._regras = construir_regras()

    def work_units(self) -> Iterable[WorkUnit]:
        for site in WP_SITES:
            for ini, fim in meses_do_intervalo(self.desde, self.ate):
                yield WorkUnit(
                    key=f"{site.nome}|{ini:%Y-%m-%d}|{fim:%Y-%m-%d}",
                    payload={"site": site, "ini": ini, "fim": fim},
                )

    def _baixar_janela(
        self, site: SiteWP, ini: dt.date, fim: dt.date
    ) -> tuple[dict[int, dict], int]:
        """Pagina a API ate TotalPages. Devolve os posts unicos por id e o X-WP-Total.

        A janela da consulta tem 1 dia de margem nos dois lados: o WP filtra pela data local
        e o recorte exato em UTC e feito depois, em process.
        """
        base = {
            "after": f"{ini - dt.timedelta(days=1):%Y-%m-%d}T00:00:00",
            "before": f"{fim + dt.timedelta(days=1):%Y-%m-%d}T00:00:00",
            "per_page": PER_PAGE,
            "_fields": FIELDS,
            "orderby": "date",
            "order": "asc",
        }
        posts: dict[int, dict] = {}
        total = 0
        total_paginas = 1
        pagina = 1
        while pagina <= total_paginas:
            resposta = self.fetch(site.endpoint, params={**base, "page": pagina})
            resposta.raise_for_status()
            if pagina == 1:
                total = _cabecalho_inteiro(resposta, "X-WP-Total") or 0
                total_paginas = max(_cabecalho_inteiro(resposta, "X-WP-TotalPages") or 0, 1)
            for item in resposta.json():
                # Post publicado durante a paginacao aparece em duas paginas: fica uma vez.
                posts.setdefault(item["id"], item)
            pagina += 1
        return posts, total

    def _conferir_total(
        self, site: SiteWP, ini: dt.date, fim: dt.date, obtidos: int, total: int
    ) -> None:
        if obtidos == total:
            return
        hoje = dt.datetime.now(ZoneInfo(self.settings.daily_tz)).date()
        resumo = f"{site.nome} {ini:%Y-%m}: {obtidos} posts unicos, X-WP-Total={total}"
        if fim < hoje - dt.timedelta(days=DIAS_FECHADO):
            raise RuntimeError(f"wordpress: divergencia de total em mes fechado ({resumo})")
        logger.warning("wordpress: total mudou no mes corrente (%s)", resumo)

    def _tem_ticker_bruto(self, item: dict) -> bool:
        titulo = titulo_texto(item["title"]["rendered"])
        return tem_ocorrencia_bruta(titulo, texto_bruto(item["content"]["rendered"]), self._regras)

    def process(self, unit: WorkUnit) -> UnitResult:
        site: SiteWP = unit.payload["site"]
        ini: dt.date = unit.payload["ini"]
        fim: dt.date = unit.payload["fim"]

        posts, total = self._baixar_janela(site, ini, fim)
        self._conferir_total(site, ini, fim, len(posts), total)

        inicio_utc = dt.datetime.combine(ini, dt.time.min, tzinfo=dt.UTC)
        fim_utc = dt.datetime.combine(fim, dt.time.min, tzinfo=dt.UTC)
        agora = dt.datetime.now(dt.UTC)
        linhas = []
        for item in posts.values():
            data = _data_gmt(item)
            if not (inicio_utc <= data < fim_utc):
                continue
            if not self._tem_ticker_bruto(item):
                continue
            linhas.append(_linha_raw(site, item, data, agora))

        logger.info(
            "wordpress %s %s..%s: %d posts na consulta, %d no intervalo e candidatos",
            site.nome, ini, fim, len(posts), len(linhas),
        )
        self._persist(linhas)
        return UnitResult(items_found=len(linhas))

    def _persist(self, linhas: list[dict]) -> None:
        """Grava so os candidatos, em lote. Append-only: post ja gravado nao e reescrito."""
        if not linhas:
            return
        with session_scope() as session:
            bulk_upsert(
                session,
                RawWpPost.__table__,
                linhas,
                index_elements=["site", "wp_id"],
                update_columns=None,
            )
