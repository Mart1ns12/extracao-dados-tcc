"""Coleta de noticias historicas via RSS do Google News.

Por que esta fonte: e o unico caminho gratuito para 2 anos de historico de noticias.
A Marketaux limita a 3 artigos por requisicao sem historico; o GDELT DOC API bloqueia
por rate limit; os arquivos brutos do GDELT dariam ~300 GB. Medido em 2026-09-07.

O parametro que torna isso possivel e o `after:`/`before:` da busca do Google, que o
RSS respeita -- devolve materias com data correta de qualquer periodo.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.parse
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from lxml import etree
from sqlalchemy import select

from tcc_scraper.collectors.base import Collector, UnitResult, WorkUnit
from tcc_scraper.config import (
    DOMINIOS_WP,
    GOOGLE_NEWS_ITEM_CAP,
    GOOGLE_NEWS_LOCALES,
    IBOV_TICKERS,
    SEARCH_QUALIFIERS,
    get_settings,
)
from tcc_scraper.db.models import Article, ArticleTicker, url_hash
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)

RSS_ENDPOINT = "https://news.google.com/rss/search"

# O pubDate do RSS so tem a data. A hora que vem junto e 00:00 do fuso do Pacifico
# (o servidor do Google fica la), convertido para UTC. Ver data_de_meia_noite_pacifica.
FUSO_PACIFICO = ZoneInfo("America/Los_Angeles")
FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")

# O Google anexa " - Veiculo" ao titulo. Isso e ruido para o FinBERT e atrapalha a
# deduplicacao por titulo, entao e removido na entrada.
_SOURCE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]+$")


@dataclass(frozen=True)
class NewsItem:
    title: str
    google_url: str
    google_id: str
    published_at: dt.datetime
    source_name: str | None
    source_domain: str | None
    # 'date' quando so a data e real (published_at e 00:00 de Sao Paulo do dia); 'datetime' senao.
    published_precision: str = "datetime"
    # 'rss' quando a hora veio do pubDate; None quando so a data e conhecida.
    published_time_source: str | None = "rss"


def data_de_meia_noite_pacifica(publicado: dt.datetime) -> dt.date | None:
    """Data do Pacifico, se o horario cair exatamente em 00:00:00 de Los Angeles.

    E o sinal de que o Google so entregou a data: a hora gravada no RSS e meia-noite
    local do servidor, nao a hora da publicacao. Devolve None para horario real.
    """
    local = publicado.astimezone(FUSO_PACIFICO)
    if local.time() == dt.time.min:
        return local.date()
    return None


def inicio_do_dia_brasil(data: dt.date) -> dt.datetime:
    """00:00 de Sao Paulo da data, em UTC (mesma convencao de cvm_ipe.artigo_de_ipe)."""
    return dt.datetime.combine(data, dt.time.min, tzinfo=FUSO_BRASIL).astimezone(dt.UTC)


def _clean_title(title: str, source_name: str | None) -> str:
    if source_name and title.endswith(f" - {source_name}"):
        return title[: -len(source_name) - 3].strip()
    return _SOURCE_SUFFIX_RE.sub("", title).strip() if " - " in title else title.strip()


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.") or None


def parse_rss(xml: str | bytes) -> list[NewsItem]:
    """Converte o XML do Google News em NewsItem.

    Tolerante por escolha: um item malformado e descartado com log em vez de
    abortar a janela inteira -- perder uma materia e melhor que perder a semana.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        root = etree.fromstring(xml, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError as exc:
        logger.warning("XML invalido do Google News: %s", exc)
        return []
    if root is None:
        return []

    items: list[NewsItem] = []
    for node in root.iter("item"):
        link = node.findtext("link")
        title = node.findtext("title")
        pub = node.findtext("pubDate")
        if not (link and title and pub):
            continue
        try:
            published = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.UTC)

        # Meia-noite do Pacifico = so a data e real: a hora gravada seria falsa.
        data_pacifica = data_de_meia_noite_pacifica(published)
        if data_pacifica is not None:
            published = inicio_do_dia_brasil(data_pacifica)
            precisao, origem_hora = "date", None
        else:
            precisao, origem_hora = "datetime", "rss"

        source_node = node.find("source")
        source_name = (source_node.text or "").strip() if source_node is not None else None
        source_url = source_node.get("url") if source_node is not None else None

        items.append(
            NewsItem(
                title=_clean_title(title, source_name),
                google_url=link.strip(),
                google_id=link.rstrip("/").split("/")[-1].split("?")[0],
                published_at=published,
                source_name=source_name or None,
                source_domain=_domain_of(source_url),
                published_precision=precisao,
                published_time_source=origem_hora,
            )
        )
    return items


