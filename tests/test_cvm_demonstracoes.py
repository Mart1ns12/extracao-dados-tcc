"""Parser e coletor de ITR/DFP da CVM, sobre as fixtures reais (sem rede e sem banco)."""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tcc_scraper.collectors import cvm_demonstracoes as demo
from tcc_scraper.collectors.base import WorkUnit
from tcc_scraper.collectors.cvm_demonstracoes import (
    CvmDemonstracoesCollector,
    parse_demonstracoes,
)
from tcc_scraper.config import TICKER_CODE_CVM, get_settings

FIXTURES = Path(__file__).parent / "fixtures"
UNIVERSO = set(TICKER_CODE_CVM.values())
PETROBRAS = 9512
ITAU = 19348


def _ler(nome: str) -> list[dict]:
    with open(FIXTURES / nome, encoding="latin-1", newline="") as arquivo:
        return list(csv.DictReader(arquivo, delimiter=";"))


def _csvs(prefixo: str) -> dict[str, list[dict]]:
    """prefixo e "itr_2025" ou "dfp_2024", o mesmo nome das fixtures cvm_<prefixo>_*_amostra.csv."""
    return {
        demo.CHAVE_PRINCIPAL: _ler(f"cvm_{prefixo}_principal_amostra.csv"),
        demo.CHAVE_DRE: _ler(f"cvm_{prefixo}_dre_con_amostra.csv"),
        demo.CHAVE_BPP: _ler(f"cvm_{prefixo}_bpp_con_amostra.csv"),
        demo.CHAVE_COMPOSICAO: _ler(f"cvm_{prefixo}_composicao_capital_amostra.csv"),
    }


def _linha_sintetica(**campos: str) -> dict:
    base = {
        "CNPJ_CIA": "33.000.167/0001-01",
        "CD_CVM": "009512",
        "DT_REFER": "2025-03-31",
        "VERSAO": "1",
        "DT_INI_EXERC": "2025-01-01",
        "DT_FIM_EXERC": "2025-03-31",
        "CD_CONTA": "3.11",
        "DS_CONTA": "Lucro",
        "ORDEM_EXERC": "ULTIMO",
        "VL_CONTA": "10.0",
        "ESCALA_MOEDA": "MIL",
    }
    base.update(campos)
    return base


def test_itr_traz_dre_e_bpp_das_contas_da_entrega() -> None:
    balancos, _ = parse_demonstracoes(_csvs("itr_2025"), "ITR", UNIVERSO)
    contas = {(r["cd_cvm"], r["demonstracao"], r["cd_conta"]) for r in balancos}
    assert {
        (PETROBRAS, "DRE", "3.01"),
        (PETROBRAS, "DRE", "3.03"),
        (PETROBRAS, "DRE", "3.11"),
        (PETROBRAS, "BPP", "2.03"),
    } <= contas


def test_itau_usa_plano_de_banco_codigos_nao_equivalem_aos_da_entrega() -> None:
    # Banco tem plano proprio. Na fixture: 3.03 e o resultado bruto da intermediacao
    # financeira; 2.03 sao passivos financeiros (nao patrimonio liquido); nao ha 3.11 (o
    # lucro esta em 3.09). O parser grava o codigo como veio; ver CONTAS_CVM em config.py.
    balancos, _ = parse_demonstracoes(_csvs("itr_2025"), "ITR", UNIVERSO)
    ds = {
        (r["demonstracao"], r["cd_conta"]): r["ds_conta"] for r in balancos if r["cd_cvm"] == ITAU
    }
    assert ds[("DRE", "3.03")].startswith("Resultado Bruto")
    assert ds[("BPP", "2.03")].startswith("Passivos Financeiros")
    assert ("DRE", "3.11") not in ds


def test_itr_3_11_mantem_trimestre_e_acumulado_separados() -> None:
    balancos, _ = parse_demonstracoes(_csvs("itr_2025"), "ITR", UNIVERSO)
    inicios = {
        r["dt_ini_exerc"]
        for r in balancos
        if r["cd_cvm"] == PETROBRAS
        and r["cd_conta"] == "3.11"
        and r["demonstracao"] == "DRE"
        and r["dt_refer"] == dt.date(2025, 6, 30)
    }
    assert inicios == {dt.date(2025, 1, 1), dt.date(2025, 4, 1)}


def test_bpp_tem_inicio_igual_ao_fim() -> None:
    balancos, _ = parse_demonstracoes(_csvs("itr_2025"), "ITR", UNIVERSO)
    bpp = [r for r in balancos if r["demonstracao"] == "BPP"]
    assert bpp
    assert all(r["dt_ini_exerc"] == r["dt_fim_exerc"] for r in bpp)


