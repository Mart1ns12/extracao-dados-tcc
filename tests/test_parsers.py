"""Testes dos parsers contra respostas reais capturadas em tests/fixtures/.

Sem rede: as fixtures sao respostas verdadeiras salvas em 2026-09-07. Ao mudar um
parser, recapture a fixture de uma resposta real -- nunca edite o XML/JSON a mao
para o teste passar, senao o teste deixa de descrever o mundo.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tcc_scraper.collectors.google_news import parse_rss
from tcc_scraper.collectors.macro_bcb import parse_ptax, parse_sgs
from tcc_scraper.collectors.prices_yahoo import parse_chart

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------------------
# Google News RSS
# --------------------------------------------------------------------------------------
@pytest.fixture
def gnews_xml() -> str:
    return (FIXTURES / "google_news_petr4_2024w23.xml").read_text(encoding="utf-8")


def test_parse_rss_extrai_todos_os_itens(gnews_xml: str) -> None:
    items = parse_rss(gnews_xml)
    assert len(items) == 54


def test_parse_rss_preenche_campos_essenciais(gnews_xml: str) -> None:
    item = parse_rss(gnews_xml)[0]
    assert item.title
    assert item.google_url.startswith("https://news.google.com/rss/articles/")
    assert item.google_id
    assert isinstance(item.published_at, dt.datetime)
    assert item.published_at.tzinfo is not None, "published_at deve ser timezone-aware"


def test_parse_rss_extrai_veiculo_da_tag_source(gnews_xml: str) -> None:
    """A tag <source> da o veiculo sem precisar resolver o link opaco."""
    items = parse_rss(gnews_xml)
    com_fonte = [i for i in items if i.source_domain]
    assert len(com_fonte) > len(items) * 0.9
    dominios = {i.source_domain for i in com_fonte}
    assert any("infomoney" in d for d in dominios)


def test_parse_rss_remove_sufixo_do_veiculo_no_titulo(gnews_xml: str) -> None:
    """O Google anexa ' - Veiculo' ao titulo; isso e ruido para o FinBERT."""
    for item in parse_rss(gnews_xml):
        if item.source_name:
            assert not item.title.endswith(f" - {item.source_name}")


def test_parse_rss_datas_dentro_da_janela_consultada(gnews_xml: str) -> None:
    """A fixture cobre after:2024-06-03 before:2024-06-10."""
    datas = [i.published_at.date() for i in parse_rss(gnews_xml)]
    assert min(datas) >= dt.date(2024, 6, 1)
    assert max(datas) <= dt.date(2024, 6, 12)


def test_parse_rss_vazio_nao_quebra() -> None:
    assert parse_rss("<rss><channel></channel></rss>") == []


# --------------------------------------------------------------------------------------
# Yahoo chart
# --------------------------------------------------------------------------------------
@pytest.fixture
def yahoo_json() -> dict:
    return json.loads((FIXTURES / "yahoo_chart_petr4.json").read_text(encoding="utf-8"))


def test_parse_chart_retorna_ohlcv_com_adj_close(yahoo_json: dict) -> None:
    rows = parse_chart(yahoo_json, ticker="PETR4")
    assert rows
    row = rows[0]
    assert row.ticker == "PETR4"
    assert isinstance(row.date, dt.date)
    assert row.adj_close is not None
    assert row.close is not None and row.close > 0


def test_parse_chart_descarta_pregoes_sem_fechamento(yahoo_json: dict) -> None:
    """O Yahoo devolve null em dias sem negocio; uma linha sem close nao e pregao."""
    assert all(r.close is not None for r in parse_chart(yahoo_json, ticker="PETR4"))


def test_parse_chart_datas_ordenadas_e_unicas(yahoo_json: dict) -> None:
    datas = [r.date for r in parse_chart(yahoo_json, ticker="PETR4")]
    assert datas == sorted(datas)
    assert len(datas) == len(set(datas))


def test_parse_chart_indice_sem_adjclose_usa_close() -> None:
    """Indice (IDIV) pode vir sem adjclose: a linha cai no close em vez de sumir."""
    raw = json.loads((FIXTURES / "yahoo_chart_idiv.json").read_text(encoding="utf-8"))
    del raw["chart"]["result"][0]["indicators"]["adjclose"]
    rows = parse_chart(raw, ticker="IDIV")

    assert rows
    assert all(r.adj_close == r.close for r in rows)


# --------------------------------------------------------------------------------------
# Banco Central
# --------------------------------------------------------------------------------------
def test_parse_sgs_converte_data_br_e_valor() -> None:
    """O SGS devolve data como 'dd/mm/aaaa' e valor como string."""
    raw = json.loads((FIXTURES / "bcb_sgs_432.json").read_text(encoding="utf-8"))
    rows = parse_sgs(raw, series="selic")

    assert len(rows) == 10
    assert rows[0].series == "selic"
    assert rows[0].date == dt.date(2024, 6, 1)
    assert rows[0].value == pytest.approx(10.50)
    assert all(isinstance(r.value, float) for r in rows)


def test_parse_ptax_usa_cotacao_venda() -> None:
    """A planilha de fontes especifica cotacaoVenda, nao compra nem media."""
    raw = json.loads((FIXTURES / "bcb_ptax.json").read_text(encoding="utf-8"))
    rows = parse_ptax(raw)

    assert len(rows) == 6
    assert rows[0].series == "ptax_venda"
    assert rows[0].date == dt.date(2024, 6, 3)
    assert rows[0].value == pytest.approx(5.23730)


def test_parse_ptax_extrai_apenas_a_data_do_timestamp() -> None:
    """dataHoraCotacao traz hora do fechamento; so a data importa para o join diario."""
    raw = json.loads((FIXTURES / "bcb_ptax.json").read_text(encoding="utf-8"))
    datas = [r.date for r in parse_ptax(raw)]

    assert datas == sorted(datas)
    assert dt.date(2024, 6, 8) not in datas, "sabado nao tem PTAX"


# --------------------------------------------------------------------------------------
# Construcao da query de busca
# --------------------------------------------------------------------------------------
def test_query_simples_para_nome_distintivo() -> None:
    """Petrobras nao e palavra ambigua: nome OR ticker basta."""
    from tcc_scraper.collectors.google_news import build_query

    q = build_query("PETR4", "Petrobras", dt.date(2024, 6, 3), dt.date(2024, 6, 10))

    assert '"Petrobras"' in q
    assert "PETR4" in q
    assert "after:2024-06-03" in q
    assert "before:2024-06-10" in q


def test_query_com_qualificador_para_nome_ambiguo() -> None:
    """'Vale' e verbo em portugues: sem contexto setorial a busca satura de ruido.

    Medido no piloto: ("Vale" OR VALE3) devolveu 100 itens (teto), com panoramas de
    mercado e falsos positivos; com qualificador, 48 itens relevantes.
    """
    from tcc_scraper.collectors.google_news import build_query

    q = build_query("VALE3", "Vale", dt.date(2024, 6, 3), dt.date(2024, 6, 10))

    assert "mineracao" in q or "minerio" in q, "faltou o contexto setorial"
    assert "VALE3" in q, "o ticker deve continuar aceito como alternativa"
    assert '"Vale"' in q


def test_qualificador_nao_se_aplica_a_ticker_sem_ambiguidade() -> None:
    from tcc_scraper.collectors.google_news import build_query
    from tcc_scraper.config import SEARCH_QUALIFIERS

    q = build_query("SUZB3", "Suzano", dt.date(2024, 6, 3), dt.date(2024, 6, 10))

    assert "SUZB3" not in SEARCH_QUALIFIERS
    assert q == '("Suzano" OR SUZB3) after:2024-06-03 before:2024-06-10'
