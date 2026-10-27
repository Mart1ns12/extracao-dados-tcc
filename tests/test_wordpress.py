"""Coletor WordPress: janelas, paginacao, filtro UTC, completude e pre-filtro. Sem rede e sem banco.

A rede e substituida por um stub de `fetch` que devolve paginas da fixture real
(tests/fixtures/wp_posts_amostra.json, copiada do cache de 2026-03). A persistencia e
substituida por captura de `bulk_upsert`, para conferir so o que chega a ela.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tcc_scraper.collectors import wordpress
from tcc_scraper.collectors.base import WorkUnit
from tcc_scraper.collectors.wordpress import (
    WordPressCollector,
    meses_do_intervalo,
    texto_bruto,
)
from tcc_scraper.config import WP_SITES, get_settings
from tcc_scraper.db.models_fontes import RawWpPost

FIXTURES = Path(__file__).parent / "fixtures"
ITENS = json.loads((FIXTURES / "wp_posts_amostra.json").read_text(encoding="utf-8"))
DESDE = dt.date(2026, 3, 1)
ATE = dt.date(2026, 4, 1)


class _Resposta:
    """Imita o que `fetch` devolve: .json(), .headers e raise_for_status()."""

    def __init__(self, itens: list[dict], headers: dict[str, str], status: int = 200) -> None:
        self._itens = itens
        self.headers = headers
        self.status_code = status

    def json(self) -> list[dict]:
        return self._itens

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def _stub_fetch(
    paginas: dict[int, list[dict]],
    total: int,
    total_paginas: int,
    chamadas: list[dict],
):
    def fake(url, **kwargs):
        params = kwargs["params"]
        chamadas.append(params)
        headers = {"X-WP-Total": str(total), "X-WP-TotalPages": str(total_paginas)}
        return _Resposta(paginas[params["page"]], headers)

    return fake


def _unidade(ini: dt.date = DESDE, fim: dt.date = ATE) -> WorkUnit:
    site = WP_SITES[0]
    return WorkUnit(
        key=f"{site.nome}|{ini:%Y-%m-%d}|{fim:%Y-%m-%d}",
        payload={"site": site, "ini": ini, "fim": fim},
    )


def _post(id_: int, data: str, titulo: str, conteudo: str) -> dict:
    return {
        "id": id_,
        "date_gmt": data,
        "link": f"https://www.moneytimes.com.br/teste-{id_}/",
        "type": "post",
        "title": {"rendered": titulo},
        "content": {"rendered": conteudo},
    }


def _captura_persistencia(monkeypatch) -> list[dict]:
    """Troca session_scope e bulk_upsert do modulo; devolve a lista onde as chamadas caem."""
    gravacoes: list[dict] = []

    @contextmanager
    def sessao_falsa():
        yield object()

    def upsert_falso(session, table, rows, *, index_elements, update_columns=None):
        gravacoes.append(
            {
                "table": table,
                "rows": list(rows),
                "index_elements": list(index_elements),
                "update_columns": update_columns,
            }
        )
        return len(rows)

    monkeypatch.setattr(wordpress, "session_scope", sessao_falsa)
    monkeypatch.setattr(wordpress, "bulk_upsert", upsert_falso)
    return gravacoes


# --- janelas -----------------------------------------------------------------------


def test_meses_do_intervalo_corta_na_virada_do_ano() -> None:
    assert list(meses_do_intervalo(dt.date(2025, 12, 31), dt.date(2026, 2, 1))) == [
        (dt.date(2025, 12, 31), dt.date(2026, 1, 1)),
        (dt.date(2026, 1, 1), dt.date(2026, 2, 1)),
    ]


def test_work_units_corta_nas_fronteiras_de_mes_por_site() -> None:
    with WordPressCollector(desde=dt.date(2026, 1, 15), ate=dt.date(2026, 3, 2)) as coletor:
        unidades = list(coletor.work_units())

    assert len(unidades) == 3 * len(WP_SITES)
    infomoney = [u.key for u in unidades if u.payload["site"].nome == "infomoney"]
    assert infomoney == [
        "infomoney|2026-01-15|2026-02-01",
        "infomoney|2026-02-01|2026-03-01",
        "infomoney|2026-03-01|2026-03-02",
    ]


def test_janela_padrao_usa_inicio_noticias_e_fim_coleta_mais_um_dia() -> None:
    settings = get_settings()
    with WordPressCollector() as coletor:
        assert coletor.desde == settings.inicio_noticias
        assert coletor.ate == settings.fim_coleta + dt.timedelta(days=1)


def test_desde_sem_ate_levanta_erro() -> None:
    with pytest.raises(ValueError):
        WordPressCollector(desde=DESDE)


def test_throttle_configurado_por_host_de_cada_veiculo() -> None:
    with WordPressCollector(desde=DESDE, ate=ATE) as coletor:
        atrasos = coletor.throttle._delays
    assert atrasos["www.infomoney.com.br"] == get_settings().throttle_wordpress
    assert atrasos["www.moneytimes.com.br"] == get_settings().throttle_wordpress
    assert atrasos["exame.com"] == get_settings().throttle_wordpress


# --- paginacao e parametros da consulta ----------------------------------------------


def test_pagina_ate_total_paginas_e_monta_a_consulta_com_margem(monkeypatch) -> None:
    _captura_persistencia(monkeypatch)
    chamadas: list[dict] = []
    with WordPressCollector(desde=DESDE, ate=ATE) as coletor:
        monkeypatch.setattr(
            coletor,
            "fetch",
            _stub_fetch({1: ITENS[:2], 2: ITENS[2:]}, total=4, total_paginas=2, chamadas=chamadas),
        )
        coletor.process(_unidade())

    assert [c["page"] for c in chamadas] == [1, 2]
    primeira = chamadas[0]
    assert primeira["after"] == "2026-02-28T00:00:00"  # 1 dia antes do inicio
    assert primeira["before"] == "2026-04-02T00:00:00"  # 1 dia depois do fim exclusivo
    assert primeira["per_page"] == 100
    assert primeira["orderby"] == "date"
    assert primeira["order"] == "asc"
    assert "content" in primeira["_fields"]


def test_para_em_uma_pagina_quando_total_paginas_e_1(monkeypatch) -> None:
    _captura_persistencia(monkeypatch)
    chamadas: list[dict] = []
    with WordPressCollector(desde=DESDE, ate=ATE) as coletor:
        monkeypatch.setattr(
            coletor,
            "fetch",
            _stub_fetch({1: ITENS}, total=4, total_paginas=1, chamadas=chamadas),
        )
        coletor.process(_unidade())
    assert [c["page"] for c in chamadas] == [1]


def test_post_repetido_entre_paginas_conta_uma_vez(monkeypatch) -> None:
    gravacoes = _captura_persistencia(monkeypatch)
    chamadas: list[dict] = []
    # Um post publicado durante a paginacao aparece nas duas paginas.
    paginas = {1: [ITENS[0], ITENS[1]], 2: [ITENS[1], ITENS[2]]}
    with WordPressCollector(desde=DESDE, ate=ATE) as coletor:
        monkeypatch.setattr(
            coletor,
            "fetch",
            _stub_fetch(paginas, total=3, total_paginas=2, chamadas=chamadas),
        )
        resultado = coletor.process(_unidade())

    ids = [linha["wp_id"] for linha in gravacoes[0]["rows"]]
    assert len(ids) == len(set(ids))
    assert resultado.items_found == len(ids)


def test_cabecalhos_lidos_sem_diferenciar_maiusculas(monkeypatch) -> None:
    _captura_persistencia(monkeypatch)
    chamadas: list[dict] = []

    def fake(url, **kwargs):
        params = kwargs["params"]
        chamadas.append(params)
        # Cabecalhos em minusculas: se nao forem lidos, total=0 e o mes fechado daria erro.
        headers = {"x-wp-total": "4", "x-wp-totalpages": "2"}
        paginas = {1: ITENS[:2], 2: ITENS[2:]}
        return _Resposta(paginas[params["page"]], headers)

    with WordPressCollector(desde=DESDE, ate=ATE) as coletor:
        monkeypatch.setattr(coletor, "fetch", fake)
        coletor.process(_unidade())  # nao levanta: total 4 == 4 ids unicos

    assert [c["page"] for c in chamadas] == [1, 2]


# --- completude --------------------------------------------------------------------


def test_divergencia_em_mes_fechado_levanta_runtime_error(monkeypatch) -> None:
    _captura_persistencia(monkeypatch)
    chamadas: list[dict] = []
    with WordPressCollector(desde=dt.date(2020, 1, 1), ate=dt.date(2020, 2, 1)) as coletor:
        monkeypatch.setattr(
            coletor,
            "fetch",
            _stub_fetch({1: ITENS}, total=5, total_paginas=1, chamadas=chamadas),
        )
        with pytest.raises(RuntimeError, match="divergencia de total em mes fechado"):
            coletor.process(_unidade(dt.date(2020, 1, 1), dt.date(2020, 2, 1)))


def test_divergencia_no_mes_corrente_so_avisa(monkeypatch, caplog) -> None:
    _captura_persistencia(monkeypatch)
    hoje = dt.datetime.now(ZoneInfo(get_settings().daily_tz)).date()
    ini = hoje.replace(day=1)
    fim = (ini.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    chamadas: list[dict] = []
    with WordPressCollector(desde=ini, ate=fim) as coletor:
        monkeypatch.setattr(
            coletor,
            "fetch",
            _stub_fetch({1: ITENS}, total=5, total_paginas=1, chamadas=chamadas),
        )
        with caplog.at_level(logging.WARNING, logger=wordpress.__name__):
            resultado = coletor.process(_unidade(ini, fim))

    assert resultado.items_found >= 0
    assert any("mudou no mes corrente" in r.getMessage() for r in caplog.records)


# --- filtro de date_gmt e pre-filtro de candidatos -----------------------------------


def test_so_candidatos_no_intervalo_utc_sao_persistidos(monkeypatch) -> None:
    gravacoes = _captura_persistencia(monkeypatch)
    petr = ITENS[2]  # Money Times: "A Petrobras (PETR4) informou..."
    assert petr["id"] == 1315446
    itens = [
        _post(1, "2026-03-31T23:59:59", petr["title"]["rendered"], petr["content"]["rendered"]),
        _post(2, "2026-04-01T00:00:00", petr["title"]["rendered"], petr["content"]["rendered"]),
        _post(3, "2026-03-01T00:00:00", petr["title"]["rendered"], petr["content"]["rendered"]),
        _post(4, "2026-02-28T23:59:59", petr["title"]["rendered"], petr["content"]["rendered"]),
        _post(5, "2026-03-15T10:00:00", "Dólar sobe com cenário externo",
              "<p>Moeda fecha em alta.</p>"),
    ]
    chamadas: list[dict] = []
    with WordPressCollector(desde=DESDE, ate=ATE) as coletor:
        monkeypatch.setattr(
            coletor,
            "fetch",
            _stub_fetch({1: itens}, total=5, total_paginas=1, chamadas=chamadas),
        )
        resultado = coletor.process(_unidade())

    assert len(gravacoes) == 1
    gravacao = gravacoes[0]
    assert gravacao["table"] is RawWpPost.__table__
    assert gravacao["index_elements"] == ["site", "wp_id"]
    assert gravacao["update_columns"] is None  # camada raw e append-only
    # 2 e 4 ficam fora de [inicio, fim) em UTC; 5 nao tem ticker; so 1 e 3 sao candidatos.
    assert sorted(linha["wp_id"] for linha in gravacao["rows"]) == [1, 3]
    assert resultado.items_found == 2
    linha = gravacao["rows"][0]
    assert linha["date_gmt"].tzinfo is not None
    assert linha["date_gmt"].utcoffset() == dt.timedelta(0)
    assert linha["wp_type"] == "post"
    assert linha["titulo_html"] == petr["title"]["rendered"]
    assert linha["content_html"] == petr["content"]["rendered"]


def test_persist_sem_candidatos_nao_toca_o_banco(monkeypatch) -> None:
    def proibido(*args, **kwargs):
        raise AssertionError("nao deveria tocar o banco")

    monkeypatch.setattr(wordpress, "session_scope", proibido)
    with WordPressCollector(desde=DESDE, ate=ATE) as coletor:
        coletor._persist([])  # nao levanta


def test_texto_bruto_nao_funde_palavras_e_nao_perde_pedacos() -> None:
    # text_content junta "Azu" e "l" em "Azul"; o itertext separado impede a fusao de paragrafos.
    assert "Azul" in texto_bruto("<p>Azu<strong>l</strong> sobe</p>")
    texto = texto_bruto("<p>Subiu 2%</p><p>PETR4</p>")
    assert re.search(r"\bPETR4\b", texto)
    assert texto_bruto(None) == ""
    assert texto_bruto("   ") == ""