def test_companhia_fora_do_universo_nao_sai() -> None:
    balancos, acoes = parse_demonstracoes(_csvs("itr_2025"), "ITR", {PETROBRAS})
    assert balancos and acoes
    assert {r["cd_cvm"] for r in balancos + acoes} == {PETROBRAS}


def test_valor_sem_escala_e_escala_preservada() -> None:
    balancos, _ = parse_demonstracoes(_csvs("itr_2025"), "ITR", UNIVERSO)
    linha = next(
        r
        for r in balancos
        if r["cd_cvm"] == PETROBRAS
        and r["demonstracao"] == "DRE"
        and r["cd_conta"] == "3.11"
        and r["dt_refer"] == dt.date(2025, 3, 31)
        and r["dt_ini_exerc"] == dt.date(2025, 1, 1)
    )
    assert linha["escala_moeda"] == "MIL"
    # 35.331.000 em milhares na CVM; multiplicar pela escala daria 35.331.000.000.
    assert linha["vl_conta"] == 35331000.0
    assert linha["dt_receb"] == dt.date(2025, 5, 12)


def test_composicao_traz_acoes_inteiras_por_cnpj_do_principal() -> None:
    _, acoes = parse_demonstracoes(_csvs("itr_2025"), "ITR", UNIVERSO)
    linha = next(
        r
        for r in acoes
        if r["cd_cvm"] == PETROBRAS and r["dt_refer"] == dt.date(2025, 3, 31) and r["versao"] == 1
    )
    assert linha["qt_on"] == 7442231382
    assert linha["qt_pn"] == 5446501379
    assert linha["qt_total"] == 12888732761
    assert isinstance(linha["qt_on"], int)
    assert linha["dt_receb"] == dt.date(2025, 5, 12)


def test_dfp_2024_traz_penultimo_exercicio_sem_sair() -> None:
    balancos, acoes = parse_demonstracoes(_csvs("dfp_2024"), "DFP", UNIVERSO)
    lucro = [
        r
        for r in balancos
        if r["cd_cvm"] == PETROBRAS
        and r["cd_conta"] == "3.11"
        and r["dt_refer"] == dt.date(2024, 12, 31)
    ]
    assert len(lucro) == 1
    assert lucro[0]["vl_conta"] == 37009000.0
    assert lucro[0]["tipo_documento"] == "DFP"
    assert any(r["cd_cvm"] == PETROBRAS for r in acoes)


def test_ordem_ultimo_aceita_com_e_sem_acento() -> None:
    csvs = {
        demo.CHAVE_PRINCIPAL: [],
        demo.CHAVE_DRE: [
            _linha_sintetica(ORDEM_EXERC="ÚLTIMO", VL_CONTA="10.0"),
            _linha_sintetica(
                ORDEM_EXERC="PENÚLTIMO",
                DT_INI_EXERC="2024-01-01",
                DT_FIM_EXERC="2024-03-31",
                DT_REFER="2024-03-31",
                VL_CONTA="99.0",
            ),
        ],
        demo.CHAVE_BPP: [],
        demo.CHAVE_COMPOSICAO: [],
    }
    balancos, _ = parse_demonstracoes(csvs, "ITR", UNIVERSO)
    assert [r["vl_conta"] for r in balancos] == [10.0]

    csvs[demo.CHAVE_DRE] = [_linha_sintetica(ORDEM_EXERC="ULTIMO", VL_CONTA="11.0")]
    balancos, _ = parse_demonstracoes(csvs, "ITR", UNIVERSO)
    assert [r["vl_conta"] for r in balancos] == [11.0]


def test_linha_repetida_na_mesma_chave_vale_a_ultima() -> None:
    csvs = {
        demo.CHAVE_PRINCIPAL: [],
        demo.CHAVE_DRE: [
            _linha_sintetica(VL_CONTA="10.0"),
            _linha_sintetica(VL_CONTA="20.0"),
        ],
        demo.CHAVE_BPP: [],
        demo.CHAVE_COMPOSICAO: [],
    }
    balancos, _ = parse_demonstracoes(csvs, "ITR", UNIVERSO)
    assert len(balancos) == 1
    assert balancos[0]["vl_conta"] == 20.0


