"""Tabelas painel_* (vitrine do painel de acompanhamento).

O painel (outro repositorio) le estas tabelas direto no MySQL. A API do painel so escreve
em painel_comandos; o executor deste repo escreve nas demais. Os nomes e colunas seguem a
proposta do painel, adotada como esta.

Double em vez de Float (mesmo motivo de models.py). Timestamps via UTCDateTime.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Double,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tcc_scraper.db.models import PREFIXO_TABELA, Base, UTCDateTime


class PainelComando(Base):
    """Comando enviado pela API do painel; o executor marca ack_em ao aplicar."""

    __tablename__ = f"{PREFIXO_TABELA}painel_comandos"
    __table_args__ = (Index("ix_painel_comandos_ack_em", "ack_em"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(16), nullable=False)
    parametros: Mapped[dict] = mapped_column(JSON, nullable=False)
    criado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    ack_em: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    erro: Mapped[str | None] = mapped_column(Text)


class PainelHeartbeat(Base):
    """Um registro por host do executor, atualizado a cada batida."""

    __tablename__ = f"{PREFIXO_TABELA}painel_heartbeat"

    host: Mapped[str] = mapped_column(String(64), primary_key=True)
    versao: Mapped[str] = mapped_column(String(32), nullable=False)
    ocupado_com: Mapped[str | None] = mapped_column(String(16))
    visto_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class PainelExecucao(Base):
    """Uma execucao diaria ou de etapa, do agendamento ao fim."""

    __tablename__ = f"{PREFIXO_TABELA}painel_execucoes"
    __table_args__ = (Index("ix_painel_execucoes_iniciada_em", "iniciada_em"),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    # daily | etapa | coletar
    tipo: Mapped[str] = mapped_column(String(12), nullable=False)
    etapa_alvo: Mapped[str | None] = mapped_column(String(24))
    # cron | manual
    origem: Mapped[str] = mapped_column(String(8), nullable=False)
    # agendada | em_andamento | concluida | parcial | falhou
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # null | historico (daily, etapa e coletar esperam a carga)
    aguardando: Mapped[str | None] = mapped_column(String(16))
    iniciada_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    finalizada_em: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    prazo_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    erro: Mapped[str | None] = mapped_column(Text)
    atualizada_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class PainelEtapa(Base):
    """Progresso de uma etapa dentro de uma execucao. Apagada junto com a execucao."""

    __tablename__ = f"{PREFIXO_TABELA}painel_etapas"

    execucao_id: Mapped[str] = mapped_column(
        String(48),
        ForeignKey(
            f"{PREFIXO_TABELA}painel_execucoes.id",
            ondelete="CASCADE",
            name="fk_painel_etapas_execucao_id",
        ),
        primary_key=True,
    )
    etapa_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    ordem: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # pendente | em_andamento | concluida | falhou | pulada
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    unidades_total: Mapped[int | None] = mapped_column(Integer)
    unidades_feitas: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    unidades_erro: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    unidades_saturadas: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    itens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Nulo enquanto a etapa esta pendente.
    iniciada_em: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    finalizada_em: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    ultimo_erro_unidade: Mapped[str | None] = mapped_column(String(128))
    ultimo_erro_mensagem: Mapped[str | None] = mapped_column(Text)
    atualizada_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class PainelHistorico(Base):
    """Uma execucao do coletor historico (2 anos de Google News)."""

    __tablename__ = f"{PREFIXO_TABELA}painel_historico"

    # formato hist-AAAAMMDD-HHMMSS, ex.: hist-20261019-210512
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # em_andamento | pausando | pausada | cancelando | cancelada | concluida | falhou
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # pendentes | reprocessar_erros
    modo: Mapped[str] = mapped_column(String(24), nullable=False)
    anos: Mapped[list] = mapped_column(JSON, nullable=False)
    ano_atual: Mapped[int | None] = mapped_column(Integer)
    coletor_atual: Mapped[str | None] = mapped_column(String(32))
    unidade_atual: Mapped[str | None] = mapped_column(String(128))
    iniciada_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    finalizada_em: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    itens_por_min: Mapped[float] = mapped_column(
        Double, nullable=False, default=0, server_default="0"
    )
    erro: Mapped[str | None] = mapped_column(Text)
    atualizada_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class PainelHistoricoProgresso(Base):
    """Progresso por (ano, coletor) do historico. Substituido a cada atualizacao."""

    __tablename__ = f"{PREFIXO_TABELA}painel_historico_progresso"

    ano: Mapped[int] = mapped_column(Integer, primary_key=True)
    coletor: Mapped[str] = mapped_column(String(32), primary_key=True)
    unidades_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    unidades_feitas: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    unidades_erro: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    itens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # concluido | em_andamento | pausado | pendente | falhou
    status_segmento: Mapped[str] = mapped_column(String(16), nullable=False)
    atualizado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class PainelLog(Base):
    """Linha do log exibida no painel. Sem FK: o log sobrevive a limpeza das execucoes."""

    __tablename__ = f"{PREFIXO_TABELA}painel_logs"
    __table_args__ = (Index("ix_painel_logs_ts", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    # info | aviso | erro
    nivel: Mapped[str] = mapped_column(String(8), nullable=False)
    # daily | historico
    origem: Mapped[str] = mapped_column(String(16), nullable=False)
    etapa: Mapped[str | None] = mapped_column(String(24))
    mensagem: Mapped[str] = mapped_column(Text, nullable=False)


class PainelComandoAplicado(Base):
    """Confirmacao de que um comando da API foi aplicado pelo executor."""

    __tablename__ = f"{PREFIXO_TABELA}painel_comandos_aplicados"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    aplicado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    erro: Mapped[str | None] = mapped_column(Text)
