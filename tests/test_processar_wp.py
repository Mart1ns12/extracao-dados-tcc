"""Processamento das noticias WordPress (F7): planejamento puro, orquestracao e gravacao.

(a) planejar_wp e puro: posts sinteticos, sem banco e sem rede.
(b) gravar_plano e opt-in com banco (TCC_TESTES_COM_BANCO=1, fixture `engine`). Limpa so o que
    criou, pela marca MARCA nas URLs e pelos wp_id reservados.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, select, update

import tcc_scraper.enrich.processar_wp as mod
from tcc_scraper.db.models import Article, url_hash
from tcc_scraper.db.models_fontes import (
    RawWpPost,
    StagingHistoria,
    StagingNoticiaJulgamento,
    StagingWpProcessamento,
)
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.enrich.processar_wp import (
    GoogleRef,
    PostRaw,
    RegistroWP,
    gravar_plano,
    planejar_wp,
    processar_wp,
)
from tcc_scraper.llm.juiza_codigo import rotulo_modelo
from tcc_scraper.llm.juiza_prompt import VERSAO

DOMINIO = {
    "infomoney": "www.infomoney.com.br",
    "moneytimes": "www.moneytimes.com.br",
    "exame": "exame.com",
}
DESDE = dt.date(2026, 3, 1)
ATE = dt.date(2026, 3, 31)
TITULO_PETR = "Petrobras (PETR4) anuncia lucro recorde no trimestre"
PARAGRAFO = (
    "PETR4 segue em alta nesta sessao com investidores atentos ao cenario externo "
    "e ao preco do petroleo hoje."
)


def dia(d: int, h: int = 10, mes: int = 3) -> dt.datetime:
    return dt.datetime(2026, mes, d, h, tzinfo=dt.UTC)


def post(site: str, wp_id: int, quando: dt.datetime, titulo: str, *paragrafos: str,
         path: str | None = None) -> PostRaw:
    link = f"https://{DOMINIO[site]}/{path or f'noticia-{wp_id}'}/"
    corpo = "".join(f"<p>{p}</p>" for p in paragrafos)
    return PostRaw(site, wp_id, link, quando, titulo, corpo)


def historia_de(plano, rep: tuple[str, int]):
    return next(h for h in plano.historias if h.rep == rep)


def par_de(plano, rep: tuple[str, int], ticker: str):
    return next(p for p in historia_de(plano, rep).pares if p.ticker == ticker)


# --------------------------------------------------------------------------------------
# (a) planejamento puro
# --------------------------------------------------------------------------------------
def test_republicacao_entre_veiculos_vira_uma_historia_com_o_representante_mais_antigo():
    a = post("moneytimes", 7, dia(10, 12), TITULO_PETR, "Texto da materia sobre o balanco.")
    b = post("infomoney", 1, dia(10, 10), TITULO_PETR, "Outro paragrafo da mesma materia.")
    plano = planejar_wp([a, b], [], {}, DESDE, ATE)

    assert len(plano.historias) == 1
    h = plano.historias[0]
    assert h.rep == ("infomoney", 1)
    assert h.processar and h.article_id is None and h.tem_artigo
    n = len(h.pares)
    assert "PETR4" in [p.ticker for p in h.pares]
    assert set(plano.registros) == {
        RegistroWP("infomoney", 1, "representante", n, ("infomoney", 1)),
        RegistroWP("moneytimes", 7, "membro", n, ("infomoney", 1)),
    }


def test_par_com_ticker_no_titulo_gera_julgamento_de_codigo_k3():
    a = post("infomoney", 1, dia(10, 10), TITULO_PETR, "Texto da materia.")
    plano = planejar_wp([a], [], {}, DESDE, ATE)

    par = par_de(plano, ("infomoney", 1), "PETR4")
    assert par.camada == "K3"
    assert rotulo_modelo(par.camada) == "codigo/r1/K3"
    assert par.julgamento == {
        "papel": "protagonista", "tipo_conteudo": "reportagem", "evento": "resultado",
        "qualidade": 0.7, "confianca": 0.9, "tickers_adicionais": [],
    }


def test_par_revisavel_gera_vinculo_sem_julgamento_de_codigo():
    # Duas mencoes de codigo seguidas de cotacao (E6) nao vinculam, mas sao revisaveis:
    # vao para a juiza.
    corpo = "ITUB4 +2,5% na sessao. ITUB4 -1,0% no fim do dia."
    a = post("infomoney", 2, dia(10, 10), "Investidores avaliam o cenario economico do pais", corpo)
    plano = planejar_wp([a], [], {}, DESDE, ATE)

    par = par_de(plano, ("infomoney", 2), "ITUB4")
    assert par.camada == "R"
    assert par.julgamento is None
    assert historia_de(plano, ("infomoney", 2)).tem_artigo


def test_duplicata_do_google_casa_por_url_e_por_titulo_e_aponta_para_o_representante():
    a = post("infomoney", 1, dia(10, 10), TITULO_PETR, "Texto.")
    b = post("moneytimes", 7, dia(10, 12), TITULO_PETR, "Outro texto.")
    g_url = GoogleRef(901, "infomoney.com.br", TITULO_PETR + " - InfoMoney",
                      a.link + "?utm_source=google", dia(10, 11))
    g_tit = GoogleRef(902, "moneytimes.com.br", TITULO_PETR + " - Money Times",
                      "https://news.example.com/materia", dia(11, 9))
    plano = planejar_wp([a, b], [g_url, g_tit], {}, DESDE, ATE)

    h = historia_de(plano, ("infomoney", 1))
    assert sorted(h.google) == [(901, "google_url"), (902, "google_titulo")]


def test_boilerplate_e_causal_so_olha_posts_anteriores():
    """Mesmo paragrafo em posts ANTERIORES (dentro de 30 d) vira boilerplate: a mencao passa a
    ser revisavel (R). Posts POSTERIORES nao mudam a decisao do post em analise (K7)."""

    def passados():
        return [post("infomoney", i, dia(2 + i, 9), f"Analise numero {i} sobre fundos",
                     PARAGRAFO) for i in range(1, 6)]

    def futuros():
        return [post("infomoney", 20 + i, dia(12 + i, 9), f"Analise numero {i} sobre bancos",
                     PARAGRAFO) for i in range(5)]

    alvo = post("infomoney", 10, dia(10, 10), "Investidores avaliam o cenario economico do pais",
                PARAGRAFO, PARAGRAFO)

    def camada_do_alvo(posts):
        plano = planejar_wp(posts, [], {}, DESDE, ATE)
        return par_de(plano, ("infomoney", 10), "PETR4").camada

    assert camada_do_alvo([alvo]) == "K7"
    assert camada_do_alvo([alvo, *futuros()]) == "K7"
    assert camada_do_alvo([alvo, *passados()]) == "R"


def test_post_sem_ticker_gera_so_o_registro_sem_artigo():
    a = post("exame", 4, dia(10, 10), "Economia cresce no trimestre", "Texto sem empresas.")
    plano = planejar_wp([a], [], {}, DESDE, ATE)

    assert plano.historias == []
    assert plano.registros == [RegistroWP("exame", 4, "sem_ticker", 0, ("exame", 4))]


def test_post_fora_do_intervalo_entra_so_como_contexto():
    antes = post("infomoney", 3, dt.datetime(2026, 2, 20, 10, tzinfo=dt.UTC), TITULO_PETR, "Texto.")
    plano = planejar_wp([antes], [], {}, DESDE, ATE)

    assert plano.posts_alvo == 0
    assert plano.historias == [] and plano.registros == []


def test_historia_ja_processada_nao_refaz_pares_mas_atualiza_o_vinculo_do_google():
    a = post("infomoney", 1, dia(10, 10), TITULO_PETR, "Texto.")
    b = post("moneytimes", 7, dia(10, 12), TITULO_PETR, "Outro texto.")
    g = GoogleRef(901, "infomoney.com.br", TITULO_PETR, a.link, dia(10, 11))
    ja = {("infomoney", 1): 55, ("moneytimes", 7): 55}

    plano = planejar_wp([a, b], [g], ja, DESDE, ATE)

    assert plano.registros == []
    h = historia_de(plano, ("infomoney", 1))
    assert not h.processar and h.article_id == 55 and h.pares == []
    assert h.google == [(901, "google_url")]


def test_refazer_reprocessa_mesmo_com_tudo_ja_registrado():
    a = post("infomoney", 1, dia(10, 10), TITULO_PETR, "Texto.")
    ja = {("infomoney", 1): 55}
    plano = planejar_wp([a], [], ja, DESDE, ATE, refazer=True)

    h = historia_de(plano, ("infomoney", 1))
    assert h.processar and h.article_id == 55
    assert "PETR4" in [p.ticker for p in h.pares]
    assert plano.registros


# --------------------------------------------------------------------------------------
# orquestracao (loaders e gravacao substituidos, sem banco)
# --------------------------------------------------------------------------------------
class _Observador:
    def __init__(self, parar: bool = False) -> None:
        self.parar = parar
        self.plano_total: int | None = None
        self.unidades: list[tuple[str, bool]] = []

    def plano(self, total: int) -> None:
        self.plano_total = total

    def unidade(self, key: str, *, ok: bool, itens: int, saturada: bool, erro: str | None) -> None:
        self.unidades.append((key, ok))

    def deve_parar(self) -> bool:
        return self.parar


def test_processar_wp_percorre_os_meses_e_devolve_as_chaves_do_collector(monkeypatch):
    chamadas: list[tuple[dt.date, dt.date]] = []

    def ler(ini, fim):
        chamadas.append((ini, fim))
        return [], [], {}

    monkeypatch.setattr(mod, "_ler_janela", ler)
    monkeypatch.setattr(mod, "gravar_plano", lambda plano: {"historias": 0})
    obs = _Observador()

    stats = processar_wp(dt.date(2026, 1, 15), dt.date(2026, 3, 1), observador=obs)

    assert chamadas == [
        (dt.date(2026, 1, 15), dt.date(2026, 2, 1)),
        (dt.date(2026, 2, 1), dt.date(2026, 3, 1)),
    ]
    assert stats == {"total": 2, "ok": 2, "erro": 0, "itens": 0, "saturadas": 0, "interrompido": 0}
    assert obs.plano_total == 2
    assert [ok for _, ok in obs.unidades] == [True, True]


def test_processar_wp_isola_a_falha_de_um_mes(monkeypatch):
    def ler(ini, fim):
        if ini == dt.date(2026, 1, 1):
            raise RuntimeError("banco indisponivel")
        return [], [], {}

    monkeypatch.setattr(mod, "_ler_janela", ler)
    monkeypatch.setattr(mod, "gravar_plano", lambda plano: {})

    stats = processar_wp(dt.date(2026, 1, 1), dt.date(2026, 3, 1))

    assert stats["total"] == 2 and stats["ok"] == 1 and stats["erro"] == 1


def test_processar_wp_para_quando_o_observador_pede(monkeypatch):
    monkeypatch.setattr(mod, "_ler_janela", lambda ini, fim: pytest.fail("nao deveria ler"))

    stats = processar_wp(DESDE, ATE, observador=_Observador(parar=True))

    assert stats["interrompido"] == 1 and stats["ok"] == 0


# --------------------------------------------------------------------------------------
# (b) gravacao com banco (opt-in)
# --------------------------------------------------------------------------------------
MARCA = "zzzz9-teste-processar-wp"
IDS_TESTE = (990000001, 990000002, 990000003)


def _limpar_teste() -> None:
    with session_scope() as s:
        s.execute(delete(StagingWpProcessamento).where(
            StagingWpProcessamento.wp_id.in_(IDS_TESTE)))
        marcados = select(Article.id).where(Article.url.like(f"%{MARCA}%"))
        s.execute(delete(StagingHistoria).where(StagingHistoria.historia_article_id.in_(marcados)))
        s.execute(delete(Article).where(Article.url.like(f"%{MARCA}%")))
        s.execute(delete(RawWpPost).where(RawWpPost.wp_id.in_(IDS_TESTE)))


def _contar_julgamento(s, article_id: int) -> list[str]:
    return [m for (m,) in s.execute(select(StagingNoticiaJulgamento.modelo).where(
        StagingNoticiaJulgamento.article_id == article_id,
        StagingNoticiaJulgamento.ticker == "PETR4",
        StagingNoticiaJulgamento.prompt_versao == VERSAO,
    )).all()]


def test_gravar_plano_grava_uma_vez_e_nao_sobrescreve_julgamento(engine):
    a = post("infomoney", IDS_TESTE[0], dia(10, 10), TITULO_PETR, "Texto.", path=f"{MARCA}-a")
    b = post("moneytimes", IDS_TESTE[1], dia(10, 12), TITULO_PETR, "Outro texto.",
             path=f"{MARCA}-b")
    c = post("exame", IDS_TESTE[2], dia(10, 13), "Economia cresce no trimestre", "Sem empresas.",
             path=f"{MARCA}-c")
    gid_linha = {
        "url_hash": url_hash(f"https://news.google.com/rss/articles/{MARCA}-g"),
        "url": f"https://news.google.com/rss/articles/{MARCA}-g",
        "google_id": None, "resolved_url": a.link, "title": TITULO_PETR + " - InfoMoney",
        "source_name": "InfoMoney", "source_domain": "infomoney.com.br",
        "published_at": dia(10, 11), "published_precision": "datetime",
        "published_time_source": None, "lang": "pt", "collected_at": dia(10, 11),
        "origem": "google_news",
    }
    _limpar_teste()
    try:
        with session_scope() as s:
            bulk_upsert(s, RawWpPost.__table__, [
                {"site": p.site, "wp_id": p.wp_id, "link": p.link, "date_gmt": p.date_gmt,
                 "wp_type": "post", "titulo_html": p.titulo_html, "content_html": p.content_html,
                 "fetched_at": dia(10, 14)} for p in (a, b, c)
            ], index_elements=["site", "wp_id"], update_columns=None)
            bulk_upsert(s, Article.__table__, [gid_linha], index_elements=["url_hash"],
                        update_columns=None)
            s.flush()
            gid = s.execute(select(Article.id).where(
                Article.url_hash == gid_linha["url_hash"])).scalar_one()

        g = GoogleRef(gid, "infomoney.com.br", gid_linha["title"], a.link, dia(10, 11))
        plano = planejar_wp([a, b, c], [g], {}, DESDE, ATE)

        primeira = gravar_plano(plano)
        assert primeira["artigos"] == 1

        with session_scope() as s:
            rep_id = s.execute(select(Article.id).where(
                Article.url_hash == url_hash(a.link))).scalar_one()
            assert _contar_julgamento(s, rep_id) == ["codigo/r1/K3"]
            historia = s.execute(select(StagingHistoria.historia_article_id,
                                        StagingHistoria.metodo).where(
                StagingHistoria.article_id == gid)).one()
            assert tuple(historia) == (rep_id, "google_url")
            registros = s.execute(select(StagingWpProcessamento.wp_id).where(
                StagingWpProcessamento.wp_id.in_(IDS_TESTE))).all()
            assert len(registros) == 3

            # um julgamento de LLM ja gravado nao pode ser trocado pelo de codigo
            s.execute(update(StagingNoticiaJulgamento).where(
                StagingNoticiaJulgamento.article_id == rep_id,
                StagingNoticiaJulgamento.ticker == "PETR4",
            ).values(modelo="llm-teste-zzzz9"))

        gravar_plano(plano)  # segunda vez: nada duplica

        with session_scope() as s:
            rep_id = s.execute(select(Article.id).where(
                Article.url_hash == url_hash(a.link))).scalar_one()
            assert _contar_julgamento(s, rep_id) == ["llm-teste-zzzz9"]
            n_artigos = s.execute(select(Article.id).where(
                Article.url.like(f"%{MARCA}%"))).all()
            assert len(n_artigos) == 2  # representante e o Google
    finally:
        _limpar_teste()
