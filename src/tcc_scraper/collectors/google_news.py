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
import time
import urllib.parse
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from lxml import etree
from sqlalchemy import select

from tcc_scraper.collectors.base import (
    AbortarColeta,
    Collector,
    FalhaBusca,
    ParadaSolicitada,
    UnitResult,
    WorkUnit,
)
from tcc_scraper.config import (
    DOMINIOS_WP,
    GOOGLE_NEWS_ITEM_CAP,
    IBOV_TICKERS,
    SEARCH_QUALIFIERS,
    get_settings,
)
from tcc_scraper.db.models import Article, ArticleTicker, url_hash
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)

RSS_ENDPOINT = "https://news.google.com/rss/search"

# Passo do sono na pausa: a parada pedida pelo painel e atendida em ate este intervalo.
PASSO_PAUSA_S = 5.0

# O pubDate do RSS so tem a data. A hora que vem junto e 00:00 do fuso do Pacifico
# (o servidor do Google fica la), convertido para UTC. Ver data_de_meia_noite_pacifica.
FUSO_PACIFICO = ZoneInfo("America/Los_Angeles")
FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")

# O Google anexa " - Veiculo" ao titulo. Isso e ruido para o FinBERT e atrapalha a
# deduplicacao por titulo, entao e removido na entrada.
_SOURCE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]+$")


class RespostaNaoRss(RuntimeError):
    """Resposta 200 que nao e RSS: pagina de captcha, de consentimento ou corpo vazio.

    Antes virava lista vazia e a unidade saia `ok` com 0 itens -- perda silenciosa, nunca
    refeita. Agora a unidade sai `error` e volta na proxima execucao (pendentes refaz erros).
    """


def _inicio(xml: bytes) -> str:
    return " ".join(xml[:120].decode("utf-8", errors="replace").split())


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


def _motivo(exc: BaseException) -> str:
    """Causa curta para log: 'HTTP 503' ou 'ReadTimeout', seguindo a cadeia de causas."""
    atual: BaseException | None = exc
    while atual is not None:
        if isinstance(atual, FalhaBusca):
            return atual.motivo
        atual = atual.__cause__
    return type(exc).__name__


def _clean_title(title: str, source_name: str | None) -> str:
    if source_name and title.endswith(f" - {source_name}"):
        return title[: -len(source_name) - 3].strip()
    return _SOURCE_SUFFIX_RE.sub("", title).strip() if " - " in title else title.strip()


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.") or None


