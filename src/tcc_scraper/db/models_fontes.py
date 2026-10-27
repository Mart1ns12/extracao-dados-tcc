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
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tcc_scraper.db.models import PREFIXO_TABELA, Base, LongText, UTCDateTime


class RawIpeDoc(Base):
    """Documento do IPE (fato relevante, comunicado etc.) da CVM, uma linha por protocolo."""

    __tablename__ = f"{PREFIXO_TABELA}raw_ipe_docs"
    __table_args__ = (Index("ix_raw_ipe_docs_cd_cvm", "cd_cvm"),)

    protocolo_entrega: Mapped[str] = mapped_column(String(32), primary_key=True)
    cnpj: Mapped[str] = mapped_column(String(14), nullable=False)
    nome_companhia: Mapped[str | None] = mapped_column(Text)
    cd_cvm: Mapped[int] = mapped_column(Integer, nullable=False)
    data_referencia: Mapped[dt.date | None] = mapped_column(Date)
    categoria: Mapped[str] = mapped_column(String(128), nullable=False)
    tipo: Mapped[str | None] = mapped_column(String(255))
    especie: Mapped[str | None] = mapped_column(String(255))
    assunto: Mapped[str | None] = mapped_column(Text)
    data_entrega: Mapped[dt.date] = mapped_column(Date, nullable=False)
    tipo_apresentacao: Mapped[str | None] = mapped_column(String(64))
    versao: Mapped[int | None] = mapped_column(Integer)
    link_download: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawCvmBalanco(Base):
    """Linha de demonstrativo (DFP/ITR) da CVM, com o valor exatamente como veio (sem escala).

    BPP nao tem inicio de exercicio: o coletor grava dt_ini_exerc = dt_fim_exerc.
    """

    __tablename__ = f"{PREFIXO_TABELA}raw_cvm_balancos"
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

    __tablename__ = f"{PREFIXO_TABELA}raw_cvm_acoes"

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

    __tablename__ = f"{PREFIXO_TABELA}raw_cvm_cadastro"
    __table_args__ = (Index("ix_raw_cvm_cadastro_cd_cvm", "cd_cvm"),)

    cnpj: Mapped[str] = mapped_column(String(14), primary_key=True)
    cd_cvm: Mapped[int] = mapped_column(Integer, nullable=False)
    denom_social: Mapped[str] = mapped_column(Text, nullable=False)
    sit: Mapped[str | None] = mapped_column(String(32))
    setor_ativ: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawB3Cadastro(Base):
    """Cadastro da companhia no listedCompaniesProxy da B3, chaveado pelo código CVM."""

    __tablename__ = f"{PREFIXO_TABELA}raw_b3_cadastro"

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

    __tablename__ = f"{PREFIXO_TABELA}raw_b3_proventos"
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

    __tablename__ = f"{PREFIXO_TABELA}raw_b3_suplemento"
    __table_args__ = (Index("ix_raw_b3_suplemento_issuing_company", "issuing_company"),)

    registro_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuing_company: Mapped[str] = mapped_column(String(8), nullable=False)
    # cash | stock
    tipo: Mapped[str] = mapped_column(String(8), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawLlmJuizaRun(Base):
    """Uma chamada da juíza ao Gemini, inclusive as que falharam. Append-only."""

    __tablename__ = f"{PREFIXO_TABELA}raw_llm_juiza_runs"

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
    custo_usd: Mapped[float | None] = mapped_column(Double)
    latencia_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # producao | backlog | concordancia: o teto mensal e a verba do backlog somam por aqui.
    finalidade: Mapped[str] = mapped_column(
        String(16), nullable=False, default="producao", server_default="producao",
    )
    erro: Mapped[str | None] = mapped_column(Text)
    resposta_bruta: Mapped[str | None] = mapped_column(LongText)
    iniciado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    finalizado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class StagingNoticiaJulgamento(Base):
    """Julgamento da juíza para um par (artigo, ticker) em uma versão do prompt."""

    __tablename__ = f"{PREFIXO_TABELA}staging_noticias_julgamento"
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
        ForeignKey(
            f"{PREFIXO_TABELA}raw_llm_juiza_runs.id",
            name="fk_staging_noticias_julgamento_run_id",
        ),
    )
    modelo: Mapped[str] = mapped_column(String(64), nullable=False)
    papel: Mapped[str] = mapped_column(String(24), nullable=False)
    tipo_conteudo: Mapped[str] = mapped_column(String(24), nullable=False)
    evento: Mapped[str] = mapped_column(String(24), nullable=False)
    tickers_adicionais: Mapped[list] = mapped_column(JSON, nullable=False)
    qualidade: Mapped[float] = mapped_column(Double, nullable=False)
    confianca: Mapped[float] = mapped_column(Double, nullable=False)
    julgado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class StagingJuizaTentativa(Base):
    """Tentativas da juiza por par (artigo, ticker) e versao do prompt, inclusive as que falharam.

    Fica fora da staging de julgamentos: la, "linha existe" quer dizer "julgado".
    """

    __tablename__ = f"{PREFIXO_TABELA}staging_juiza_tentativas"

    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "raw_articles.id", ondelete="CASCADE",
            name="fk_staging_juiza_tentativas_article_id",
        ),
        primary_key=True,
    )
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    prompt_versao: Mapped[int] = mapped_column(Integer, primary_key=True)
    tentativas: Mapped[int] = mapped_column(Integer, nullable=False)
    ultimo_status: Mapped[str] = mapped_column(String(16), nullable=False)
    ultimo_erro: Mapped[str | None] = mapped_column(Text)
    atualizado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class RawWpPost(Base):
    """Post do WordPress (InfoMoney, Money Times, Exame), uma linha por site e id do WP."""

    __tablename__ = f"{PREFIXO_TABELA}raw_wp_posts"
    __table_args__ = (Index("ix_raw_wp_posts_date_gmt", "date_gmt"),)

    site: Mapped[str] = mapped_column(String(16), primary_key=True)
    wp_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    link: Mapped[str] = mapped_column(Text, nullable=False)
    date_gmt: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    wp_type: Mapped[str] = mapped_column(String(32), nullable=False)
    titulo_html: Mapped[str] = mapped_column(Text, nullable=False)
    content_html: Mapped[str] = mapped_column(LongText, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class StagingHistoria(Base):
    """Historia (republicacao entre veiculos) a que cada artigo pertence.

    O metodo diz como o artigo foi ligado a historia; regras_versao, a versao do casador.
    """

    __tablename__ = f"{PREFIXO_TABELA}staging_historias"
    __table_args__ = (Index("ix_staging_historias_historia_article_id", "historia_article_id"),)

    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "raw_articles.id", ondelete="CASCADE",
            name="fk_staging_historias_article_id",
        ),
        primary_key=True,
    )
    historia_article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("raw_articles.id", name="fk_staging_historias_historia_article_id"),
        nullable=False,
    )
    # wp_titulo | wp_fuzzy | google_url | google_titulo | google_fuzzy | representante
    metodo: Mapped[str] = mapped_column(String(16), nullable=False)
    regras_versao: Mapped[int] = mapped_column(Integer, nullable=False)
    atualizado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class StagingWpProcessamento(Base):
    """Processamento de um post WordPress por versao de regras e prompt (com ou sem ticker)."""

    __tablename__ = f"{PREFIXO_TABELA}staging_wp_processamento"
    __table_args__ = (
        ForeignKeyConstraint(
            ["site", "wp_id"],
            [f"{PREFIXO_TABELA}raw_wp_posts.site", f"{PREFIXO_TABELA}raw_wp_posts.wp_id"],
            name="fk_staging_wp_processamento_raw_wp_posts",
        ),
    )

    site: Mapped[str] = mapped_column(String(16), primary_key=True)
    wp_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    regras_versao: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_versao: Mapped[int] = mapped_column(Integer, nullable=False)
    article_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("raw_articles.id", name="fk_staging_wp_processamento_article_id"),
    )
    # representante | membro | sem_ticker
    papel_historia: Mapped[str] = mapped_column(String(16), nullable=False)
    n_pares: Mapped[int] = mapped_column(Integer, nullable=False)
    processado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
