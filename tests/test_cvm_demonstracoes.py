"""Parser e coletor de ITR/DFP da CVM, sobre as fixtures reais (sem rede e sem banco)."""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tcc_scraper.collectors import cvm_demonstracoes as demo
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
