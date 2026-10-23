"""Composicao do IBOV: carteiras oficiais, eventos, vigencias e parse da API (sem banco)."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from tcc_scraper.collectors import b3_carteira_ibov as B
from tcc_scraper.config import REBALANCEAMENTOS_IBOV
from tcc_scraper.entrega import composicao_ibov as C
from tcc_scraper.entrega.composicao_ibov import (
    EventoIbov,
    aplicar_eventos,
    carregar_carteiras_csv,
    inicio_da_carteira,
    linha_do_tempo_ibov,
    montar_vigencias,
    primeiro_dia_util,
)

FIXTURES = Path(__file__).parent / "fixtures"
D = dt.date


def _linhas(vigencias: list[dict]) -> list[tuple]:
    return [
        (v["ticker"], v["vigencia_inicio"], v["vigencia_fim"], v["participacao"], v["fonte"])
        for v in vigencias
    ]


# -- datas de inicio ----------------------------------------------------------------------

def test_primeiro_dia_util_pula_fim_de_semana():
    assert primeiro_dia_util(2021, 1) == D(2021, 1, 1)  # sexta; feriado nao conta
    assert primeiro_dia_util(2021, 5) == D(2021, 5, 3)  # 1o de maio de 2021 foi sabado


def test_inicio_usa_data_conhecida_ou_primeiro_dia_util():
    assert inicio_da_carteira("IBOV_2024-05-02_2024-08-02.csv") == D(2024, 5, 6)
    assert inicio_da_carteira("IBOV_2023-09-02_2023-12-02.csv") == D(2023, 9, 4)
    assert inicio_da_carteira("IBOV_2021-09-02_2021-12-02.csv") == D(2021, 9, 1)


# -- CSV das carteiras oficiais -----------------------------------------------------------

def test_csv_com_bom_e_crlf_e_lido_com_pesos(tmp_path: Path):
    conteudo = (
        "Codigo,Acao,Tipo,Qtde Teorica,Part %\r\n"
        "ABEV3,AMBEV,ON,1,4.8429\r\n"
        "BBAS3,BB,ON,1,\r\n"
    )
    (tmp_path / "IBOV_2021-01-02_2021-04-02.csv").write_bytes(
        b"\xef\xbb\xbf" + conteudo.encode("utf-8")
    )

    carteiras = carregar_carteiras_csv(tmp_path)

    assert carteiras == [(D(2021, 1, 1), {"ABEV3": 4.8429, "BBAS3": None})]


def test_csv_com_codigo_repetido_levanta(tmp_path: Path):
    (tmp_path / "IBOV_2021-01-02_2021-04-02.csv").write_text(
        "Codigo,Acao,Tipo,Qtde Teorica,Part %\nABEV3,A,ON,1,1\nABEV3,A,ON,1,1\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="repetido"):
        carregar_carteiras_csv(tmp_path)


def test_carteiras_reais_carregam_em_ordem_e_cobrem_2021_a_2024():
    carteiras = carregar_carteiras_csv(C.PASTA_CARTEIRAS)

    datas = [d for d, _ in carteiras]
    assert datas == sorted(datas)
    assert datas[0] == D(2021, 1, 1)
    assert datas[-1] == D(2024, 9, 2)
    assert len(carteiras) == 12  # 4 anos x 3 carteiras (jan, mai, set), sem 2019 e 2020
    assert "ALPA4" in carteiras[-1][1]


# -- eventos ----------------------------------------------------------------------------

def test_saida_de_papel_ausente_nao_muda_a_carteira():
    timeline = aplicar_eventos({"A": 1.0}, [EventoIbov(D(2025, 1, 6), (), ("Z",))])

    assert timeline == [(D(2025, 1, 6), {"A": 1.0})]


def test_entrada_de_papel_ja_presente_mantem_o_peso():
    timeline = aplicar_eventos({"A": 1.0}, [EventoIbov(D(2025, 1, 6), ("A",), ())])

    assert timeline == [(D(2025, 1, 6), {"A": 1.0})]


def test_eventos_do_mesmo_dia_geram_um_item_so():
    eventos = [
        EventoIbov(D(2025, 1, 6), ("B",), ()),
        EventoIbov(D(2025, 1, 6), (), ("A",)),
    ]

    timeline = aplicar_eventos({"A": 1.0}, eventos)

    assert timeline == [(D(2025, 1, 6), {"B": None})]


def test_troca_de_codigo_so_vale_se_o_antigo_estava_na_carteira():
    troca = EventoIbov(D(2025, 11, 10), ("AXIA3",), ("ELET3",), so_se_presente=True)

    ausente = aplicar_eventos({"A": 1.0}, [troca])
    presente = aplicar_eventos({"ELET3": 3.3, "A": 1.0}, [troca])

    assert ausente == [(D(2025, 11, 10), {"A": 1.0})]
    assert presente == [(D(2025, 11, 10), {"AXIA3": None, "A": 1.0})]


def test_troca_cria_um_periodo_para_cada_codigo():
    carteira = {"NTCO3": 0.5}
    eventos = [EventoIbov(D(2025, 7, 2), ("NATU3",), ("NTCO3",), so_se_presente=True)]

    linhas = montar_vigencias(
        [(D(2024, 9, 2), carteira, "paccafelipe_csv")]
        + [(d, c, "rebalanceamento") for d, c in aplicar_eventos(carteira, eventos)]
    )

    assert _linhas(linhas) == [
        ("NATU3", D(2025, 7, 2), None, None, "rebalanceamento"),
        ("NTCO3", D(2024, 9, 2), D(2025, 7, 1), 0.5, "paccafelipe_csv"),
    ]


def test_eventos_de_rebalanceamento_seguem_a_config():
    eventos = C.eventos_rebalanceamento()

    assert len(eventos) == len(REBALANCEAMENTOS_IBOV) == 6
    assert eventos[0] == EventoIbov(D(2025, 1, 6), ("POMO4", "PSSA3"), ("ALPA4", "EZTC3"))


def test_evento_extraordinario_segue_a_lista_explicita(monkeypatch):
    monkeypatch.setattr(
        C, "EVENTOS_IBOV_EXTRAORDINARIOS", [(D(2025, 1, 6), ("NOVO3",), ("VELHO3",))]
    )

    assert C.eventos_extraordinarios() == [EventoIbov(D(2025, 1, 6), ("NOVO3",), ("VELHO3",))]


def test_evento_extraordinario_muda_a_linha_do_tempo_real():
    por_data = {
        data: carteira
        for data, carteira, _ in linha_do_tempo_ibov(carregar_carteiras_csv(C.PASTA_CARTEIRAS))
    }

    assert "AMOB3" in por_data[D(2024, 12, 16)]  # cisao da Vamos, entrada sem papel de saida
    assert "RRRP3" not in por_data[D(2024, 9, 9)]  # 3R vira Brava: troca de codigo
    assert "BRAV3" in por_data[D(2024, 9, 9)]
    assert "BRKM5" not in por_data[D(2026, 8, 26)]  # recuperacao extrajudicial


def test_troca_coberta_por_evento_explicito_nao_e_aplicada_de_novo():
    explicitos = C.eventos_rebalanceamento() + C.eventos_extraordinarios()

    novos = {papel for evento in C.eventos_trocas(explicitos) for papel in evento.entram}

    # JBSS32 (BDR, fora do indice), AUAU3, AZUL3, AXIA3 e CPLE3 saem de trocas ja tratadas.
    assert novos.isdisjoint({"JBSS32", "AUAU3", "AZUL3", "AXIA3", "CPLE3"})
    assert "EMBJ3" in novos  # sem evento explicito: segue como troca


def test_troca_e_ignorada_quando_o_codigo_antigo_nao_esta_na_carteira():
    timeline = aplicar_eventos({}, C.eventos_trocas())

    assert timeline  # ha datas de troca, mas nenhuma adiciona papel sem o antigo
    assert all(carteira == {} for _, carteira in timeline)


# -- linha do tempo e vigencias ---------------------------------------------------------

def test_linha_do_tempo_so_usa_eventos_depois_da_ultima_carteira_oficial():
    carteiras = [
        (D(2024, 1, 2), {"A": 1.0, "ALPA4": 0.1}),
        (D(2024, 9, 2), {"A": 1.5, "ALPA4": 0.2}),
    ]

    linha = linha_do_tempo_ibov(carteiras)

    fontes = {data: fonte for data, _, fonte in linha}
    assert fontes[D(2024, 1, 2)] == "paccafelipe_csv"
    assert fontes[D(2025, 1, 6)] == "rebalanceamento"
    assert all(data > D(2024, 9, 2) for data, _, f in linha if f == "rebalanceamento")


def test_vigencia_fecha_na_vespera_da_primeira_carteira_sem_o_papel():
    linha = [
        (D(2024, 1, 2), {"A": 1.0, "B": 2.0}, "paccafelipe_csv"),
        (D(2024, 5, 6), {"A": 1.5}, "paccafelipe_csv"),
        (D(2024, 9, 2), {"A": 1.2, "B": None}, "paccafelipe_csv"),
    ]

    assert _linhas(montar_vigencias(linha)) == [
        ("A", D(2024, 1, 2), None, 1.0, "paccafelipe_csv"),
        ("B", D(2024, 1, 2), D(2024, 5, 5), 2.0, "paccafelipe_csv"),
        ("B", D(2024, 9, 2), None, None, "paccafelipe_csv"),
    ]


def test_participacao_e_fonte_vem_do_dia_em_que_o_periodo_comeca():
    linha = [
        (D(2024, 1, 2), {"A": 1.0}, "paccafelipe_csv"),
        (D(2025, 1, 6), {"A": None, "C": None}, "rebalanceamento"),
    ]

    vigencias = montar_vigencias(linha)

    assert _linhas(vigencias) == [
        ("A", D(2024, 1, 2), None, 1.0, "paccafelipe_csv"),
        ("C", D(2025, 1, 6), None, None, "rebalanceamento"),
    ]


def test_montar_vigencias_rejeita_duas_carteiras_no_mesmo_dia():
    with pytest.raises(ValueError, match="mesmo dia"):
        montar_vigencias([(D(2024, 1, 2), {}, "x"), (D(2024, 1, 2), {}, "x")])


def test_carteira_real_fica_com_as_vigencias_abertas_da_ultima_carteira_da_linha_do_tempo():
    linha = linha_do_tempo_ibov(carregar_carteiras_csv(C.PASTA_CARTEIRAS))
    ultima = linha[-1][1]

    abertas = {v["ticker"] for v in montar_vigencias(linha) if v["vigencia_fim"] is None}

    assert abertas == set(ultima)


def test_reconstrucao_ate_2026_10_23_bate_com_a_carteira_da_api():
    data, api = B.parse_carteira(_corpo_api())
    vigencias = montar_vigencias(linha_do_tempo_ibov(carregar_carteiras_csv(C.PASTA_CARTEIRAS)))

    vigentes = {
        v["ticker"]
        for v in vigencias
        if v["vigencia_inicio"] <= data and (v["vigencia_fim"] is None or v["vigencia_fim"] >= data)
    }

    assert data == D(2026, 10, 23)
    assert vigentes == set(api)


# -- API da B3 ---------------------------------------------------------------------------

def _corpo_api() -> dict:
    return json.loads((FIXTURES / "b3_carteira_ibov_2026.json").read_text(encoding="utf-8"))


def test_parse_da_fixture_real_da_api():
    data, carteira = B.parse_carteira(_corpo_api())

    assert data == D(2026, 10, 23)
    assert len(carteira) == 76
    assert carteira["ALOS3"] == 0.579
    assert all(peso is not None and peso > 0 for peso in carteira.values())


def test_parse_rejeita_carteira_incompleta():
    corpo = _corpo_api()
    corpo["page"]["totalRecords"] = 77

    with pytest.raises(ValueError, match="incompleta"):
        B.parse_carteira(corpo)


def test_data_do_header_aceita_ano_com_dois_ou_quatro_digitos():
    assert B._data_header("23/10/26") == D(2026, 10, 23)
    assert B._data_header("23/10/2026") == D(2026, 10, 23)
    with pytest.raises(ValueError):
        B._data_header("31/02/26")


# -- mudancas da API contra as vigencias abertas -----------------------------------------

def _aberta(ticker: str, inicio: D, part: float | None = 1.0, fonte: str = "paccafelipe_csv"):
    return {
        "ticker": ticker,
        "vigencia_inicio": inicio,
        "vigencia_fim": None,
        "participacao": part,
        "fonte": fonte,
    }


def test_mudancas_gravam_entrada_nova_e_fecham_a_saida():
    abertas = [_aberta("A", D(2024, 1, 2)), _aberta("B", D(2024, 1, 2))]

    linhas = B.mudancas_da_carteira(abertas, {"A": 0.7, "C": 0.2}, D(2026, 10, 23))

    assert _linhas(linhas) == [
        ("C", D(2026, 10, 23), None, 0.2, "b3_api"),
        ("B", D(2024, 1, 2), D(2026, 10, 22), 1.0, "paccafelipe_csv"),
    ]


def test_carteira_igual_nao_grava_nada():
    abertas = [_aberta("A", D(2024, 1, 2))]

    assert B.mudancas_da_carteira(abertas, {"A": 1.0}, D(2026, 10, 23)) == []


def test_papel_que_sai_no_dia_em_que_entrou_fica_de_fora():
    abertas = [_aberta("A", D(2026, 10, 23))]

    assert B.mudancas_da_carteira(abertas, {}, D(2026, 10, 23)) == []


def test_coletor_sem_vigencia_aberta_nao_grava(monkeypatch, caplog):
    colet = B.B3CarteiraIbovCollector()
    gravados: list = []
    monkeypatch.setattr(colet, "get_json", lambda *a, **k: _corpo_api())
    monkeypatch.setattr(B, "_vigencias_abertas", lambda: [])
    monkeypatch.setattr(colet, "_persist", lambda linhas: gravados.append(linhas))

    with caplog.at_level(logging.WARNING):
        resultado = colet.process(next(iter(colet.work_units())))

    assert resultado.items_found == 0
    assert gravados == []
    assert "rode build-entrega" in caplog.text
    colet.close()


def test_coletor_grava_so_a_diferenca(monkeypatch):
    colet = B.B3CarteiraIbovCollector()
    gravados: list = []
    corpo = _corpo_api()
    # A carteira da fixture tem ALOS3; a referencia abre so ALOS3 e um papel que nao esta.
    abertas = [_aberta("ALOS3", D(2024, 1, 2)), _aberta("XXXX3", D(2024, 1, 2))]
    monkeypatch.setattr(colet, "get_json", lambda *a, **k: corpo)
    monkeypatch.setattr(B, "_vigencias_abertas", lambda: abertas)
    monkeypatch.setattr(colet, "_persist", lambda linhas: gravados.append(linhas))

    resultado = colet.process(next(iter(colet.work_units())))

    # 75 entradas (a carteira tem 76 e ALOS3 ja estava aberta) e 1 saida (XXXX3).
    assert resultado.items_found == 76
    entradas = [linha for linha in gravados[0] if linha["fonte"] == "b3_api"]
    assert len(entradas) == 75
    assert all(linha["vigencia_inicio"] == D(2026, 10, 23) for linha in entradas)
    saida = [linha for linha in gravados[0] if linha["ticker"] == "XXXX3"]
    assert saida[0]["vigencia_fim"] == D(2026, 10, 22)
    colet.close()


# -- builder: fechamento gravado pelo coletor ---------------------------------------------

def test_montar_composicao_respeita_fechamento_gravado_pelo_coletor():
    from tcc_scraper.entrega.builder import montar_composicao

    linhas = montar_composicao({("ALPA4", D(2021, 9, 1)): D(2022, 1, 2)})

    alpa = [v for v in linhas if v["ticker"] == "ALPA4" and v["vigencia_inicio"] == D(2021, 9, 1)]
    assert len(alpa) == 1
    assert alpa[0]["vigencia_fim"] == D(2022, 1, 2)