def parse_rss(xml: str | bytes, *, estrito: bool = False) -> list[NewsItem]:
    """Converte o XML do Google News em NewsItem.

    Tolerante por escolha: um item malformado e descartado com log em vez de
    abortar a janela inteira -- perder uma materia e melhor que perder a semana.

    estrito=True (usado pela coleta): o documento precisa ser um RSS. Pagina de captcha,
    consentimento ou corpo vazio levantam RespostaNaoRss em vez de virar lista vazia.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        root = etree.fromstring(xml, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError as exc:
        if estrito:
            raise RespostaNaoRss(
                f"XML invalido do Google News: {exc}; inicio: {_inicio(xml)!r}"
            ) from exc
        logger.warning("XML invalido do Google News: %s", exc)
        return []
    if root is None:
        if estrito:
            raise RespostaNaoRss(f"resposta vazia ou ilegivel; inicio: {_inicio(xml)!r}")
        return []
    if estrito and (str(root.tag) != "rss" or root.find("channel") is None):
        raise RespostaNaoRss(
            f"resposta sem <rss>/<channel> (raiz <{root.tag}>); inicio: {_inicio(xml)!r}"
        )

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
        titulo = _clean_title(title, source_name)
        if not titulo:
            # So o sufixo da fonte (ex.: " - Pagina 3"): sem manchete, nao ha o que gravar.
            continue

        items.append(
            NewsItem(
                title=titulo,
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
        self.throttle.configure(
            "news.google.com",
            self.settings.throttle_google_news,
            jitter=self.settings.throttle_google_news_jitter_s,
        )
        # Disjuntor: unidades seguidas com falha (ver process). Zera a cada sucesso.
        self._falhas_seguidas = 0
        # Pausas usadas sem nenhum sucesso entre elas (a proxima e a de indice igual a este).
        self._pausas_seguidas = 0
        # Observador da execucao corrente (ver run): a pausa consulta deve_parar por ele.
        self._observador = None
        # Injetaveis: os testes trocam relogio e sono por fakes, sem esperar 30 min reais.
        self._relogio = time.monotonic
        self._dormir = time.sleep
        self._agora = lambda: dt.datetime.now(dt.UTC)

    def work_units(self) -> Iterable[WorkUnit]:
        settings = get_settings()
        recorte = self.desde is not None and self.ate is not None
        inicio = self.desde if recorte else settings.data_start
        fim = self.ate if recorte else settings.fim_coleta
        locales = settings.google_news_locales_ativos
        for ticker, company in self.tickers.items():
            for start, end in iter_weeks(inicio, fim):
                for locale in locales:
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
        return parse_rss(self.fetch(url).content, estrito=True)

    def run(self, *, observador=None, **kwargs) -> dict[str, int]:
        """Guarda o observador da execucao: a pausa do disjuntor consulta `deve_parar` por ele."""
        self._observador = observador
        try:
            return super().run(observador=observador, **kwargs)
        finally:
            self._observador = None

    def process(self, unit: WorkUnit) -> UnitResult:
        """Executa a janela e conta falhas seguidas (ver google_news_max_falhas_seguidas).

        Ao atingir o limite, pausa (google_news_pausas_min) em vez de abortar. Aborta so quando
        as pausas acabam sem nenhum sucesso entre elas.
        """
        try:
            resultado = self._processar(unit)
        except Exception as exc:
            self._falhas_seguidas += 1
            limite = self.settings.google_news_max_falhas_seguidas
            if limite and self._falhas_seguidas >= limite:
                self._pausar_por_bloqueio(exc)
            # A unidade que falhou segue como erro no checkpoint; a pausa vale para as proximas.
            raise
        self._falhas_seguidas = 0
        self._pausas_seguidas = 0
        return resultado

    def _pausar_por_bloqueio(self, exc: Exception) -> None:
        """Pausa a k-esima vez (k = pausas seguidas sem sucesso + 1); volta ao fim da pausa.

        Levanta AbortarColeta quando as pausas acabam, e ParadaSolicitada se o observador
        pedir parada durante a pausa.
        """
        pausas = self.settings.google_news_pausas
        if self._pausas_seguidas >= len(pausas):
            termo = "pausa" if len(pausas) == 1 else "pausas"
            raise AbortarColeta(
                f"google_news: {self._falhas_seguidas} unidades seguidas falharam "
                f"(provavel bloqueio); ultima: {exc}; apos {len(pausas)} {termo}"
            ) from exc
        self._pausas_seguidas += 1
        minutos = pausas[self._pausas_seguidas - 1]
        ate = self._agora() + dt.timedelta(minutes=minutos)
        mensagem = (
            f"google_news: provavel bloqueio ({self._falhas_seguidas} unidades seguidas "
            f"falharam; ultima causa: {_motivo(exc)}); pausa de {minutos:g} min ate "
            f"{ate:%H:%M} UTC (pausa {self._pausas_seguidas}/{len(pausas)})"
        )
        logger.warning(mensagem)
        # O painel so ve o que passa pelo observador: o log da pausa e a "unidade atual" (sem
        # isso a coleta parece travada por ate 2 h). Os dois sao opcionais no Protocol.
        if (registrar := getattr(self._observador, "log", None)) is not None:
            registrar("aviso", mensagem)
        if (inicio := getattr(self._observador, "inicio_unidade", None)) is not None:
            inicio(f"pausa por bloqueio ate {ate:%H:%M} UTC "
                   f"({self._pausas_seguidas}/{len(pausas)})")
        interrompida = self._dormir_pausa(minutos * 60)
        self._falhas_seguidas = 0
        if interrompida:
            raise ParadaSolicitada(f"{self.name}: parada solicitada durante a pausa")

    def _dormir_pausa(self, segundos: float) -> bool:
        """Dorme a pausa em passos de PASSO_PAUSA_S. True se o observador pediu parada.

        A condicao e a mesma do laco de `Collector.run` (`observador.deve_parar()`, em
        collectors/base.py). Sem ela, uma pausa de 2 h tornaria o cancelamento do painel inoperante.
        """
        fim = self._relogio() + segundos
        while True:
            if self._observador is not None and self._observador.deve_parar():
                return True
            restante = fim - self._relogio()
            if restante <= 0:
                return False
            self._dormir(min(PASSO_PAUSA_S, restante))

    def _processar(self, unit: WorkUnit) -> UnitResult:
        ticker = unit.payload["ticker"]
        company = unit.payload["company"]
        start: dt.date = unit.payload["start"]
        end: dt.date = unit.payload["end"]
        locale = unit.payload["locale"]

        items = self._fetch_window(ticker, company, start, end, locale)
        hit_cap = len(items) >= GOOGLE_NEWS_ITEM_CAP

        falha: Exception | None = None
        dia_falho: dt.date | None = None
        # Saturou o teto: subdivide em dias para nao perder materia. Sem isto, um
        # ticker em semana de noticia forte perderia tudo acima do centesimo item.
        if hit_cap:
            logger.info("%s %s: teto atingido, subdividindo em dias", ticker, start)
            # A resposta da semana entra junto: e materia real, e o dict deduplica pelo id.
            por_id: dict[str, NewsItem] = {i.google_id: i for i in items}
            for offset in range((end - start).days):
                day = start + dt.timedelta(days=offset)
                try:
                    diarios = self._fetch_window(
                        ticker, company, day, day + dt.timedelta(days=1), locale
                    )
                except Exception as exc:
                    # Para no 1o dia que falha: se a fonte esta bloqueando, insistir nos outros
                    # dias so gastaria tentativas. O que ja veio e gravado abaixo e a unidade
                    # sai com erro para ser refeita (o persist e idempotente).
                    falha, dia_falho = exc, day
                    break
                for item in diarios:
                    por_id[item.google_id] = item
            items = list(por_id.values())

        if self.settings.wp_enabled:
            # InfoMoney, Money Times e Exame vem pela API do WordPress (collectors/wordpress.py).
            # O hit_cap acima ja foi calculado sobre a resposta crua.
            items = [i for i in items if i.source_domain not in DOMINIOS_WP]

        self._persist(items, ticker, locale["lang"])
        if falha is not None:
            raise RuntimeError(
                f"subdivisao incompleta: o dia {dia_falho:%Y-%m-%d} falhou ({falha})"
            ) from falha
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
