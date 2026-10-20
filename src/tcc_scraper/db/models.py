"""Modelo de dados em tres camadas.

raw      -- imutavel e append-only. E o unico lugar onde a rede escreve.
staging  -- derivado da raw por modelos/heuristicas (sentimento, vinculo noticia-ticker).
features -- o dataset final, materializado.

A separacao existe para que trocar o modelo de sentimento ou a regra do target seja um
reprocessamento local, sem recoletar 17 mil requisicoes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

# As camadas viram PREFIXO no nome da tabela (raw_/staging_/features_), nao schema
# separado: o MySQL nao tem schemas como o PostgreSQL -- la "schema" e "database"
# sao a mesma coisa. Um unico esquema de nomes funciona nos dois bancos.
TABLE_PREFIXES = ("raw_", "staging_", "features_", "painel_")
# Tabelas de entrega: nomes fixos pela spec da etapa seguinte do TCC, sem prefixo de camada.
ENTREGA_TABLES = (
    "noticias", "precos", "cadastro", "mudancas_ticker",
    "balancos", "acoes_emitidas", "proventos", "indices",
)


def tabela_do_projeto(name: str) -> bool:
    """Tabela criada por este projeto: prefixo de camada, ou uma das 8 tabelas de entrega.

    As tabelas de entrega tem nome fixo pela spec, por isso nao carregam prefixo.
    """
    return name.startswith(TABLE_PREFIXES) or name in ENTREGA_TABLES


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class UTCDateTime(TypeDecorator):
    """Timestamp sempre em UTC, nos dois bancos.

    O PostgreSQL guarda o fuso junto (TIMESTAMPTZ); o MySQL NAO -- um DATETIME volta
    sem fuso nenhum. Sem este tipo, `published_at` chegaria aware do Postgres e naive
    do MySQL, e a agregacao diaria (que converte para America/Sao_Paulo) mudaria de
    resultado conforme o banco -- uma materia das 22h poderia cair no dia seguinte.

    Contrato: entra aware (ou assume-se UTC), e gravado em UTC, volta sempre aware.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name in ("mysql", "mariadb"):
            # fsp=6 preserva microssegundos; o default do MySQL truncaria para segundos.
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        value = value.astimezone(dt.UTC)
        # O MySQL rejeita datetime com tzinfo: grava-se o instante UTC ja "naive".
        if dialect.name in ("mysql", "mariadb"):
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


# Double, nao Float: `sa.Float()` vira `double precision` (8 bytes) no PostgreSQL mas
# `float` (4 bytes, ~7 digitos) no MySQL. Medido: a probabilidade 0.7766114473342896
# voltava 0.776611 do MySQL, e o dataset mudava conforme o banco -- inaceitavel, ainda
# mais porque o erro propaga para score_finbert e para os retornos. `Double` mapeia
# para 8 bytes nos dois.

# Parametros de rastreamento que mudam a URL sem mudar o artigo. Removidos antes do
# hash, senao a mesma materia entra varias vezes vinda de queries diferentes.
_TRACKING_PARAMS = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "oc", "fbclid", "gclid"}
)