def test_work_units_gera_itr_e_dfp_com_data_de_hoje_sem_last_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo, "last_modified", lambda _coletor, _url: "")
    with CvmDemonstracoesCollector(anos=[2025]) as coletor:
        unidades = list(coletor.work_units())

    hoje = dt.datetime.now(ZoneInfo(get_settings().daily_tz)).date().isoformat()
    chaves = [u.key for u in unidades]
    assert chaves == [f"ITR|2025|{hoje}", f"DFP|2025|{hoje}"]
    assert all(len(chave.split("|")) == 3 for chave in chaves)
    assert unidades[0].payload["url"].endswith("/DOC/ITR/DADOS/itr_cia_aberta_2025.zip")


def test_dfp_2024_traz_ebit_dividas_pai_e_arrendamento() -> None:
    balancos, _ = parse_demonstracoes(_csvs("dfp_2024"), "DFP", UNIVERSO)
    valores = {
        (r["demonstracao"], r["cd_conta"]): r["vl_conta"]
        for r in balancos
        if r["cd_cvm"] == PETROBRAS and r["dt_refer"] == dt.date(2024, 12, 31)
    }
    assert valores[("DRE", "3.05")] == 137201000.0
    assert valores[("BPP", "2.01.04.01")] == 15887000.0
    assert valores[("BPP", "2.02.01.01")] == 127539000.0
    assert valores[("BPP", "2.01.04.02")] == 0.0
    # Conta-pai e arrendamento vao ao raw (o builder decide o uso); subconta fora da lista nao.
    assert valores[("BPP", "2.01.04")] == 68783000.0
    assert valores[("BPP", "2.02.01")] == 304684000.0
    assert valores[("BPP", "2.01.04.03")] == 52896000.0
    assert valores[("BPP", "2.02.01.03")] == 177145000.0
    assert ("BPP", "2.01.04.01.01") not in valores

    itau = [
        r
        for r in balancos
        if r["cd_cvm"] == ITAU and r["demonstracao"] == "DRE" and r["cd_conta"] == "3.05"
    ]
    assert itau
    assert itau[0]["ds_conta"].startswith("Resultado Antes dos Tributos")


def test_bpa_grava_caixa_e_aplicacoes_com_inicio_igual_ao_fim() -> None:
    periodo = {"DT_INI_EXERC": "2025-01-01", "DT_FIM_EXERC": "2025-06-30", "DT_REFER": "2025-06-30"}
    bpa = [
        _linha_sintetica(**periodo, CD_CONTA="1.01", DS_CONTA="Ativo Circulante", VL_CONTA="1.0"),
        _linha_sintetica(
            **periodo,
            CD_CONTA="1.01.01",
            DS_CONTA="Caixa e Equivalentes de Caixa",
            VL_CONTA="38177000.0",
        ),
        _linha_sintetica(
            **periodo, CD_CONTA="1.01.02", DS_CONTA="Aplicacoes Financeiras", VL_CONTA="13670000.0"
        ),
        _linha_sintetica(
            **periodo, CD_CONTA="1.01.03", DS_CONTA="Contas a Receber", VL_CONTA="2.0"
        ),
    ]
    csvs = {
        demo.CHAVE_PRINCIPAL: [],
        demo.CHAVE_DRE: [],
        demo.CHAVE_BPA: bpa,
        demo.CHAVE_BPP: [],
        demo.CHAVE_COMPOSICAO: [],
    }
    balancos, _ = parse_demonstracoes(csvs, "ITR", UNIVERSO)
    assert {r["cd_conta"]: r["vl_conta"] for r in balancos} == {
        "1.01.01": 38177000.0,
        "1.01.02": 13670000.0,
    }
    assert {r["demonstracao"] for r in balancos} == {"BPA"}
    assert all(r["dt_ini_exerc"] == r["dt_fim_exerc"] == dt.date(2025, 6, 30) for r in balancos)


