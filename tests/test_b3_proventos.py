"""Coletor de proventos da B3 contra as fixtures reais. Sem rede e sem banco."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tcc_scraper.collectors.b3_proventos import (
    B3ProventosCollector,
    _nomes_candidatos,
    empresas_de_periodos,
    parse_cash_dividends,
    parse_suplemento,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _carregar(nome: str):
    return json.loads((FIXTURES / nome).read_text(encoding="utf-8"))


@pytest.fixture
def coletor() -> B3ProventosCollector:
    return B3ProventosCollector(empresas=[("PETR", "PETROBRAS")])


@pytest.mark.parametrize(
    "arquivo", ["b3_cashdividends_PETROBRAS_p1.json", "b3_cashdividends_KLABIN_p1.json",
                "b3_cashdividends_USIMINAS_p1.json"],
)
def test_um_registro_parseado_por_resultado(arquivo: str) -> None:
    pagina = _carregar(arquivo)
    regs = parse_cash_dividends([pagina], "XXXX", "NOME")
    assert len(regs) == len(pagina["results"]) == 120


def test_value_cash_com_virgula_vira_float() -> None:
    pagina = _carregar("b3_cashdividends_PETROBRAS_p1.json")
    regs = parse_cash_dividends([pagina], "PETR", "PETROBRAS")
    assert pagina["results"][0]["valueCash"] == "0,67407131"
    assert isinstance(regs[0]["value_cash"], float)
    assert regs[0]["value_cash"] == pytest.approx(0.67407131)


def test_registro_hash_estavel_entre_chamadas_e_distinto_entre_registros() -> None:
    pagina = _carregar("b3_cashdividends_USIMINAS_p1.json")
    primeira = parse_cash_dividends([pagina], "USIM", "USIMINAS")
    segunda = parse_cash_dividends([pagina], "USIM", "USIMINAS")
    hashes = [r["registro_hash"] for r in primeira]
    assert hashes == [r["registro_hash"] for r in segunda]
    assert len(set(hashes)) == len(hashes)


def test_registro_hash_inclui_a_empresa_e_nao_colide_entre_empresas() -> None:
    pagina = _carregar("b3_cashdividends_USIMINAS_p1.json")
    da_usim = parse_cash_dividends([pagina], "USIM", "USIMINAS")
    da_outra = parse_cash_dividends([pagina], "XPTO", "OUTRA")
    assert {r["registro_hash"] for r in da_usim}.isdisjoint(
        {r["registro_hash"] for r in da_outra}
    )
    sup_a = parse_suplemento({"cashDividends": [{"valor": 1}]}, "USIM")
    sup_b = parse_suplemento({"cashDividends": [{"valor": 1}]}, "XPTO")
    assert sup_a[0]["registro_hash"] != sup_b[0]["registro_hash"]


def test_tipos_e_acoes_observados_nas_fixtures() -> None:
    # Spike S1: valores reais. UNIT nao aparece em nenhuma fixture de proventos.
    tipos: set[str] = set()
    acoes: set[str] = set()
    for arquivo in ("b3_cashdividends_PETROBRAS_p1.json", "b3_cashdividends_KLABIN_p1.json",
                    "b3_cashdividends_USIMINAS_p1.json"):
        for r in _carregar(arquivo)["results"]:
            tipos.add(r["typeStock"])
            acoes.add(r["corporateAction"])
    assert {"ON", "PN", "PNA", "PNB"} <= tipos
    assert {"DIVIDENDO", "JRS CAP PROPRIO", "RENDIMENTO"} <= acoes
    usiminas = {r["typeStock"] for r in _carregar("b3_cashdividends_USIMINAS_p1.json")["results"]}
    assert {"PNA", "PNB"} <= usiminas


def test_parse_suplemento_separa_cash_de_stock() -> None:
    supl = _carregar("b3_supplement_PETR.json")
    linhas = parse_suplemento(supl, "PETR")
    cash = [r for r in linhas if r["tipo"] == "cash"]
    stock = [r for r in linhas if r["tipo"] == "stock"]
    assert len(cash) == len(supl[0]["cashDividends"]) == 24
    assert len(stock) == len(supl[0]["stockDividends"]) == 6
    assert len(linhas) == 30  # subscriptions ficam de fora
    assert all("paymentDate" in r["payload"] for r in cash)
    assert all("factor" in r["payload"] for r in stock)
    assert any(r["payload"]["label"] == "DESDOBRAMENTO" for r in stock)


def test_parse_suplemento_aceita_objeto_ou_lista() -> None:
    supl = _carregar("b3_supplement_KLBN.json")

    def conteudo(linhas: list[dict]) -> list[tuple]:
        # fetched_at varia entre chamadas; compara so o que veio da B3.
        return [(r["registro_hash"], r["tipo"], r["payload"]) for r in linhas]

    assert conteudo(parse_suplemento(supl, "KLBN")) == conteudo(parse_suplemento(supl[0], "KLBN"))


def test_parse_suplemento_none_devolve_lista_vazia() -> None:
    assert parse_suplemento(None, "PETR") == []


def test_paginar_para_quando_a_pagina_vem_curta(
    coletor: B3ProventosCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    chamadas: list[dict] = []
    respostas = [
        {"results": [{"valueCash": str(i)} for i in range(120)]},
        {"results": [{"valueCash": str(i)} for i in range(5)]},
    ]

    def fake_get_json(metodo, params):
        chamadas.append(params)
        return respostas[len(chamadas) - 1]

    monkeypatch.setattr(coletor, "get_json", fake_get_json)
    paginas = coletor._paginar("PETROBRAS")
    assert len(chamadas) == 2
    assert [c["pageNumber"] for c in chamadas] == [1, 2]
    assert len(paginas) == 2


def test_paginar_none_na_primeira_pagina_devolve_lista_vazia(
    coletor: B3ProventosCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coletor, "get_json", lambda metodo, params: None)
    assert coletor._paginar("PETROBRAS") == []


def test_buscar_proventos_cai_para_companyname_quando_trading_name_vazio(
    coletor: B3ProventosCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    pagina = _carregar("b3_cashdividends_KLABIN_p1.json")

    def fake_paginar(self, nome):
        return [pagina] if nome == "KLABIN S.A." else [{"results": []}]

    monkeypatch.setattr(B3ProventosCollector, "_paginar", fake_paginar)
    nome, paginas = coletor._buscar_proventos(["KLABIN S/A", "KLABIN S.A."])
    assert nome == "KLABIN S.A."
    assert paginas == [pagina]


def test_nomes_candidatos_sem_vazios_nem_repetidos() -> None:
    payload = {"trading_name": "KLABIN S/A", "company_name": "KLABIN S.A."}
    assert _nomes_candidatos(payload) == ["KLABIN S/A", "KLABIN S.A."]
    assert _nomes_candidatos({"trading_name": "  ", "company_name": None}) == []
    assert _nomes_candidatos({"trading_name": "X", "company_name": "X"}) == ["X"]


def test_nomes_candidatos_inclui_nomes_do_periodo_sem_repetir() -> None:
    payload = {
        "trading_name": "JBS",
        "company_name": "JBS S.A.",
        "nomes": ["JBS", "JBS S.A.", "JBS SA"],
    }
    assert _nomes_candidatos(payload) == ["JBS", "JBS S.A.", "JBS SA"]


def test_empresas_de_periodos_so_emissores_diferentes_do_codigo_atual() -> None:
    empresas = empresas_de_periodos()
    assert [(e["issuing_company"], e["cd_cvm"]) for e in empresas] == [
        ("JBSS", 20575),
        ("NTCO", 24783),
    ]
    jbs = empresas[0]
    assert jbs == {
        "issuing_company": "JBSS",
        "trading_name": "JBS",
        "company_name": None,
        "nomes": ["JBS", "JBS S.A.", "JBS SA"],
        "cd_cvm": 20575,
    }


def test_work_units_sem_empresas_inclui_emissores_de_periodo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universo = [{"issuing_company": "JBSS", "trading_name": "JBS", "company_name": None}]
    monkeypatch.setattr(B3ProventosCollector, "_ler_universo", lambda self: universo)
    chaves = [u.key for u in B3ProventosCollector().work_units()]
    assert chaves == ["prov|JBSS", "prov|JBSS|20575", "prov|NTCO|24783"]


def test_work_units_com_empresas_explicitas_nao_inclui_periodos() -> None:
    chaves = [u.key for u in B3ProventosCollector(empresas=[("PETR", "PETROBRAS")]).work_units()]
    assert chaves == ["prov|PETR"]
