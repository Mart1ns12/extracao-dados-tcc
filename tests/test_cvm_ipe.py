"""Coletor do IPE da CVM: parser, artigo derivado e persistencia, sem rede e sem banco."""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import String

from tcc_scraper.collectors import cvm_ipe, google_news
from tcc_scraper.collectors.base import WorkUnit
from tcc_scraper.collectors.cvm_ipe import (
    CvmIpeCollector,
    artigo_de_ipe,
    parse_ipe,
    tickers_do_cd_cvm,
)
from tcc_scraper.config import IPE_CATEGORIAS, TICKER_CODE_CVM
from tcc_scraper.db.models import Article
from tcc_scraper.db.models_fontes import RawIpeDoc

FIXTURE = Path(__file__).parent / "fixtures" / "cvm_ipe_2026_amostra.csv"
CD_CVM_PETR = 9512
DESDE_TUDO = dt.date(2000, 1, 1)


def _linhas() -> list[dict]:
    texto = FIXTURE.read_bytes().decode("latin-1")
    return list(csv.DictReader(io.StringIO(texto), delimiter=";"))


def _universo() -> set[int]:
    return set(TICKER_CODE_CVM.values())


def test_categoria_fora_da_lista_nao_entra() -> None:
    docs = parse_ipe(_linhas(), _universo(), frozenset({"Fato Relevante"}), DESDE_TUDO)

    assert docs
    assert {doc.categoria for doc in docs} == {"Fato Relevante"}


def test_so_companhias_do_universo_entram() -> None:
    docs = parse_ipe(_linhas(), {CD_CVM_PETR}, IPE_CATEGORIAS, DESDE_TUDO)

    assert docs
    assert {doc.cd_cvm for doc in docs} == {CD_CVM_PETR}


def test_codigo_cvm_com_zeros_a_esquerda_e_comparado_como_int() -> None:
    linha = dict(_linhas()[0])
    linha["Codigo_CVM"] = "0" + str(linha["Codigo_CVM"])
    linha["Categoria"] = "Fato Relevante"
    linha["Data_Entrega"] = "2026-10-10"
    cd_cvm = int(linha["Codigo_CVM"])

    docs = parse_ipe([linha], {cd_cvm}, IPE_CATEGORIAS, DESDE_TUDO)

    assert len(docs) == 1
    assert docs[0].cd_cvm == cd_cvm


def test_entrega_anterior_a_desde_nao_entra() -> None:
    docs = parse_ipe(_linhas(), _universo(), IPE_CATEGORIAS, dt.date(2026, 10, 1))

    assert docs
    assert all(doc.data_entrega >= dt.date(2026, 10, 1) for doc in docs)


def test_published_at_e_inicio_do_dia_em_brasilia_convertido_para_utc() -> None:
    linha = dict(_linhas()[0])
    linha["Categoria"] = "Fato Relevante"
    linha["Data_Entrega"] = "2026-10-10"
    doc = parse_ipe([linha], {int(linha["Codigo_CVM"])}, IPE_CATEGORIAS, DESDE_TUDO)[0]

    artigo = artigo_de_ipe(doc, dt.datetime(2026, 10, 10, 12, tzinfo=dt.UTC))

    assert artigo["published_at"].isoformat() == "2026-10-10T03:00:00+00:00"
    assert artigo["published_precision"] == "date"
    assert artigo["origem"] == "cvm_ipe"
    assert artigo["source_name"] == "CVM"
    assert artigo["resolved_url"] == doc.link


def test_url_hash_igual_ao_do_google_news() -> None:
    doc = parse_ipe(_linhas(), _universo(), IPE_CATEGORIAS, DESDE_TUDO)[0]

    artigo = artigo_de_ipe(doc, dt.datetime(2026, 10, 10, tzinfo=dt.UTC))

    assert artigo["url_hash"] == google_news.url_hash(doc.link)


def test_tickers_do_cd_cvm_lista_todas_as_classes_da_companhia(monkeypatch) -> None:
    monkeypatch.setitem(TICKER_CODE_CVM, "PETR3", CD_CVM_PETR)
    monkeypatch.setitem(TICKER_CODE_CVM, "PETR4", CD_CVM_PETR)

    tickers = tickers_do_cd_cvm(CD_CVM_PETR)

    assert "PETR3" in tickers
    assert "PETR4" in tickers
    assert tickers == sorted(tickers)