def test_dfc_guarda_so_ajustes_6_01_01_nn_com_periodo_acumulado() -> None:
    periodo = {"DT_INI_EXERC": "2025-01-01", "DT_FIM_EXERC": "2025-06-30", "DT_REFER": "2025-06-30"}
    dfc = [
        _linha_sintetica(**periodo, CD_CONTA="6.01", DS_CONTA="Caixa Gerado", VL_CONTA="90.0"),
        _linha_sintetica(**periodo, CD_CONTA="6.01.01", DS_CONTA="Ajustes", VL_CONTA="50.0"),
        _linha_sintetica(
            **periodo,
            CD_CONTA="6.01.01.04",
            DS_CONTA="Depreciação, depleção e amortização",
            VL_CONTA="39928000.0",
        ),
        _linha_sintetica(
            **periodo,
            CD_CONTA="6.01.01.04.01",
            DS_CONTA="Depreciacao de imobilizado",
            VL_CONTA="1.0",
        ),
        _linha_sintetica(
            **periodo, CD_CONTA="6.01.02.01", DS_CONTA="Contas a receber", VL_CONTA="2.0"
        ),
    ]
    csvs = {
        demo.CHAVE_PRINCIPAL: [],
        demo.CHAVE_DRE: [],
        demo.CHAVE_BPA: [],
        demo.CHAVE_BPP: [],
        demo.CHAVE_DFC: dfc,
        demo.CHAVE_COMPOSICAO: [],
    }
    balancos, _ = parse_demonstracoes(csvs, "ITR", UNIVERSO)
    assert [(r["demonstracao"], r["cd_conta"], r["dt_ini_exerc"]) for r in balancos] == [
        ("DFC", "6.01.01.04", dt.date(2025, 1, 1))
    ]
    assert balancos[0]["vl_conta"] == 39928000.0


def _zip_cvm(arquivos: dict[str, str]) -> bytes:
    """Zip em memoria; cada CSV e gravado em latin-1, como a CVM publica."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for nome, texto in arquivos.items():
            zf.writestr(nome, texto.encode("latin-1"))
    return buffer.getvalue()


def test_process_le_bpa_e_dfc_mi_ignora_dfc_md_e_fora_do_universo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cabecalho = (
        "CNPJ_CIA;DT_REFER;VERSAO;CD_CVM;DT_INI_EXERC;DT_FIM_EXERC;CD_CONTA;DS_CONTA;"
        "ORDEM_EXERC;VL_CONTA;ESCALA_MOEDA\n"
    )
    cnpj = "33.000.167/0001-01"
    zip_cvm = _zip_cvm({
        "itr_cia_aberta_2025.csv": (
            "CNPJ_CIA;DT_REFER;VERSAO;CD_CVM;DT_RECEB\n"
            f"{cnpj};2025-06-30;1;009512;2025-08-12\n"
        ),
        "itr_cia_aberta_BPA_con_2025.csv": cabecalho + (
            f"{cnpj};2025-06-30;1;009512;2025-01-01;2025-06-30;1.01.01;"
            "Caixa e Equivalentes de Caixa;ULTIMO;38177000.0;MIL\n"
            f"{cnpj};2025-06-30;1;009512;2025-01-01;2025-06-30;1.01.02;"
            "Aplicacoes Financeiras;ULTIMO;13670000.0;MIL\n"
            "33.999.999/0001-99;2025-06-30;1;999999;2025-01-01;2025-06-30;1.01.01;"
            "Caixa;ULTIMO;1.0;MIL\n"
        ),
        "itr_cia_aberta_DFC_MI_con_2025.csv": cabecalho + (
            f"{cnpj};2025-06-30;1;009512;2025-01-01;2025-06-30;6.01.01.04;"
            "Depreciação, depleção e amortização;ULTIMO;39928000.0;MIL\n"
            "33.999.999/0001-99;2025-06-30;1;999999;2025-01-01;2025-06-30;6.01.01.04;"
            "Depreciacao;ULTIMO;5.0;MIL\n"
        ),
        "itr_cia_aberta_DFC_MD_con_2025.csv": cabecalho + (
            f"{cnpj};2025-06-30;1;009512;2025-01-01;2025-06-30;6.01.01.04;"
            "Depreciação, depleção e amortização;ULTIMO;777.0;MIL\n"
        ),
    })
    gravados: list[list[dict]] = []
    with CvmDemonstracoesCollector(anos=[2025]) as coletor:
        monkeypatch.setattr(coletor, "baixar_zip", lambda url: zip_cvm)
        monkeypatch.setattr(coletor, "_persist", lambda balancos, acoes: gravados.append(balancos))
        resultado = coletor.process(
            WorkUnit(
                key="ITR|2025|teste",
                payload={"tipo": "ITR", "ano": 2025, "url": "https://exemplo/itr_2025.zip"},
            )
        )

    balancos = gravados[0]
    assert {(r["demonstracao"], r["cd_conta"]) for r in balancos} == {
        ("BPA", "1.01.01"),
        ("BPA", "1.01.02"),
        ("DFC", "6.01.01.04"),
    }
    assert {r["cd_cvm"] for r in balancos} == {PETROBRAS}
    assert 777.0 not in {r["vl_conta"] for r in balancos}
    assert resultado.items_found == 3
