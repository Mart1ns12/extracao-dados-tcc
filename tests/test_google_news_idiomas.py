"""GOOGLE_NEWS_IDIOMAS: quais locales do Google News a coleta usa, e validacao do valor.

Sem rede e sem banco: so a montagem das unidades de trabalho.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from tcc_scraper.collectors import google_news as google_news_mod
from tcc_scraper.collectors.google_news import GoogleNewsCollector
from tcc_scraper.config import Settings, parse_idiomas

INICIO = dt.date(2026, 7, 1)
FIM = dt.date(2026, 7, 4)  # uma unica janela semanal


def _unidades_com_idiomas(monkeypatch, idiomas: str) -> list:
    """Unidades de um ticker numa janela, com GOOGLE_NEWS_IDIOMAS fixado (sem ler o .env)."""
    monkeypatch.setattr(
        google_news_mod, "get_settings", lambda: Settings(google_news_idiomas=idiomas)
    )
    with GoogleNewsCollector(tickers={"PETR4": "Petrobras"}, desde=INICIO, ate=FIM) as coletor:
        return list(coletor.work_units())


def test_pt_gera_so_unidades_pt(monkeypatch) -> None:
    unidades = _unidades_com_idiomas(monkeypatch, "pt")

    assert len(unidades) == 1
    assert unidades[0].key.endswith("|pt")
    assert unidades[0].payload["locale"]["lang"] == "pt"


def test_pt_e_en_geram_as_duas_unidades(monkeypatch) -> None:
    unidades = _unidades_com_idiomas(monkeypatch, "pt,en")

    assert sorted(u.key.rsplit("|", 1)[1] for u in unidades) == ["en", "pt"]


def test_ordem_e_caixa_nao_mudam_o_resultado() -> None:
    assert parse_idiomas("en,pt") == ("pt", "en")  # sempre na ordem do catalogo
    assert parse_idiomas(" PT , en ") == ("pt", "en")
    assert parse_idiomas("pt,pt") == ("pt",)


def test_locales_ativos_seguem_o_valor_configurado() -> None:
    ativos_pt = Settings(google_news_idiomas="pt").google_news_locales_ativos
    ativos_en = Settings(google_news_idiomas="en").google_news_locales_ativos
    assert [loc["lang"] for loc in ativos_pt] == ["pt"]
    assert [loc["lang"] for loc in ativos_en] == ["en"]


@pytest.mark.parametrize("valor", ["es", "pt,fr", "ptbr"])
def test_idioma_fora_do_catalogo_e_erro_claro(valor: str) -> None:
    with pytest.raises(ValueError, match="GOOGLE_NEWS_IDIOMAS invalido"):
        parse_idiomas(valor)


@pytest.mark.parametrize("valor", ["", " , ", ","])
def test_lista_vazia_e_erro_claro(valor: str) -> None:
    with pytest.raises(ValueError, match="GOOGLE_NEWS_IDIOMAS vazio"):
        parse_idiomas(valor)


def test_settings_recusa_valor_invalido_na_validacao() -> None:
    with pytest.raises(ValidationError, match="GOOGLE_NEWS_IDIOMAS invalido"):
        Settings(google_news_idiomas="es")
