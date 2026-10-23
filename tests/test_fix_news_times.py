"""Testes do fix-news-times e da recuperacao de hora no enrich, sem banco e sem rede.

O banco e substituido por listas em memoria (buscar_candidatos_hora e _gravar_horas), e
a pagina por um fetch falso. O dry-run tem um teste proprio que garante que nada e gravado.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tcc_scraper.enrich import article_fetcher as af

DOMINIO = "infomoney.com.br"
URL = "https://www.infomoney.com.br/materia-exemplo"

# PDT: 00:00 de Los Angeles em 15/07/2026 = 07:00 UTC.
MEIA_NOITE_PACIFICA = dt.datetime(2026, 7, 15, 7, 0, tzinfo=dt.UTC)
# 00:00 de SP em 15/07/2026 = 03:00 UTC (valor que a fase (a) grava).
INICIO_SP = dt.datetime(2026, 7, 15, 3, 0, tzinfo=dt.UTC)
# Hora normal, sem sinal de meia-noite pacifica.
HORA_NORMAL = dt.datetime(2026, 7, 15, 15, 30, tzinfo=dt.UTC)

HTML_COM_HORA = (
    '<html><head><meta property="article:published_time" '
    'content="2026-07-15T14:30:00-03:00"></head></html>'
)


def test_planejar_remarca_meia_noite_e_ignora_hora_normal() -> None:
    candidatos = [
        (1, MEIA_NOITE_PACIFICA, "datetime", URL, DOMINIO),
        (2, HORA_NORMAL, "datetime", URL, DOMINIO),
    ]
    plano = af.planejar_correcao_hora(candidatos)

    assert plano.remarcar == [(1, INICIO_SP)]
    assert [p[0] for p in plano.paginas] == [1]
    assert plano.paginas[0][2] == INICIO_SP


def test_planejar_separa_sem_url_e_fora_da_allowlist() -> None:
    candidatos = [
        (1, INICIO_SP, "date", None, DOMINIO),
        (2, INICIO_SP, "date", URL, "exemplo-fora-da-lista.com"),
        (3, INICIO_SP, "date", URL, DOMINIO),
    ]
    plano = af.planejar_correcao_hora(candidatos)

    assert plano.sem_url == 1
    assert plano.fora_da_allowlist == 1
    assert [p[0] for p in plano.paginas] == [3]


def test_dry_run_nao_grava_e_conta_o_que_seria_corrigido(monkeypatch) -> None:
    monkeypatch.setattr(
        af,
        "buscar_candidatos_hora",
        lambda: [
            (1, MEIA_NOITE_PACIFICA, "datetime", URL, DOMINIO),
            (2, INICIO_SP, "date", URL, DOMINIO),
        ],
    )
    monkeypatch.setattr(
        af.ArticleBodyCollector,
        "fetch",
        lambda self, url, **kw: SimpleNamespace(text=HTML_COM_HORA),
    )

    def proibido(*args, **kwargs):
        raise AssertionError("dry-run nao pode gravar")

    monkeypatch.setattr(af, "_gravar_horas", proibido)

    r = af.corrigir_horas_publicacao(dry_run=True)

    assert r["remarcados_para_date"] == 1
    assert r["paginas_candidatas"] == 2
    assert r["hora_corrigida_pela_pagina"] == 2
    assert r["gravados"] == 0


def test_execucao_real_grava_remarcacao_e_hora_da_pagina(monkeypatch) -> None:
    monkeypatch.setattr(
        af,
        "buscar_candidatos_hora",
        lambda: [
            (1, MEIA_NOITE_PACIFICA, "datetime", URL, DOMINIO),
            (2, INICIO_SP, "date", URL, DOMINIO),
        ],
    )
    monkeypatch.setattr(
        af.ArticleBodyCollector,
        "fetch",
        lambda self, url, **kw: SimpleNamespace(text=HTML_COM_HORA),
    )
    gravacoes: list[list[dict]] = []
    monkeypatch.setattr(af, "_gravar_horas", lambda linhas: gravacoes.append(linhas))

    r = af.corrigir_horas_publicacao(dry_run=False)

    remarcacao, correcao = gravacoes
    assert remarcacao == [{"_id": 1, "_hora": INICIO_SP, "_precisao": "date", "_origem": None}]
    assert {linha["_id"] for linha in correcao} == {1, 2}
    assert all(linha["_precisao"] == "datetime" for linha in correcao)
    assert all(linha["_origem"] == "pagina" for linha in correcao)
    assert r["gravados"] == 3


def test_pagina_com_hora_de_outro_dia_nao_corrige(monkeypatch) -> None:
    monkeypatch.setattr(
        af, "buscar_candidatos_hora", lambda: [(2, INICIO_SP, "date", URL, DOMINIO)]
    )
    html_outro_dia = (
        '<meta property="article:published_time" content="2026-07-20T10:00:00-03:00">'
    )
    monkeypatch.setattr(
        af.ArticleBodyCollector,
        "fetch",
        lambda self, url, **kw: SimpleNamespace(text=html_outro_dia),
    )

    r = af.corrigir_horas_publicacao(dry_run=True)

    assert r["hora_corrigida_pela_pagina"] == 0
    assert r["hora_fora_da_janela"] == 1


def test_limite_restringe_so_as_paginas_baixadas(monkeypatch) -> None:
    monkeypatch.setattr(
        af,
        "buscar_candidatos_hora",
        lambda: [(i, INICIO_SP, "date", URL, DOMINIO) for i in range(5)],
    )
    baixados: list[str] = []

    def fetch(self, url, **kw):
        baixados.append(url)
        return SimpleNamespace(text="<html></html>")

    monkeypatch.setattr(af.ArticleBodyCollector, "fetch", fetch)

    r = af.corrigir_horas_publicacao(limite=2, dry_run=True)

    assert len(baixados) == 2
    assert r["paginas_candidatas"] == 5


def test_enrich_guarda_hora_da_pagina_so_para_artigo_com_data(monkeypatch) -> None:
    """O gancho do enrich: artigo 'date' ganha a hora; artigo 'datetime' nao entra."""
    gravadas: list[list[dict]] = []
    monkeypatch.setattr(af, "_gravar_horas", lambda linhas: gravadas.append(linhas))

    with af.ArticleBodyCollector(workers=1) as c:
        c._publicados_em = {7: INICIO_SP}
        c._tentar_hora_da_pagina(7, HTML_COM_HORA)
        c._tentar_hora_da_pagina(8, HTML_COM_HORA)  # sem data registrada: ignorado
        c._persist_horas()

    assert gravadas == [
        [
            {
                "_id": 7,
                "_hora": dt.datetime(2026, 7, 15, 17, 30, tzinfo=dt.UTC),
                "_precisao": "datetime",
                "_origem": "pagina",
            }
        ]
    ]


@pytest.mark.parametrize("html", ["", "<html><body>sem data</body></html>"])
def test_enrich_sem_hora_na_pagina_nao_altera_nada(monkeypatch, html) -> None:
    gravadas: list[list[dict]] = []
    monkeypatch.setattr(af, "_gravar_horas", lambda linhas: gravadas.append(linhas))

    with af.ArticleBodyCollector(workers=1) as c:
        c._publicados_em = {7: INICIO_SP}
        c._tentar_hora_da_pagina(7, html)
        c._persist_horas()

    assert gravadas == []
