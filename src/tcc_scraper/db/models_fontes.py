"""Tabelas raw_/staging_ das fontes novas (CVM, B3) e da juíza.

Fontes: IPE (CVM dados abertos), balanços e ações da CVM, cadastro e proventos da B3
(listedCompaniesProxy) e a juíza de notícias (LLM que classifica cada artigo por ticker).

Double em vez de Float (mesmo motivo de models.py). Timestamps via UTCDateTime.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tcc_scraper.db.models import Base, LongText, UTCDateTime


class RawIpeDoc(Base):
    """Documento do IPE (fato relevante, comunicado etc.) da CVM, uma linha por protocolo."""

    __tablename__ = "raw_ipe_docs"
    __table_args__ = (Index("ix_raw_ipe_docs_cd_cvm", "cd_cvm"),)

    protocolo_entrega: Mapped[str] = mapped_column(String(32), primary_key=True)
    cnpj: Mapped[str] = mapped_column(String(14), nullable=False)
    nome_companhia: Mapped[str | None] = mapped_column(Text)
    cd_cvm: Mapped[int] = mapped_column(Integer, nullable=False)
    data_referencia: Mapped[dt.date | None] = mapped_column(Date)
    categoria: Mapped[str] = mapped_column(String(64), nullable=False)
    tipo: Mapped[str | None] = mapped_column(String(128))
    especie: Mapped[str | None] = mapped_column(String(128))
    assunto: Mapped[str | None] = mapped_column(Text)
    data_entrega: Mapped[dt.date] = mapped_column(Date, nullable=False)
    tipo_apresentacao: Mapped[str | None] = mapped_column(String(32))
    versao: Mapped[int | None] = mapped_column(Integer)
    link_download: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawCvmBalanco(Base):
    """Linha de demonstrativo (DFP/ITR) da CVM, com o valor exatamente como veio (sem escala).

    BPP nao tem inicio de exercicio: o coletor grava dt_ini_exerc = dt_fim_exerc.
    """

    __tablename__ = "raw_cvm_balancos"
    __table_args__ = (Index("ix_raw_cvm_balancos_cd_cvm", "cd_cvm"),)

    cnpj: Mapped[str] = mapped_column(String(14), primary_key=True)
    tipo_documento: Mapped[str] = mapped_column(String(3), primary_key=True)
    dt_refer: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    versao: Mapped[int] = mapped_column(Integer, primary_key=True)
    demonstracao: Mapped[str] = mapped_column(String(3), primary_key=True)
    cd_conta: Mapped[str] = mapped_column(String(16), primary_key=True)
    dt_ini_exerc: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    dt_fim_exerc: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    cd_cvm: Mapped[int] = mapped_column(Integer, nullable=False)
    ds_conta: Mapped[str | None] = mapped_column(String(255))
    ordem_exerc: Mapped[str] = mapped_column(String(16), nullable=False)
    escala_moeda: Mapped[str] = mapped_column(String(8), nullable=False)
    vl_conta: Mapped[float] = mapped_column(Double, nullable=False)
    dt_receb: Mapped[dt.date | None] = mapped_column(Date)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawCvmAcoes(Base):
    """Quantidade de ações emitidas por companhia e data de referência da CVM."""

    __tablename__ = "raw_cvm_acoes"

    cnpj: Mapped[str] = mapped_column(String(14), primary_key=True)
    tipo_documento: Mapped[str] = mapped_column(String(3), primary_key=True)
    dt_refer: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    versao: Mapped[int] = mapped_column(Integer, primary_key=True)
    cd_cvm: Mapped[int] = mapped_column(Integer, nullable=False)
    qt_on: Mapped[int | None] = mapped_column(BigInteger)
    qt_pn: Mapped[int | None] = mapped_column(BigInteger)
    qt_total: Mapped[int | None] = mapped_column(BigInteger)
    dt_receb: Mapped[dt.date | None] = mapped_column(Date)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawCvmCadastro(Base):
    """Cadastro da companhia aberta na CVM (uma linha por CNPJ)."""

    __tablename__ = "raw_cvm_cadastro"
    __table_args__ = (Index("ix_raw_cvm_cadastro_cd_cvm", "cd_cvm"),)

    cnpj: Mapped[str] = mapped_column(String(14), primary_key=True)
    cd_cvm: Mapped[int] = mapped_column(Integer, nullable=False)
    denom_social: Mapped[str] = mapped_column(Text, nullable=False)
    sit: Mapped[str | None] = mapped_column(String(32))
    setor_ativ: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawB3Cadastro(Base):
    """Cadastro da companhia no listedCompaniesProxy da B3, chaveado pelo código CVM."""

    __tablename__ = "raw_b3_cadastro"

    cd_cvm: Mapped[int] = mapped_column(Integer, primary_key=True)
    cnpj: Mapped[str | None] = mapped_column(String(14))
    trading_name: Mapped[str | None] = mapped_column(String(128))
    issuing_company: Mapped[str | None] = mapped_column(String(8))
    industry_classification: Mapped[str | None] = mapped_column(Text)
    other_codes: Mapped[dict | list | None] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawB3Proventos(Base):
    """Evento de provento (dividendo, JCP etc.) da B3. Chave: hash do registro original."""

    __tablename__ = "raw_b3_proventos"
    __table_args__ = (Index("ix_raw_b3_proventos_issuing_company", "issuing_company"),)

    registro_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuing_company: Mapped[str] = mapped_column(String(8), nullable=False)
    trading_name: Mapped[str | None] = mapped_column(String(128))
    type_stock: Mapped[str | None] = mapped_column(String(16))
    corporate_action: Mapped[str | None] = mapped_column(String(64))
    value_cash: Mapped[float | None] = mapped_column(Double)
    last_date_prior_ex: Mapped[dt.date | None] = mapped_column(Date)
    date_approval: Mapped[dt.date | None] = mapped_column(Date)
    ratio: Mapped[str | None] = mapped_column(String(32))
    quoted_per_shares: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawB3Suplemento(Base):
    """Suplemento da B3 (eventos de dinheiro ou de ações). Chave: hash do registro original."""

    __tablename__ = "raw_b3_suplemento"
    __table_args__ = (Index("ix_raw_b3_suplemento_issuing_company", "issuing_company"),)

    registro_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuing_company: Mapped[str] = mapped_column(String(8), nullable=False)
    # cash | stock
    tipo: Mapped[str] = mapped_column(String(8), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawLlmJuizaRun(Base):
    """Uma chamada da juíza ao Gemini, inclusive as que falharam. Append-only."""

    __tablename__ = "raw_llm_juiza_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prompt_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("raw_llm_prompts.id", name="fk_raw_llm_juiza_runs_prompt_id"),
        nullable=False,
    )
    modelo: Mapped[str] = mapped_column(String(64), nullable=False)
    n_itens: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_entrada: Mapped[int | None] = mapped_column(Integer)
    tokens_saida: Mapped[int | None] = mapped_column(Integer)
    latencia_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    erro: Mapped[str | None] = mapped_column(Text)
    resposta_bruta: Mapped[str | None] = mapped_column(LongText)
    iniciado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    finalizado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class StagingNoticiaJulgamento(Base):
    """Julgamento da juíza para um par (artigo, ticker) em uma versão do prompt."""

    __tablename__ = "staging_noticias_julgamento"
    __table_args__ = (Index("ix_staging_noticias_julgamento_ticker", "ticker"),)

    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "raw_articles.id", ondelete="CASCADE",
            name="fk_staging_noticias_julgamento_article_id",
        ),
        primary_key=True,
    )
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    prompt_versao: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("raw_llm_juiza_runs.id", name="fk_staging_noticias_julgamento_run_id"),
    )
    modelo: Mapped[str] = mapped_column(String(64), nullable=False)
    papel: Mapped[str] = mapped_column(String(24), nullable=False)
    tipo_conteudo: Mapped[str] = mapped_column(String(24), nullable=False)
    evento: Mapped[str] = mapped_column(String(24), nullable=False)
    tickers_adicionais: Mapped[list] = mapped_column(JSON, nullable=False)
    qualidade: Mapped[float] = mapped_column(Double, nullable=False)
    confianca: Mapped[float] = mapped_column(Double, nullable=False)
    julgado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
