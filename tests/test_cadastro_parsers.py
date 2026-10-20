"""Parsers do cadastro CVM e B3 (T16), com fixtures reais e sem rede nem banco."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from tcc_scraper.collectors.b3_cadastro import (
    B3CadastroCollector,
    classificacao_b3,
    parse_get_detail,
)
from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.collectors.cvm_cadastro import parse_cad_cia_aberta
from tcc_scraper.config import TICKER_CODE_CVM

FIXTURES = Path(__file__).parent / "fixtures"


def _b3(nome: str) -> dict:
    return json.loads((FIXTURES / nome).read_text(encoding="utf-8"))


def _cvm_linhas() -> list[dict]:
    texto = (FIXTURES / "cvm_cad_cia_aberta_amostra.csv").read_bytes().decode("latin-1")
    return list(csv.DictReader(io.StringIO(texto), delimiter=";"))


def test_parse_get_detail_extrai_cnpj_e_codigos_sem_repeticao() -> None:
    reg = parse_get_detail(_b3("b3_getdetail_9512.json"), 9512)
    assert reg["cnpj"] == "33000167000101"
    assert reg["cd_cvm"] == 9512
    assert "PETR4" in reg["other_codes"]
    assert "PETR3" in reg["other_codes"]
    # PETR4 aparece como code e em otherCodes: deve constar uma vez só.
    assert reg["other_codes"].count("PETR4") == 1


def test_classificacao_b3_separa_em_tres_e_troca_ponto_por_virgula() -> None:
    texto = _b3("b3_getdetail_9512.json")["industryClassification"]
    assert classificacao_b3(texto) == (
        "Petróleo, Gás e Biocombustíveis",
        "Petróleo, Gás e Biocombustíveis",
        "Exploração, Refino e Distribuição",
    )


def test_classificacao_b3_vazia_devolve_none() -> None:
    assert classificacao_b3(None) == (None, None, None)


def test_parse_cad_cia_aberta_filtra_pelo_universo_e_normaliza() -> None:
    regs = parse_cad_cia_aberta(_cvm_linhas(), {9512, 12653})
    por_cvm = {r["cd_cvm"]: r for r in regs}
    # PETR aparece duas vezes no CSV: uma linha por CNPJ.
    assert len(regs) == 2
    assert set(por_cvm) == {9512, 12653}
    assert all(isinstance(r["cd_cvm"], int) for r in regs)
    assert por_cvm[9512]["cnpj"] == "33000167000101"
    assert por_cvm[12653]["cnpj"] == "89637490000145"
    assert "BRASILEIRO" in por_cvm[9512]["denom_social"]


def test_parse_cad_cia_aberta_codigo_com_zeros_a_esquerda_vira_int() -> None:
    linhas = [{"CNPJ_CIA": "33.000.167/0001-01", "CD_CVM": "09512", "DENOM_SOCIAL": "X"}]
    regs = parse_cad_cia_aberta(linhas, {9512})
    assert [r["cd_cvm"] for r in regs] == [9512]


def test_b3_work_units_gera_uma_chave_por_codigo() -> None:
    with B3CadastroCollector(codigos=[9512, 12653]) as col:
        unidades = list(col.work_units())
    assert [u.key for u in unidades] == ["detail|9512", "detail|12653"]


def test_b3_codigos_padrao_vem_do_universo_do_config() -> None:
    with B3CadastroCollector() as col:
        assert col.codigos == sorted(set(TICKER_CODE_CVM.values()))


def test_b3_process_sem_detalhe_devolve_zero_sem_levantar(monkeypatch) -> None:
    with B3CadastroCollector(codigos=[9512]) as col:
        monkeypatch.setattr(col, "get_json", lambda metodo, params: None)
        resultado = col.process(WorkUnit(key="detail|9512", payload={"cd_cvm": 9512}))
    assert resultado == UnitResult(items_found=0)


def test_cvm_cadastro_baixa_o_csv_com_o_timeout_longo_dos_zips(monkeypatch) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from tcc_scraper.collectors import cvm_cadastro
    from tcc_scraper.collectors.cvm_base import TIMEOUT_ZIP

    chamadas: list[dict] = []

    def falso_fetch(url, **kwargs):
        chamadas.append(kwargs)
        return SimpleNamespace(content=b"")

    monkeypatch.setattr(cvm_cadastro, "session_scope", nullcontext)
    monkeypatch.setattr(cvm_cadastro, "bulk_upsert", lambda *a, **k: None)
    with cvm_cadastro.CvmCadastroCollector() as col:
        monkeypatch.setattr(col, "fetch", falso_fetch)
        col.process(WorkUnit(key="cad|x", payload={"url": cvm_cadastro.URL_CAD_CIA_ABERTA}))
    assert chamadas == [{"timeout": TIMEOUT_ZIP}]
