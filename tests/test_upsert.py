"""Testes de integracao do upsert contra o MySQL do .env (opt-in: TCC_TESTES_COM_BANCO=1).

Por que banco real e nao mock: o que se testa aqui e o comportamento real do
`ON DUPLICATE KEY UPDATE`. Um mock confirmaria apenas que o codigo chama o que o codigo
chama.
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import Session

from tcc_scraper.db.upsert import bulk_upsert


@pytest.fixture
def tabela(engine):
    """Tabela descartavel com uma chave unica, recriada a cada teste."""
    meta = MetaData()
    t = Table(
        "upsert_probe",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("chave", String(64), nullable=False),
        Column("valor", String(64), nullable=False),
        UniqueConstraint("chave", name="uq_upsert_probe_chave"),
    )
    meta.drop_all(engine)
    meta.create_all(engine)
    yield t
    meta.drop_all(engine)


def _linhas(session, tabela):
    return {r.chave: r.valor for r in session.execute(select(tabela)).all()}


def test_insere_linhas_novas(engine, tabela) -> None:
    with Session(engine) as s:
        n = bulk_upsert(
            s, tabela,
            [{"chave": "a", "valor": "1"}, {"chave": "b", "valor": "2"}],
            index_elements=["chave"], update_columns=["valor"],
        )
        s.commit()
        assert n == 2
        assert _linhas(s, tabela) == {"a": "1", "b": "2"}


def test_conflito_com_update_sobrescreve(engine, tabela) -> None:
    with Session(engine) as s:
        bulk_upsert(s, tabela, [{"chave": "a", "valor": "1"}],
                    index_elements=["chave"], update_columns=["valor"])
        s.commit()

        bulk_upsert(s, tabela, [{"chave": "a", "valor": "ATUALIZADO"}],
                    index_elements=["chave"], update_columns=["valor"])
        s.commit()

        assert _linhas(s, tabela) == {"a": "ATUALIZADO"}


def test_conflito_sem_update_preserva_o_existente(engine, tabela) -> None:
    """update_columns=None significa nao atualizar: a linha existente e preservada."""
    with Session(engine) as s:
        bulk_upsert(s, tabela, [{"chave": "a", "valor": "ORIGINAL"}],
                    index_elements=["chave"], update_columns=["valor"])
        s.commit()

        bulk_upsert(s, tabela, [{"chave": "a", "valor": "IGNORADO"}],
                    index_elements=["chave"], update_columns=None)
        s.commit()

        assert _linhas(s, tabela) == {"a": "ORIGINAL"}


def test_lote_misto_insere_novos_e_atualiza_existentes(engine, tabela) -> None:
    """O caso real da coleta: a janela traz materia nova junto com materia repetida."""
    with Session(engine) as s:
        bulk_upsert(s, tabela, [{"chave": "a", "valor": "1"}],
                    index_elements=["chave"], update_columns=["valor"])
        s.commit()

        bulk_upsert(
            s, tabela,
            [{"chave": "a", "valor": "1-novo"}, {"chave": "b", "valor": "2"}],
            index_elements=["chave"], update_columns=["valor"],
        )
        s.commit()

        assert _linhas(s, tabela) == {"a": "1-novo", "b": "2"}


def test_lista_vazia_nao_emite_query(engine, tabela) -> None:
    with Session(engine) as s:
        assert bulk_upsert(s, tabela, [], index_elements=["chave"]) == 0
        assert _linhas(s, tabela) == {}


def test_divide_em_blocos_acima_do_chunk_size(engine, tabela) -> None:
    """Lotes grandes viram varios INSERTs para nao estourar max_allowed_packet."""
    rows = [{"chave": f"k{i}", "valor": str(i)} for i in range(120)]
    with Session(engine) as s:
        n = bulk_upsert(s, tabela, rows, index_elements=["chave"],
                        update_columns=["valor"], chunk_size=25)
        s.commit()
        assert n == 120
        assert len(_linhas(s, tabela)) == 120


def test_duplicatas_dentro_do_mesmo_lote(engine, tabela) -> None:
    """A mesma URL pode aparecer duas vezes na resposta de uma janela.

    Este e o caso que mais diverge entre os bancos, por isso tem teste proprio:
    o comportamento precisa ser DETERMINISTICO -- uma linha por chave.
    """
    rows = [{"chave": "dup", "valor": "primeiro"}, {"chave": "dup", "valor": "segundo"}]
    with Session(engine) as s:
        bulk_upsert(s, tabela, rows, index_elements=["chave"], update_columns=["valor"])
        s.commit()

        resultado = _linhas(s, tabela)
        assert len(resultado) == 1
        # Deterministico: a ultima ocorrencia vence.
        assert resultado["dup"] == "segundo"


def test_dialeto_nao_suportado_falha_claramente() -> None:
    """SQLite silenciosamente ignoraria a semantica de upsert -- melhor falhar."""
    eng = create_engine("sqlite:///:memory:")
    meta = MetaData()
    t = Table("x", meta, Column("chave", String(10), primary_key=True))
    meta.create_all(eng)

    with Session(eng) as s, pytest.raises(NotImplementedError, match="sqlite"):
        bulk_upsert(s, t, [{"chave": "a"}], index_elements=["chave"])