def build_query(ticker: str, company: str, start: dt.date, end: dt.date) -> str:
    """Monta a busca de um ticker numa janela.

    O nome da empresa carrega quase toda a cobertura; o ticker sozinho traz pouca
    materia. Ambos entram em OR para nao perder textos que so citam o codigo.

    Quando o nome e palavra comum em portugues (Vale, Localiza, Motiva...), exige-se
    contexto setorial. Medido no piloto: ("Vale" OR VALE3) devolveu 100 itens -- o
    teto do Google News -- com panoramas de mercado e falsos positivos do verbo
    "valer"; saturar significa que noticia real tambem foi cortada. Com o contexto,
    48 itens e nenhum panorama.
    """
    periodo = f"after:{start:%Y-%m-%d} before:{end:%Y-%m-%d}"
    qualificador = SEARCH_QUALIFIERS.get(ticker)
    if qualificador:
        # O ticker entra junto do contexto: uma materia que diz "VALE3" e relevante
        # mesmo sem citar "mineracao".
        return f'"{company}" ({qualificador} OR {ticker}) {periodo}'
    return f'("{company}" OR {ticker}) {periodo}'


def iter_weeks(start: dt.date, end: dt.date) -> Iterator[tuple[dt.date, dt.date]]:
    """Divide o periodo em janelas semanais.

    Semanal e uma exigencia da fonte, nao preferencia: o RSS devolve no maximo 100
    itens por consulta e uma janela mensal satura esse teto (medido: 100 no mes,
    ~52 na semana). Janelas maiores perderiam noticias silenciosamente.
    """
    cursor = start
    step = dt.timedelta(days=7)
    while cursor < end:
        yield cursor, min(cursor + step, end)
        cursor += step


