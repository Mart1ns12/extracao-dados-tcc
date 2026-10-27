"""Historias: republicacao entre veiculos, union-find, representante e casamento do Google.

Funcoes puras, com posts sinteticos. Sem banco e sem rede.
"""
from __future__ import annotations

import datetime as dt

from tcc_scraper.enrich.historias import (
    IndiceWP,
    Ref,
    canon,
    casar,
    escolhe_representante,
    grupos_historias,
    norm_titulo,
    pares_republicacao,
)

UTC = dt.UTC
TITULO = "Petrobras anuncia lucro recorde no primeiro trimestre de 2026 com alta de receita"


def ref(site: str, ident: int, titulo: str, dia: int, hora: int = 10,
        link: str | None = None) -> Ref:
    pub = dt.datetime(2026, 3, dia, hora, tzinfo=UTC)
    return Ref(site, ident, norm_titulo(titulo), canon(link), pub)


def test_canon_ignora_www_http_amp_barra_e_querystring_de_tracking():
    a = canon("http://www.infomoney.com.br/mercados/noticia-1/amp/?utm_source=x")
    b = canon("https://infomoney.com.br/mercados/noticia-1")
    assert a == b == "https://infomoney.com.br/mercados/noticia-1"
    assert canon(None) is None


def test_norm_titulo_corta_sufixo_do_google_so_quando_a_cauda_e_curta():
    assert norm_titulo("Petrobras sobe - InfoMoney", cortar_sufixo=True) == "petrobras sobe"
    longa = "Petrobras sobe - " + "palavra " * 10
    assert norm_titulo(longa, cortar_sufixo=True).startswith("petrobras sobe palavra")
    assert norm_titulo("Acao: Vale  ", cortar_sufixo=False) == "acao vale"


def test_republicacao_com_titulo_igual_entre_sites_forma_par():
    a = ref("infomoney", 1, TITULO, 10)
    b = ref("moneytimes", 7, TITULO, 10, hora=12)
    pares = pares_republicacao([a, b])
    assert len(pares) == 1
    assert pares[0][0] == 1.0


def test_republicacao_fuzzy_acima_de_095_forma_par_e_abaixo_nao():
    quase = TITULO.replace("de receita", "da receita")
    a = ref("infomoney", 1, TITULO, 10)
    assert len(pares_republicacao([a, ref("exame", 3, quase, 10)])) == 1
    assert pares_republicacao([a, ref("exame", 3, "Vale fecha acordo com a China", 10)]) == []


def test_republicacao_ignora_mesmo_veiculo_e_dias_distantes():
    a = ref("infomoney", 1, TITULO, 10)
    assert pares_republicacao([a, ref("infomoney", 2, TITULO, 10, hora=12)]) == []
    assert pares_republicacao([a, ref("moneytimes", 7, TITULO, 12)]) == []
    # um dia de diferenca ainda conta; dois nao
    assert len(pares_republicacao([a, ref("moneytimes", 7, TITULO, 11)])) == 1


def test_union_find_une_cadeia_e_representante_e_o_mais_antigo():
    a = ref("moneytimes", 7, TITULO, 10, hora=12)
    b = ref("infomoney", 1, TITULO, 10, hora=9)
    c = ref("exame", 3, TITULO, 11, hora=8)  # republica depois de b
    isolado = ref("exame", 9, "Outro assunto totalmente diferente sobre bancos", 10)
    refs = [a, b, c, isolado]
    grupos = grupos_historias(refs, pares_republicacao(refs))
    tamanhos = sorted(len(g) for g in grupos)
    assert tamanhos == [1, 3]
    grande = next(g for g in grupos if len(g) == 3)
    assert escolhe_representante(grande).chave == ("infomoney", 1)


def test_representante_empata_pela_ordem_de_sites_e_depois_pelo_id():
    a = ref("exame", 5, TITULO, 10, hora=9)
    b = ref("infomoney", 9, TITULO, 10, hora=9)
    assert escolhe_representante([a, b]).site == "infomoney"
    c = ref("infomoney", 4, TITULO, 10, hora=9)
    assert escolhe_representante([b, c]).ident == 4


def test_casar_google_por_url_canonica_antes_do_titulo():
    wp = ref("infomoney", 1, TITULO, 10, link="https://www.infomoney.com.br/noticia-1/")
    idx = IndiceWP([wp])
    g = Ref("infomoney", 900, norm_titulo("Outro titulo - InfoMoney", True),
            canon("https://www.infomoney.com.br/noticia-1?utm_source=google"),
            dt.datetime(2026, 3, 10, 11, tzinfo=UTC))
    assert casar(g, idx) == ("google_url", wp)


def test_casar_google_por_titulo_igual_e_fuzzy_no_mesmo_veiculo():
    wp = ref("infomoney", 1, TITULO, 10)
    idx = IndiceWP([wp])
    igual = Ref("infomoney", 901, norm_titulo(TITULO + " - InfoMoney", True), None,
                dt.datetime(2026, 3, 11, 9, tzinfo=UTC))
    assert casar(igual, idx) == ("google_titulo", wp)
    fuzzy_titulo = TITULO.replace("de receita", "da receita") + " - InfoMoney"
    fuzzy = Ref("infomoney", 902, norm_titulo(fuzzy_titulo, True), None,
                dt.datetime(2026, 3, 10, 9, tzinfo=UTC))
    assert casar(fuzzy, idx) == ("google_fuzzy", wp)


def test_casar_nao_cruza_veiculo_nem_passa_da_tolerancia_de_dois_dias():
    wp = ref("infomoney", 1, TITULO, 10)
    idx = IndiceWP([wp])
    outro_veiculo = Ref("moneytimes", 903, norm_titulo(TITULO), None,
                        dt.datetime(2026, 3, 10, 9, tzinfo=UTC))
    assert casar(outro_veiculo, idx) == (None, None)
    tarde = Ref("infomoney", 904, norm_titulo(TITULO), None,
                dt.datetime(2026, 3, 13, 9, tzinfo=UTC))
    assert casar(tarde, idx) == (None, None)
