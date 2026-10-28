"""Google News robusto (T2): resposta que nao e RSS, subdivisao parcial e disjuntor.

Sem rede e sem banco: fetch, _fetch_window e _persist sao trocados por listas em memoria.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from tcc_scraper.collectors.base import AbortarColeta, WorkUnit
from tcc_scraper.collectors.google_news import (
    GoogleNewsCollector,
    NewsItem,
    RespostaNaoRss,
    parse_rss,
)
from tcc_scraper.config import GOOGLE_NEWS_ITEM_CAP, GOOGLE_NEWS_LOCALES, Settings
from tests.relogio_falso import RelogioFalso

FIXTURES = Path(__file__).parent / "fixtures"
INICIO = dt.date(2026, 7, 1)
FIM = dt.date(2026, 7, 4)  # 3 dias


def _item(marca: str, dominio: str = "valor.globo.com") -> NewsItem:
    return NewsItem(
        title=f"Petrobras {marca}",
        google_url=f"https://news.google.com/rss/articles/{marca}",
        google_id=marca,
        published_at=dt.datetime(2026, 7, 2, 15, 0, tzinfo=dt.UTC),
        source_name=None,
        source_domain=dominio,
    )


def _unidade(start: dt.date = INICIO, end: dt.date = FIM) -> WorkUnit:
    return WorkUnit(
        key=f"PETR4|{start:%Y-%m-%d}",
        payload={
            "ticker": "PETR4",
            "company": "Petrobras",
            "start": start,
            "end": end,
            "locale": GOOGLE_NEWS_LOCALES[0],
        },
    )


def _sequencia(respostas: list):
    """Falso _fetch_window: cada chamada devolve o proximo item (ou levanta, se for excecao)."""
    fila = list(respostas)
    chamadas: list[tuple] = []

    def falso(*args):
        chamadas.append(args)
        valor = fila.pop(0)
        if isinstance(valor, Exception):
            raise valor
        return list(valor)

    return falso, chamadas


def _coletor(monkeypatch, *, relogio: RelogioFalso | None = None, **config) -> GoogleNewsCollector:
    """Coletor com config injetada. Relogio e sono sao falsos: a pausa do disjuntor nao espera."""
    coletor = GoogleNewsCollector(tickers={"PETR4": "Petrobras"}, desde=INICIO, ate=FIM)
    monkeypatch.setattr(coletor, "settings", Settings(gemini_api_key="", **config))
    if relogio is None:
        relogio = RelogioFalso()
    monkeypatch.setattr(coletor, "_relogio", relogio)
    monkeypatch.setattr(coletor, "_dormir", relogio.dormir)
    return coletor


# -- resposta que nao e RSS ------------------------------------------------------------


def test_html_de_captcha_levanta_resposta_nao_rss_no_modo_estrito() -> None:
    with pytest.raises(RespostaNaoRss):
        parse_rss(b"<html><body>captcha</body></html>", estrito=True)


def test_corpo_vazio_levanta_resposta_nao_rss_no_modo_estrito() -> None:
    with pytest.raises(RespostaNaoRss):
        parse_rss(b"", estrito=True)


def test_rss_sem_channel_levanta_resposta_nao_rss_no_modo_estrito() -> None:
    with pytest.raises(RespostaNaoRss):
        parse_rss(b"<rss></rss>", estrito=True)


def test_rss_valido_sem_itens_nao_e_erro_no_modo_estrito() -> None:
    assert parse_rss(b"<rss><channel></channel></rss>", estrito=True) == []


def test_modo_tolerante_mantem_html_como_lista_vazia() -> None:
    assert parse_rss(b"<html><body>captcha</body></html>") == []


def test_fixture_real_do_google_news_passa_no_modo_estrito() -> None:
    xml = (FIXTURES / "google_news_petr4_2024w23.xml").read_bytes()

    assert len(parse_rss(xml, estrito=True)) == len(parse_rss(xml))
    assert parse_rss(xml, estrito=True)


def test_fetch_window_com_html_levanta_resposta_nao_rss(monkeypatch) -> None:
    coletor = _coletor(monkeypatch)
    monkeypatch.setattr(
        coletor, "fetch", lambda url: SimpleNamespace(content=b"<html>x</html>")
    )

    with pytest.raises(RespostaNaoRss):
        coletor._fetch_window("PETR4", "Petrobras", INICIO, FIM, GOOGLE_NEWS_LOCALES[0])


# -- subdivisao parcial ----------------------------------------------------------------


def test_subdivisao_que_falha_no_meio_grava_o_parcial_e_levanta(monkeypatch) -> None:
    semana = [_item(f"w{n}") for n in range(GOOGLE_NEWS_ITEM_CAP)]
    falso, chamadas = _sequencia(
        [semana, [_item("d0")], RuntimeError("503")],
    )
    coletor = _coletor(monkeypatch, wp_enabled=False)
    monkeypatch.setattr(coletor, "_fetch_window", falso)
    gravadas: list[NewsItem] = []
    monkeypatch.setattr(
        coletor, "_persist", lambda items, ticker, lang: gravadas.extend(items)
    )

    with pytest.raises(RuntimeError, match="subdivisao incompleta") as info:
        coletor.process(_unidade())

    assert len(chamadas) == 3  # semana + dia 1 + dia 2 (que falhou); o dia 3 nao e tentado
    assert len(gravadas) == GOOGLE_NEWS_ITEM_CAP + 1
    assert isinstance(info.value.__cause__, RuntimeError)


# -- disjuntor -------------------------------------------------------------------------


def test_disjuntor_pausa_no_limite_e_aborta_quando_as_pausas_acabam(monkeypatch) -> None:
    # Limite 3, pausas de 1 e 2 min: a 3a falha pausa (1 min), a 6a pausa (2 min) e a 9a aborta.
    falso, chamadas = _sequencia([RespostaNaoRss("captcha")] * 9)
    relogio = RelogioFalso()
    coletor = _coletor(
        monkeypatch, relogio=relogio,
        google_news_max_falhas_seguidas=3, google_news_pausas_min="1,2",
    )
    monkeypatch.setattr(coletor, "_fetch_window", falso)

    for _ in range(8):
        with pytest.raises(RespostaNaoRss):
            coletor.process(_unidade())
    with pytest.raises(AbortarColeta, match="unidades seguidas falharam.*apos 2 pausas"):
        coletor.process(_unidade())

    assert len(chamadas) == 9
    assert sum(relogio.dormidas) == 180  # 60 s + 120 s


def test_sucesso_zera_o_contador_do_disjuntor(monkeypatch) -> None:
    falha = RespostaNaoRss("captcha")
    falso, _ = _sequencia([falha, falha, [], falha, falha])
    coletor = _coletor(monkeypatch, google_news_max_falhas_seguidas=3)
    monkeypatch.setattr(coletor, "_fetch_window", falso)
    monkeypatch.setattr(coletor, "_persist", lambda items, ticker, lang: None)

    for _ in range(2):
        with pytest.raises(RespostaNaoRss):
            coletor.process(_unidade())
    assert coletor.process(_unidade()).items_found == 0
    for _ in range(2):
        with pytest.raises(RespostaNaoRss):
            coletor.process(_unidade())


def test_limite_zero_desliga_o_disjuntor(monkeypatch) -> None:
    falso, chamadas = _sequencia([RespostaNaoRss("captcha")] * 5)
    coletor = _coletor(monkeypatch, google_news_max_falhas_seguidas=0)
    monkeypatch.setattr(coletor, "_fetch_window", falso)

    for _ in range(5):
        with pytest.raises(RespostaNaoRss):
            coletor.process(_unidade())

    assert len(chamadas) == 5