class GoogleNewsCollector(Collector):
    """Coleta noticias por (ticker x semana x idioma)."""

    name = "google_news"

    def __init__(
        self,
        tickers: dict[str, str] | None = None,
        desde: dt.date | None = None,
        ate: dt.date | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if (desde is None) != (ate is None):
            raise ValueError("desde e ate devem ser informados juntos")
        self.tickers = tickers if tickers is not None else IBOV_TICKERS
        # desde/ate: recorte manual (collect-news --desde/--ate). Sem eles, a coleta
        # cobre a janela do dataset ate fim_coleta.
        self.desde = desde
        self.ate = ate
        self.default_delay = self.settings.throttle_google_news
        self.throttle.configure("news.google.com", self.settings.throttle_google_news)

    def work_units(self) -> Iterable[WorkUnit]:
        settings = get_settings()
        recorte = self.desde is not None and self.ate is not None
        inicio = self.desde if recorte else settings.data_start
        fim = self.ate if recorte else settings.fim_coleta
        for ticker, company in self.tickers.items():
            for start, end in iter_weeks(inicio, fim):
                for locale in GOOGLE_NEWS_LOCALES:
                    # Com recorte, a chave ganha o fim da janela: nao colide com a
                    # serie historica de 3 partes.
                    if recorte:
                        chave = f"{ticker}|{start:%Y-%m-%d}|{end:%Y-%m-%d}|{locale['lang']}"
                    else:
                        chave = f"{ticker}|{start:%Y-%m-%d}|{locale['lang']}"
                    yield WorkUnit(
                        key=chave,
                        payload={
                            "ticker": ticker,
                            "company": company,
                            "start": start,
                            "end": end,
                            "locale": locale,
                        },
                    )

    def _fetch_window(
        self, ticker: str, company: str, start: dt.date, end: dt.date, locale: dict[str, str]
    ) -> list[NewsItem]:
        params = {
            "q": build_query(ticker, company, start, end),
            "hl": locale["hl"],
            "gl": locale["gl"],
            "ceid": locale["ceid"],
        }
        url = f"{RSS_ENDPOINT}?{urllib.parse.urlencode(params)}"
        return parse_rss(self.fetch(url).content)

    def process(self, unit: WorkUnit) -> UnitResult:
        ticker = unit.payload["ticker"]
        company = unit.payload["company"]
        start: dt.date = unit.payload["start"]
        end: dt.date = unit.payload["end"]
        locale = unit.payload["locale"]

        items = self._fetch_window(ticker, company, start, end, locale)
        hit_cap = len(items) >= GOOGLE_NEWS_ITEM_CAP

        # Saturou o teto: subdivide em dias para nao perder materia. Sem isto, um
        # ticker em semana de noticia forte perderia tudo acima do centesimo item.
        if hit_cap:
            logger.info("%s %s: teto atingido, subdividindo em dias", ticker, start)
            daily: dict[str, NewsItem] = {}
            for offset in range((end - start).days):
                day = start + dt.timedelta(days=offset)
                for item in self._fetch_window(
                    ticker, company, day, day + dt.timedelta(days=1), locale
                ):
                    daily[item.google_id] = item
            items = list(daily.values())

        if self.settings.wp_enabled:
            # InfoMoney, Money Times e Exame vem pela API do WordPress (collectors/wordpress.py).
            # O hit_cap acima ja foi calculado sobre a resposta crua.
            items = [i for i in items if i.source_domain not in DOMINIOS_WP]

        self._persist(items, ticker, locale["lang"])
        return UnitResult(items_found=len(items), hit_cap=hit_cap)

    def _persist(self, items: list[NewsItem], ticker: str, lang: str) -> None:
        """Grava artigos e o vinculo com o ticker, em tres idas ao banco.

        A versao anterior fazia ate tres `execute` POR ARTIGO, usando `RETURNING`
        para obter o id recem-inserido. Dois problemas: numa carga de ~150 mil
        artigos isso vira ~450 mil idas ao banco, e `RETURNING` nao existe no MySQL.

        Aqui: (1) grava os artigos em lote, (2) le os ids de volta pelos url_hash,
        (3) grava os vinculos em lote. Tres queries por janela, independente do
        numero de materias, e nenhuma sintaxe especifica de banco.
        """
        if not items:
            return
        now = dt.datetime.now(dt.UTC)

        # A mesma materia pode vir repetida na resposta; o dict resolve antes de
        # chegar ao banco (o lote nao pode trazer a mesma chave duas vezes).
        por_hash = {url_hash(item.google_url): item for item in items}

        artigos = [
            {
                "url_hash": h,
                "url": item.google_url,
                "google_id": item.google_id,
                "title": item.title,
                "source_name": item.source_name,
                "source_domain": item.source_domain,
                "published_at": item.published_at,
                "published_precision": item.published_precision,
                "published_time_source": item.published_time_source,
                "lang": lang,
                "collected_at": now,
            }
            for h, item in por_hash.items()
        ]

        with session_scope() as session:
            # Artigo ja conhecido nao e reescrito: a camada raw e append-only, e o
            # primeiro registro preserva quando a materia foi vista pela primeira vez.
            bulk_upsert(
                session,
                Article.__table__,
                artigos,
                index_elements=["url_hash"],
                update_columns=None,
            )
            session.flush()

            ids = session.execute(
                select(Article.id).where(Article.url_hash.in_(list(por_hash)))
            ).scalars().all()

            bulk_upsert(
                session,
                ArticleTicker.__table__,
                [{"article_id": aid, "ticker": ticker} for aid in ids],
                index_elements=["article_id", "ticker"],
                update_columns=None,
            )
