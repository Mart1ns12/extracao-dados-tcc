"""Testes da COTAHIST da B3 (correcao de precos do AZUL3). Sem rede: a linha e montada aqui.

O layout de 245 colunas segue o oficial; o helper confere o tamanho de cada campo, para o
teste nao depender de um arquivo real (que nao e versionado).
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from decimal import Decimal

import pytest

from tcc_scraper.collectors.b3_cotahist import (
    carregar_correcoes,
    grupamentos_do_suplemento,
    ler_cotahist,
    parse_linha,
    serie_corrigida,
)

ISIN_AZUL = "BRAZULA01OR8"
GRUPAMENTOS = [
    (dt.date(2026, 2, 18), Decimal("0.01333333334")),
    (dt.date(2026, 4, 17), Decimal("0.00000666670")),
]


def _linha(
    *,
    tipreg: str = "01",
    data: str = "20260113",
    codneg: str = "AZUL53",
    tpmerc: str = "010",
    preabe: int = 9899,
    premax: int = 11690,
    premin: int = 4050,
    preult: int = 4125,
    quatot: int = 273490000000,
    voltot: int = 1648449569,
    fatcot: int = 1000000,
    codisi: str = ISIN_AZUL,
    nomres: str = "AÇÃO AZUL",
) -> str:
    """Registro TIPREG 01 com as posicoes 1-indexadas do layout oficial da COTAHIST."""
    campos = [
        (1, 2, tipreg),
        (3, 10, data),
        (11, 12, "02"),
        (13, 24, codneg.ljust(12)),
        (25, 27, tpmerc),
        (28, 39, nomres.ljust(12)[:12]),
        (40, 49, "ON".ljust(10)),
        (50, 52, "000"),
        (53, 56, "R$  "),
        (57, 69, f"{preabe:013d}"),
        (70, 82, f"{premax:013d}"),
        (83, 95, f"{premin:013d}"),
        (96, 108, f"{preult:013d}"),
        (109, 121, f"{preult:013d}"),
        (122, 134, f"{0:013d}"),
        (135, 147, f"{0:013d}"),
        (148, 152, f"{0:05d}"),
        (153, 170, f"{quatot:018d}"),
        (171, 188, f"{voltot:018d}"),
        (189, 201, f"{0:013d}"),
        (202, 202, "0"),
        (203, 210, "00000000"),
        (211, 217, f"{fatcot:07d}"),
        (218, 230, f"{0:013d}"),
        (231, 242, codisi.ljust(12)),
        (243, 245, "010"),
    ]
    saida = [" "] * 245
    for inicio, fim, texto in campos:
        assert len(texto) == fim - inicio + 1, (inicio, fim, texto)
        saida[inicio - 1 : fim] = list(texto)
    return "".join(saida)


def _zip_com(*linhas: str) -> bytes:
    """ZIP em memoria com um TXT latin-1, como o COTAHIST_A{ano}.ZIP da B3."""
    texto = "\r\n".join(linhas) + "\r\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("COTAHIST_A2026.TXT", texto.encode("latin-1"))
    return buffer.getvalue()


# -- parse_linha ----------------------------------------------------------------------
def test_parse_linha_le_campos_nas_posicoes_do_layout() -> None:
    reg = parse_linha(_linha())

    assert reg is not None
    assert reg["data"] == dt.date(2026, 1, 13)
    assert reg["codneg"] == "AZUL53"
    assert reg["tpmerc"] == "010"
    assert reg["preult"] == Decimal("41.25")
    assert reg["preabe"] == Decimal("98.99")
    assert reg["quatot"] == 273490000000
    assert reg["voltot"] == Decimal("16484495.69")
    assert reg["fatcot"] == 1000000
    assert reg["codisi"] == ISIN_AZUL


def test_parse_linha_ignora_outro_tipo_de_registro() -> None:
    assert parse_linha(_linha(tipreg="00")) is None
    assert parse_linha(_linha(tipreg="99")) is None


# -- ler_cotahist ---------------------------------------------------------------------
def test_ler_cotahist_filtra_mercado_a_vista_e_codigo() -> None:
    zip_bytes = _zip_com(
        _linha(tipreg="00"),
        _linha(codneg="AZUL53", tpmerc="010"),  # entra
        _linha(codneg="AZUL53", tpmerc="020", data="20260114"),  # fracionario: sai
        _linha(codneg="AZUL3", tpmerc="010", data="20260115"),  # outro codigo: sai
        _linha(tipreg="99"),
    )

    registros = ler_cotahist(zip_bytes, {"AZUL53"})

    assert [(r["codneg"], r["tpmerc"], r["data"]) for r in registros] == [
        ("AZUL53", "010", dt.date(2026, 1, 13))
    ]


def test_ler_cotahist_le_latin1_no_nome_resumido() -> None:
    """O TXT e latin-1: o nome com cedilha e acento nao pode quebrar a leitura."""
    registros = ler_cotahist(_zip_com(_linha(nomres="AÇÃO AZUL")), {"AZUL53"})
    assert len(registros) == 1


def test_ler_cotahist_zip_sem_txt_levanta_erro() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("LEIAME.md", "nada")
    with pytest.raises(ValueError, match="sem arquivo .TXT"):
        ler_cotahist(buffer.getvalue(), {"AZUL53"})


# -- grupamentos_do_suplemento --------------------------------------------------------
def test_grupamentos_pega_so_grupamento_do_isin_em_ordem_de_data() -> None:
    payloads = [
        {"label": "GRUPAMENTO", "factor": "0,00000666670", "lastDatePrior": "17/04/2026",
         "isinCode": ISIN_AZUL},
        {"label": "GRUPAMENTO", "factor": "0,01333333334", "lastDatePrior": "18/02/2026",
         "isinCode": ISIN_AZUL},
        {"label": "DESDOBRAMENTO", "factor": "100,00000000000", "lastDatePrior": "01/03/2020",
         "isinCode": ISIN_AZUL},
        {"label": "GRUPAMENTO", "factor": "0,5", "lastDatePrior": "01/03/2020",
         "isinCode": "BRPETRACNPR6"},
    ]

    assert grupamentos_do_suplemento(payloads, ISIN_AZUL) == GRUPAMENTOS


def test_grupamentos_fator_invalido_levanta_erro() -> None:
    payloads = [{"label": "GRUPAMENTO", "factor": "0,0", "lastDatePrior": "18/02/2026",
                 "isinCode": ISIN_AZUL}]
    with pytest.raises(ValueError, match="grupamento invalido"):
        grupamentos_do_suplemento(payloads, ISIN_AZUL)


# -- serie_corrigida ------------------------------------------------------------------
def _registros(*linhas: str) -> list[dict]:
    return [parse_linha(linha) for linha in linhas]


def test_serie_corrigida_aplica_os_dois_grupamentos_na_janela_certa() -> None:
    """Pregao em 18/02 entra no grupamento de 18/02 (limite inclusivo); 19/02 nao."""
    registros = _registros(
        _linha(data="20260113", preult=4125, quatot=273490000000),
        _linha(data="20260218", preult=340),
        _linha(data="20260219", preult=340),
        _linha(data="20260417", preult=21500),
    )

    linhas = {r.date: r for r in serie_corrigida(registros, "AZUL3", GRUPAMENTOS)}

    # Mesma conta da fonte: 41,25 / 1.000.000 / (0,01333333334 x 0,00000666670).
    fator_total = Decimal("0.01333333334") * Decimal("0.00000666670")
    esperado_0113 = float(Decimal("41.25") / Decimal(1000000) / fator_total)
    assert linhas[dt.date(2026, 1, 13)].close == pytest.approx(esperado_0113, rel=1e-12)
    # Conferido com o nominal (75 x 150.000 = 11.250.000): diferenca de 5e-6 vem do fator
    # publicado pela B3 (0,00000666670, arredondado).
    assert linhas[dt.date(2026, 1, 13)].close == pytest.approx(464.0625, rel=1e-4)
    assert linhas[dt.date(2026, 2, 18)].close == pytest.approx(38.25, rel=1e-4)
    # 19/02 so tem o grupamento de 150.000: 3,40 / 1.000.000 x 150.000 = 0,51.
    assert linhas[dt.date(2026, 2, 19)].close == pytest.approx(0.51, rel=1e-4)
    assert linhas[dt.date(2026, 4, 17)].close == pytest.approx(32.25, rel=1e-4)


def test_serie_corrigida_ajusta_ohlc_e_adj_close_igual_ao_close() -> None:
    linha = serie_corrigida(_registros(_linha()), "AZUL3", GRUPAMENTOS)[0]

    assert linha.ticker == "AZUL3"
    assert linha.source == "b3_cotahist"
    assert linha.adj_close == linha.close
    # Abertura, maxima e minima seguem a mesma escala do fechamento.
    assert linha.open == pytest.approx(linha.close * 98.99 / 41.25, rel=1e-9)
    assert linha.high == pytest.approx(linha.close * 116.90 / 41.25, rel=1e-9)
    assert linha.low == pytest.approx(linha.close * 40.50 / 41.25, rel=1e-9)


def test_serie_corrigida_volume_e_quatot_vezes_fator_arredondado() -> None:
    """Volume 273.490.000.000 x 1/11.250.000 = 24.310,2 -> 24.310 (valor do plano)."""
    linha = serie_corrigida(_registros(_linha()), "AZUL3", GRUPAMENTOS)[0]
    assert linha.volume == 24310


def test_serie_corrigida_sem_grupamento_nao_escala_mas_divide_por_fatcot() -> None:
    linha = serie_corrigida(_registros(_linha(preult=4125)), "AZUL3", [])[0]
    assert linha.close == pytest.approx(0.00004125, rel=1e-12)


def test_serie_corrigida_duas_linhas_na_mesma_data_levanta_erro() -> None:
    registros = _registros(_linha(codneg="AZUL53"), _linha(codneg="AZUL54"))
    with pytest.raises(ValueError, match="mais de uma linha"):
        serie_corrigida(registros, "AZUL3", GRUPAMENTOS)


# -- carregar_correcoes ---------------------------------------------------------------
CSV_AZUL = (
    "ticker,date,open,high,low,close,adj_close,volume,codneg,fatcot,fator_grupamento\n"
    "AZUL3,2026-01-13,1113.7,1316.2,456.6,464.06,464.06,24310,AZUL53,1000000,8.8889E-8\n"
    "AZUL3,2026-04-17,33.4,33.9,31.6,32.25,32.25,20007,AZUL53,1000000,0.0000066667\n"
)


def test_carregar_correcoes_le_csv_da_pasta(tmp_path) -> None:
    (tmp_path / "AZUL3.csv").write_text(CSV_AZUL, encoding="utf-8")

    correcoes = carregar_correcoes(tmp_path)

    assert list(correcoes) == ["AZUL3"]
    primeira, segunda = correcoes["AZUL3"]
    assert primeira.date == dt.date(2026, 1, 13)
    assert primeira.close == pytest.approx(464.06)
    assert primeira.volume == 24310
    assert primeira.source == "b3_cotahist"
    assert segunda.date == dt.date(2026, 4, 17)


def test_carregar_correcoes_pasta_ausente_devolve_vazio(tmp_path) -> None:
    assert carregar_correcoes(tmp_path / "nao_existe") == {}