def canonical_url(url: str) -> str:
    """Normaliza a URL para deduplicacao: minusculas no host, sem tracking, sem fragmento."""
    parts = urlsplit(url.strip())
    query = "&".join(
        sorted(
            kv
            for kv in parts.query.split("&")
            if kv and kv.split("=", 1)[0].lower() not in _TRACKING_PARAMS
        )
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def url_hash(url: str) -> str:
    """SHA-256 da URL canonicalizada. Chave de deduplicacao de artigos."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# raw
# --------------------------------------------------------------------------------------
class Article(Base):
    """Uma noticia unica. A mesma materia citada por varios tickers aparece UMA vez aqui."""

    __tablename__ = "raw_articles"
    __table_args__ = (
        Index("ix_articles_published_at", "published_at"),
        Index("ix_articles_source_domain", "source_domain"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Hash da URL de DESCOBERTA (a do Google News). E a chave de deduplicacao e
    # precisa continuar batendo com `url`, por isso nenhum dos dois e sobrescrito
    # quando o link e resolvido -- a URL real vai para resolved_url.
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)

    # Id opaco do Google News. NAO e uma URL e nao decodifica para uma -- guardado
    # so para reprocessar caso surja um metodo de resolucao melhor.
    google_id: Mapped[str | None] = mapped_column(Text)

    # URL real do veiculo, preenchida pelo link_resolver. Deixa o dataset auditavel
    # (da para abrir a materia) sem quebrar a deduplicacao.
    resolved_url: Mapped[str | None] = mapped_column(Text)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str | None] = mapped_column(Text)
    source_domain: Mapped[str | None] = mapped_column(String(255))

    published_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False)
    collected_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)

    body = relationship("ArticleBody", back_populates="article", uselist=False)
    tickers = relationship("ArticleTicker", back_populates="article")


class ArticleBody(Base):
    """Corpo do artigo, quando extraido. Ausencia = so a manchete esta disponivel."""

    __tablename__ = "raw_article_bodies"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("raw_articles.id", ondelete="CASCADE"), primary_key=True
    )
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 'rss_content_encoded' | 'trafilatura' -- registra COMO o texto foi obtido.
    extraction_method: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)

    article = relationship("Article", back_populates="body")


class Price(Base):
    """OHLCV diario. Cobre acoes, ^BVSP e as series macro do Yahoo."""

    __tablename__ = "raw_prices"

    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Double)
    high: Mapped[float | None] = mapped_column(Double)
    low: Mapped[float | None] = mapped_column(Double)
    close: Mapped[float | None] = mapped_column(Double)
    # adj_close e a base de TODO retorno: neutraliza dividendos e desdobramentos.
    adj_close: Mapped[float | None] = mapped_column(Double)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16), nullable=False)


class Macro(Base):
    """Series macroeconomicas do Banco Central (selic, ptax_venda)."""

    __tablename__ = "raw_macro"

    series: Mapped[str] = mapped_column(String(24), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    value: Mapped[float] = mapped_column(Double, nullable=False)


class CollectionRun(Base):
    """Checkpoint de coleta.

    A coleta de noticias sao ~17 mil requisicoes (~10 h). Sem checkpoint, qualquer
    interrupcao obrigaria a recomecar. Uma linha por unidade de trabalho concluida.
    """

    __tablename__ = "raw_collection_runs"
    __table_args__ = (
        UniqueConstraint("collector", "unit_key", name="uq_collection_runs_collector_unit"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collector: Mapped[str] = mapped_column(String(32), nullable=False)
    # Ex.: "PETR4|2024-06-03|pt" -- identifica a unidade de trabalho concluida.
    unit_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    # Marca saturacao do teto de 100 do Google News: indica perda silenciosa se a
    # janela nao tiver sido subdividida.
    hit_cap: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


# --------------------------------------------------------------------------------------
# staging
# --------------------------------------------------------------------------------------
class ArticleTicker(Base):
    """Vinculo N:N noticia-ticker. Uma materia sobre a Vale pode citar Petrobras tambem."""

    __tablename__ = "staging_article_tickers"
    __table_args__ = (
        Index("ix_article_tickers_ticker", "ticker"),
    )

    article_id: Mapped[int] = mapped_column(
        ForeignKey("raw_articles.id", ondelete="CASCADE"), primary_key=True
    )
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    article = relationship("Article", back_populates="tickers")


class ArticleSentiment(Base):
    """Saida do FinBERT para um artigo, sob um modelo especifico.

    A PK inclui o modelo: permite comparar FinBERT-PT-BR e ProsusAI no mesmo artigo
    sem apagar o resultado anterior -- util para a analise de qualidade do TCC.
    """

    __tablename__ = "staging_article_sentiment"
    __table_args__ = (
        CheckConstraint(
            "prob_pos >= 0 AND prob_neu >= 0 AND prob_neg >= 0",
            name="ck_article_sentiment_probs_nonneg",
        ),
    )

    article_id: Mapped[int] = mapped_column(
        ForeignKey("raw_articles.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    prob_pos: Mapped[float] = mapped_column(Double, nullable=False)
    prob_neu: Mapped[float] = mapped_column(Double, nullable=False)
    prob_neg: Mapped[float] = mapped_column(Double, nullable=False)
    # 'title' | 'body' -- sem isto nao da para avaliar se o corpo melhorou o sinal.
    text_source: Mapped[str] = mapped_column(String(8), nullable=False)
    scored_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


# --------------------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------------------
class DatasetV1(Base):
    """As 14 colunas de docs/Dataset1.0.xlsx, uma linha por (data, ticker)."""

    __tablename__ = "features_dataset_v1"

    data: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)

    quant_noticias: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Nulos quando quant_noticias = 0: nao ha sentimento a inventar. A etapa seguinte
    # decide como imputar -- preencher com zero aqui seria mentir sobre o dado.
    positivo: Mapped[float | None] = mapped_column(Double)
    neutro: Mapped[float | None] = mapped_column(Double)
    negativo: Mapped[float | None] = mapped_column(Double)
    score_finbert: Mapped[float | None] = mapped_column(Double)

    retorno_1d: Mapped[float | None] = mapped_column(Double)
    retorno_5d: Mapped[float | None] = mapped_column(Double)
    retorno_20d: Mapped[float | None] = mapped_column(Double)
    volatilidade_20d: Mapped[float | None] = mapped_column(Double)
    volume_relativo: Mapped[float | None] = mapped_column(Double)
    retorno_ibovespa: Mapped[float | None] = mapped_column(Double)

    # Unico campo que olha t+1. Nulo na ultima data disponivel, que e descartada no export.
    target: Mapped[int | None] = mapped_column(SmallInteger)


# --------------------------------------------------------------------------------------
# llm -- analise diaria por Gemini. raw_ = o que veio da rede (prompt e resposta);
# staging_ = o JSON normalizado em linhas. Nada aqui alimenta features_dataset_v1.
# --------------------------------------------------------------------------------------
# `Text` e TEXT de 64 KB no MySQL: uma resposta de ~15 mil tokens passa de 60 KB e seria
# truncada (ou rejeitada em modo estrito). LONGTEXT onde o tamanho e imprevisivel.
LongText = Text().with_variant(mysql.LONGTEXT, "mysql")


class LlmPrompt(Base):
    """Template versionado. Imutavel: mudou o texto, sobe a versao."""
    __tablename__ = "raw_llm_prompts"
    __table_args__ = (UniqueConstraint("nome", "versao", name="uq_llm_prompts_nome_versao"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(64), nullable=False)
    versao: Mapped[int] = mapped_column(Integer, nullable=False)
    texto_template: Mapped[str] = mapped_column(LongText, nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    criado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class LlmRun(Base):
    """Uma linha por tentativa, inclusive as que falharam. Append-only."""
    __tablename__ = "raw_llm_runs"
    __table_args__ = (Index("ix_llm_runs_data_referencia", "data_referencia"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("raw_llm_prompts.id"), nullable=False)
    data_referencia: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # Ultimo pregao <= data_referencia pelo ^BVSP em raw_prices; NULL se nao ha preco.
    pregao_referencia: Mapped[dt.date | None] = mapped_column(Date)
    modelo: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_renderizado: Mapped[str] = mapped_column(LongText, nullable=False)
    resposta_bruta: Mapped[str | None] = mapped_column(LongText)
    # Parseado e validado; NULL quando status != ok. JSON nativo nos dois bancos.
    resposta_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    resumo_geral: Mapped[str | None] = mapped_column(Text)
    # Auditoria do grounding: queries executadas e {url, title} das anotacoes.
    buscas_json: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    citacoes_json: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    tokens_entrada: Mapped[int | None] = mapped_column(Integer)
    tokens_saida: Mapped[int | None] = mapped_column(Integer)
    tokens_pensamento: Mapped[int | None] = mapped_column(Integer)
    latencia_ms: Mapped[int | None] = mapped_column(Integer)
    # ok | erro_api | erro_json | erro_validacao | erro_gravacao
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    erro: Mapped[str | None] = mapped_column(Text)
    iniciado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    finalizado_em: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class LlmNoticiaEmpresa(Base):
    """Noticia por ticker. PK natural: reexecutar o dia substitui, nao duplica."""
    __tablename__ = "staging_llm_noticias_empresas"
    __table_args__ = (Index("ix_llm_noticias_empresas_ticker", "ticker"),)

    data_referencia: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    # url_hash da URL canonicalizada; sem URL, sha256 do titulo normalizado.
    item_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("raw_llm_runs.id", ondelete="CASCADE"), nullable=False
    )
    pregao_referencia: Mapped[dt.date | None] = mapped_column(Date)
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    resumo: Mapped[str] = mapped_column(Text, nullable=False)
    fonte: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    publicado_em: Mapped[dt.date | None] = mapped_column(Date)
    setor: Mapped[str] = mapped_column(String(32), nullable=False)
    sentimento: Mapped[str] = mapped_column(String(16), nullable=False)
    relevancia: Mapped[float] = mapped_column(Double, nullable=False)


class LlmNoticiaMacro(Base):
    __tablename__ = "staging_llm_noticias_macro"

    data_referencia: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    item_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("raw_llm_runs.id", ondelete="CASCADE"), nullable=False
    )
    pregao_referencia: Mapped[dt.date | None] = mapped_column(Date)
    escopo: Mapped[str] = mapped_column(String(8), nullable=False)       # global | brasil
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    resumo: Mapped[str] = mapped_column(Text, nullable=False)
    fonte: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    publicado_em: Mapped[dt.date | None] = mapped_column(Date)
    # Listas do enum como JSON: sem tabela N:N (YAGNI).
    commodities_afetadas: Mapped[list] = mapped_column(JSON, nullable=False)
    direcao_commodity: Mapped[str] = mapped_column(String(16), nullable=False)
    setores_afetados: Mapped[list] = mapped_column(JSON, nullable=False)


class LlmAcaoSinal(Base):
    __tablename__ = "staging_llm_acoes_sinais"
    __table_args__ = (Index("ix_llm_acoes_sinais_ticker", "ticker"),)

    data_referencia: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("raw_llm_runs.id", ondelete="CASCADE"), nullable=False
    )
    pregao_referencia: Mapped[dt.date | None] = mapped_column(Date)
    direcao_esperada: Mapped[str] = mapped_column(String(8), nullable=False)   # alta|baixa|neutra
    confianca: Mapped[float] = mapped_column(Double, nullable=False)
    motivo: Mapped[str] = mapped_column(Text, nullable=False)
    setor: Mapped[str] = mapped_column(String(32), nullable=False)
    noticias_relacionadas: Mapped[list] = mapped_column(JSON, nullable=False)


class LlmMercado(Base):
    __tablename__ = "staging_llm_mercados"

    data_referencia: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    nome: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("raw_llm_runs.id", ondelete="CASCADE"), nullable=False
    )
    pregao_referencia: Mapped[dt.date | None] = mapped_column(Date)
    tipo: Mapped[str] = mapped_column(String(16), nullable=False)   # indice|commodity|moeda|setor
    variacao_pct: Mapped[float | None] = mapped_column(Double)
    motivo: Mapped[str] = mapped_column(Text, nullable=False)
