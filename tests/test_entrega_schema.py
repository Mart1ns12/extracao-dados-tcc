"""Modelos das 8 tabelas de entrega, sem banco.

Garante o contrato da migration 0006: nomes fixos pela spec, colunas na ordem da spec
(parte 2), chaves primarias compostas como na spec, indices uteis e nenhum Float (so Double).
"""

from __future__ import annotations

from sqlalchemy import Double, Float

from tcc_scraper.db import models_entrega
from tcc_scraper.db.models import ENTREGA_TABLES, Base, tabela_do_projeto
from tcc_scraper.db.models_entrega import COLUNAS

PKS_ESPERADAS = {
    "noticias": ["id", "ticker"],
    "precos": ["data", "ticker"],
    "cadastro": ["ticker"],
    "mudancas_ticker": ["ticker_antigo"],
    "balancos": ["cnpj", "tipo_documento", "data_inicio_periodo", "data_fim_periodo"],
    "acoes_emitidas": ["cnpj", "data_referencia"],
    "proventos": ["ticker", "tipo", "data_com"],
    "indices": ["data", "indice"],
}

INDICES_ESPERADOS = {
    "noticias": {"ix_noticias_ticker_data_publicacao"},
    "precos": {"ix_precos_ticker"},
    "proventos": {"ix_proventos_ticker"},
    "indices": {"ix_indices_indice"},
}


def test_oito_tabelas_de_entrega_no_metadata() -> None:
    assert set(COLUNAS) == set(ENTREGA_TABLES)
    assert set(ENTREGA_TABLES) <= set(Base.metadata.tables)
    assert len(ENTREGA_TABLES) == 8


def test_nomes_passam_no_filtro_de_camada() -> None:
    for nome in ENTREGA_TABLES:
        assert tabela_do_projeto(nome), nome
        assert not nome.startswith(("raw_", "staging_", "features_", "painel_")), nome


def test_colunas_conferem_com_a_ordem_da_spec() -> None:
    for nome, colunas in COLUNAS.items():
        modelo = Base.metadata.tables[nome]
        assert [c.name for c in modelo.columns] == colunas, nome


def test_noticias_comeca_pelas_dez_colunas_da_spec() -> None:
    assert COLUNAS["noticias"][:10] == [
        "id", "ticker", "data_hora_publicacao", "data_hora_coleta", "titulo", "fonte",
        "link", "prob_positivo", "prob_neutro", "prob_negativo",
    ]


def test_chaves_primarias_conferem() -> None:
    for nome, chave in PKS_ESPERADAS.items():
        modelo = Base.metadata.tables[nome]
        assert [c.name for c in modelo.primary_key.columns] == chave, nome


def test_indices_uteis_existem() -> None:
    for nome, esperados in INDICES_ESPERADOS.items():
        modelo = Base.metadata.tables[nome]
        assert {ix.name for ix in modelo.indexes} == esperados, nome


def test_nenhuma_coluna_float_so_double() -> None:
    for nome in COLUNAS:
        for coluna in Base.metadata.tables[nome].columns:
            # Double herda de Float no SQLAlchemy: compara para nao confundir os dois.
            assert not (isinstance(coluna.type, Float) and not isinstance(coluna.type, Double)), (
                f"{nome}.{coluna.name} usa Float; use Double"
            )


def test_modulo_registra_as_classes() -> None:
    nomes = {m.__tablename__ for m in vars(models_entrega).values()
             if isinstance(m, type) and issubclass(m, Base) and hasattr(m, "__tablename__")}
    assert nomes == set(COLUNAS)
