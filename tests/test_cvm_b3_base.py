"""Helpers de CVM e B3 e os coletores-base, sem rede e sem banco."""

from __future__ import annotations

import base64
import io
import zipfile
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from tcc_scraper.collectors.b3_base import (
    B3_BASE,
    B3_DOMINIO,
    USER_AGENT_B3,
    B3Collector,
    data_b3,
    decimal_b3,
    registro_hash,
    url_b3,
)
from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.collectors.cvm_base import (
    CVM_DOMINIO,
    CvmCollector,
    cnpj_digitos,
    data_cvm,
    decimal_cvm,
    last_modified,
    ler_csvs_do_zip,
)
from tcc_scraper.config import get_settings


def _zip_com(arquivos: dict[str, str]) -> bytes:
    """Zip em memória; cada CSV é gravado em latin-1, como a CVM publica."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for nome, texto in arquivos.items():
            zf.writestr(nome, texto.encode("latin-1"))
    return buffer.getvalue()


def test_ler_csvs_do_zip_devolve_as_duas_listas_com_acentos() -> None:
    conteudo = _zip_com(
        {
            "dfp_cia_aberta_BPP_con_2024.csv": (
                "CNPJ_CIA;DS_CONTA;VL_CONTA\n"
                "33000167000101;Ativo Não Circulante;1234.5\n"
            ),
            "dfp_cia_aberta_DRE_con_2024.csv": (
                "CNPJ_CIA;DS_CONTA;VL_CONTA\n"
                "33000167000101;Receita de Vendação;-10\n"
            ),
        }
    )

    resultado = ler_csvs_do_zip(conteudo, ["BPP_con", "DRE_con"])

    assert set(resultado) == {"BPP_con", "DRE_con"}
    assert resultado["BPP_con"] == [
        {"CNPJ_CIA": "33000167000101", "DS_CONTA": "Ativo Não Circulante", "VL_CONTA": "1234.5"}
    ]
    assert resultado["DRE_con"][0]["DS_CONTA"] == "Receita de Vendação"


def test_ler_csvs_do_zip_sufixo_sem_arquivo_vira_lista_vazia() -> None:
    conteudo = _zip_com({"dfp_cia_aberta_BPP_con_2024.csv": "A;B\n1;2\n"})

    resultado = ler_csvs_do_zip(conteudo, ["BPP_con", "DFC_MI_con"])

    assert resultado["DFC_MI_con"] == []
    assert resultado["BPP_con"] == [{"A": "1", "B": "2"}]


def test_ler_csvs_do_zip_manter_descarta_linhas() -> None:
    conteudo = _zip_com({"dfp_cia_aberta_BPP_con_2024.csv": "A;B\n1;x\n2;y\n"})
    visto: list[str] = []

    def manter(sufixo: str, linha: dict) -> bool:
        visto.append(sufixo)
        return linha["A"] == "2"

    resultado = ler_csvs_do_zip(conteudo, ["BPP_con"], manter=manter)

    assert resultado["BPP_con"] == [{"A": "2", "B": "y"}]
    assert set(visto) == {"BPP_con"}


def test_url_b3_codifica_os_parametros_em_base64_no_caminho() -> None:
    params = {"codeCVM": "9512", "language": "pt-br"}

    url = url_b3("GetDetail", params)

    esperado = base64.b64encode(
        b'{"codeCVM":"9512","language":"pt-br"}'
    ).decode("ascii")
    assert url == f"{B3_BASE}/GetDetail/{esperado}"
    assert url.endswith(esperado)


def test_decimal_b3_formato_brasileiro() -> None:
    assert decimal_b3("1.234,56") == Decimal("1234.56")
    assert decimal_b3("") is None
    assert decimal_b3(None) is None
    assert decimal_b3("abc") is None


def test_data_b3_dd_mm_aaaa() -> None:
    assert data_b3("21/08/2026") == date(2026, 8, 21)
    assert data_b3("") is None
    assert data_b3("2026-08-21") is None


def test_registro_hash_independe_da_ordem_das_chaves() -> None:
    a = {"ticker": "PETR4", "valor": 1.5, "nome": "Petróleo"}
    b = {"nome": "Petróleo", "valor": 1.5, "ticker": "PETR4"}

    assert registro_hash(a) == registro_hash(b)
    assert len(registro_hash(a)) == 64
    assert registro_hash(a) != registro_hash({**a, "valor": 2.0})


def test_cnpj_digitos_so_numeros() -> None:
    assert cnpj_digitos("33.000.167/0001-01") == "33000167000101"
    assert cnpj_digitos("") == ""


def test_data_cvm_e_decimal_cvm() -> None:
    assert data_cvm("2024-12-31") == date(2024, 12, 31)
    assert data_cvm(" ") is None
    assert data_cvm("31/12/2024") is None
    assert decimal_cvm("-1234.50") == Decimal("-1234.50")
    assert decimal_cvm("") is None
    assert decimal_cvm("x") is None


def test_last_modified_le_o_header_e_devolve_vazio_em_falha() -> None:
    class Fake:
        def fetch(self, url: str, *, method: str = "GET", **kwargs):
            assert method == "HEAD"
            return SimpleNamespace(headers={"Last-Modified": "Tue, 20 Oct 2026 10:00:00 GMT"})

    class Falha:
        def fetch(self, url: str, *, method: str = "GET", **kwargs):
            raise RuntimeError("sem rede")

    assert last_modified(Fake(), "https://x/y.zip") == "Tue, 20 Oct 2026 10:00:00 GMT"
    assert last_modified(Falha(), "https://x/y.zip") == ""


class _CvmStub(CvmCollector):
    """Subclasse mínima só para instanciar a base (que é abstrata)."""

    def work_units(self):
        return []

    def process(self, unit: WorkUnit) -> UnitResult:
        return UnitResult()


class _B3Stub(B3Collector):
    def work_units(self):
        return []

    def process(self, unit: WorkUnit) -> UnitResult:
        return UnitResult()


@pytest.fixture
def cvm():
    coletor = _CvmStub()
    yield coletor
    coletor.close()


@pytest.fixture
def b3():
    coletor = _B3Stub()
    yield coletor
    coletor.close()


def test_cvm_collector_configura_o_delay_do_dominio(cvm: CvmCollector) -> None:
    assert cvm.throttle._delays[CVM_DOMINIO] == get_settings().throttle_cvm
    assert cvm.throttle._default == get_settings().throttle_cvm


def test_b3_collector_configura_o_delay_do_dominio(b3: B3Collector) -> None:
    assert b3.throttle._delays[B3_DOMINIO] == get_settings().throttle_b3


def test_b3_get_json_devolve_json_valido(b3: B3Collector, monkeypatch) -> None:
    capturado = {}

    def fake_fetch(url, *, method="GET", **kwargs):
        capturado.update(kwargs)
        return httpx.Response(200, json={"issuingCompany": "PETR"})

    monkeypatch.setattr(b3, "fetch", fake_fetch)

    assert b3.get_json("GetDetail", {"codeCVM": "9512"}) == {"issuingCompany": "PETR"}
    assert capturado["headers"]["User-Agent"] == USER_AGENT_B3


def test_b3_get_json_devolve_none_para_html(b3: B3Collector, monkeypatch) -> None:
    monkeypatch.setattr(
        b3, "fetch",
        lambda url, **kw: httpx.Response(200, text="<html><body>bloqueado</body></html>"),
    )

    assert b3.get_json("GetDetail", {"codeCVM": "9512"}) is None


def test_b3_get_json_devolve_none_em_erro_http_e_em_falha_de_rede(
    b3: B3Collector, monkeypatch
) -> None:
    monkeypatch.setattr(b3, "fetch", lambda url, **kw: httpx.Response(404, text="nao achou"))
    assert b3.get_json("GetDetail", {"codeCVM": "9512"}) is None

    def falha(url, **kw):
        raise RuntimeError("b3: falha ao buscar")

    monkeypatch.setattr(b3, "fetch", falha)
    assert b3.get_json("GetDetail", {"codeCVM": "9512"}) is None
