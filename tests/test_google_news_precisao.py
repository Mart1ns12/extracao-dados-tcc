"""Precisao da data de publicacao no parse do RSS do Google News.

O pubDate traz so a data, e a hora que vem junto e 00:00 do fuso de Los Angeles. Quando
isso acontece, a materia vira published_precision='date' e a hora falsa nao e gravada.
"""

from __future__ import annotations

import datetime as dt

from tcc_scraper.collectors.google_news import parse_rss


def _rss(pub_date: str) -> str:
    return (
        "<rss><channel><item>"
        "<title>Vale anuncia dividendos - Veja</title>"
        "<link>https://news.google.com/rss/articles/ABC123</link>"
        f"<pubDate>{pub_date}</pubDate>"
        '<source url="https://veja.abril.com.br">Veja</source>'
        "</item></channel></rss>"
    )


def test_meia_noite_pacifica_em_horario_de_verao_vira_date() -> None:
    """PDT (UTC-7): 00:00 de Los Angeles e 07:00 UTC."""
    item = parse_rss(_rss("Wed, 15 Jul 2026 07:00:00 GMT"))[0]

    assert item.published_precision == "date"
    assert item.published_time_source is None
    # 00:00 de Sao Paulo do dia 15/07, em UTC.
    assert item.published_at == dt.datetime(2026, 7, 15, 3, 0, tzinfo=dt.UTC)


def test_meia_noite_pacifica_em_horario_padrao_vira_date() -> None:
    """PST (UTC-8): 00:00 de Los Angeles e 08:00 UTC."""
    item = parse_rss(_rss("Thu, 15 Jan 2026 08:00:00 GMT"))[0]

    assert item.published_precision == "date"
    assert item.published_time_source is None
    assert item.published_at == dt.datetime(2026, 1, 15, 3, 0, tzinfo=dt.UTC)


def test_hora_normal_continua_datetime_vinda_do_rss() -> None:
    item = parse_rss(_rss("Wed, 15 Jul 2026 15:30:00 GMT"))[0]

    assert item.published_precision == "datetime"
    assert item.published_time_source == "rss"
    assert item.published_at == dt.datetime(2026, 7, 15, 15, 30, tzinfo=dt.UTC)


def test_meia_noite_utc_nao_e_meia_noite_pacifica() -> None:
    """00:00 UTC cai as 21:00 do dia anterior em Los Angeles: nao e sinal de so-data."""
    item = parse_rss(_rss("Wed, 15 Jul 2026 00:00:00 GMT"))[0]

    assert item.published_precision == "datetime"
    assert item.published_time_source == "rss"
    assert item.published_at == dt.datetime(2026, 7, 15, 0, 0, tzinfo=dt.UTC)