def test_work_units_gera_uma_chave_por_ano(monkeypatch) -> None:
    monkeypatch.setattr(
        cvm_ipe, "last_modified", lambda collector, url: "Wed, 01 Jan 2026 00:00:00 GMT"
    )
    coletor = CvmIpeCollector(anos=[2025, 2026])

    unidades = list(coletor.work_units())

    assert len(unidades) == 2
    assert {unidade.payload["ano"] for unidade in unidades} == {2025, 2026}
    assert len({unidade.key for unidade in unidades}) == 2


def test_process_le_o_csv_do_zip_e_persiste(monkeypatch) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("ipe_cia_aberta_2026.csv", FIXTURE.read_bytes())
    gravados: list[list] = []
    monkeypatch.setattr(CvmIpeCollector, "baixar_zip", lambda self, url: buffer.getvalue())
    monkeypatch.setattr(cvm_ipe, "_persist", lambda docs: gravados.append(docs) or len(docs))
    coletor = CvmIpeCollector(anos=[2026])

    resultado = coletor.process(WorkUnit(key="x", payload={"ano": 2026, "url": "u"}))

    assert resultado.items_found == len(gravados[0])
    assert gravados[0]


class _FakeResult:
    def __init__(self, linhas: list[tuple]) -> None:
        self._linhas = linhas

    def all(self) -> list[tuple]:
        return self._linhas


class _FakeSession:
    def __init__(self) -> None:
        self.articles: list[dict] = []

    def flush(self) -> None:
        return None

    def execute(self, stmt) -> _FakeResult:
        return _FakeResult([(i + 1, a["url_hash"]) for i, a in enumerate(self.articles)])


def test_persist_grava_documentos_artigos_e_um_vinculo_por_ticker(monkeypatch) -> None:
    gravacoes: dict[str, list] = {}
    sessao = _FakeSession()

    def fake_bulk_upsert(session, table, rows, *, index_elements, update_columns=None):
        gravacoes.setdefault(table.name, []).extend(rows)
        if table is Article.__table__:
            session.articles.extend(rows)
        return len(rows)

    @contextmanager
    def fake_session_scope():
        yield sessao

    monkeypatch.setattr(cvm_ipe, "bulk_upsert", fake_bulk_upsert)
    monkeypatch.setattr(cvm_ipe, "session_scope", fake_session_scope)
    monkeypatch.setitem(TICKER_CODE_CVM, "PETR3", CD_CVM_PETR)
    monkeypatch.setitem(TICKER_CODE_CVM, "PETR4", CD_CVM_PETR)
    docs = parse_ipe(_linhas(), {CD_CVM_PETR}, IPE_CATEGORIAS, DESDE_TUDO)

    total = cvm_ipe._persist(docs)

    assert total == len(docs)
    assert len(gravacoes["v1-raw_ipe_docs"]) == len(docs)
    assert len(gravacoes["v1-raw_articles"]) == len(docs)
    vinculos = gravacoes["v1-staging_article_tickers"]
    assert {v["ticker"] for v in vinculos} == {"PETR3", "PETR4"}
    assert len(vinculos) == 2 * len(gravacoes["v1-raw_articles"])
    assert all(linha["origem"] == "cvm_ipe" for linha in gravacoes["v1-raw_articles"])
    assert gravacoes["v1-raw_articles"][0]["title"]


def test_persist_sem_documentos_nao_toca_o_banco(monkeypatch) -> None:
    def proibido(*args, **kwargs):
        raise AssertionError("nao deveria tocar o banco")

    monkeypatch.setattr(cvm_ipe, "session_scope", proibido)

    assert cvm_ipe._persist([]) == 0


def test_colunas_de_texto_cabem_todos_os_valores_da_fixture() -> None:
    # Regressao: tipo_apresentacao (32) estourou com valor real de 33 caracteres.
    linhas = _linhas()
    categorias = frozenset(linha["Categoria"] for linha in linhas)
    universo = {int(linha["Codigo_CVM"]) for linha in linhas}

    docs = parse_ipe(linhas, universo, categorias, DESDE_TUDO)

    atributo_de = {
        "protocolo_entrega": "protocolo",
        "cnpj": "cnpj",
        "categoria": "categoria",
        "tipo": "tipo",
        "especie": "especie",
        "tipo_apresentacao": "tipo_apresentacao",
    }
    colunas = [c for c in RawIpeDoc.__table__.c if isinstance(c.type, String) and c.type.length]
    assert {c.name for c in colunas} == set(atributo_de)
    assert docs
    for coluna in colunas:
        maior = max(len(getattr(doc, atributo_de[coluna.name])) for doc in docs)
        assert maior <= coluna.type.length, f"{coluna.name}: {maior} > {coluna.type.length}"
