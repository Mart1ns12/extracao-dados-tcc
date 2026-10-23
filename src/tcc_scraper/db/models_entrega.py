"""As 9 tabelas de entrega (8 fixas pela spec e a composicao do IBOV), sem prefixo de camada.

Colunas na ordem da spec (parte 2, dicionario de colunas). A `noticias` leva, depois das 10
colunas da spec, as 7 extras da secao 5.1 do desenho. `COLUNAS` e a fonte unica dessa ordem
para o CSV e para os testes.

Double em vez de Float (mesmo motivo de models.py). Datas em Date; data_hora_* em String com
ISO e offset, porque o MySQL nao guarda fuso (ver UTCDateTime para o restante).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Date,
    Double,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tcc_scraper.db.models import PREFIXO_TABELA, Base

COLUNAS: dict[str, list[str]] = {
    "noticias": [
        "id", "ticker", "data_hora_publicacao", "data_hora_coleta", "titulo", "fonte",
        "link", "prob_positivo", "prob_neutro", "prob_negativo",
        "data_publicacao", "momento_pregao", "origem", "papel", "tipo_conteudo",
        "evento", "qualidade",
    ],
    "precos": [
        "data", "ticker", "abertura", "maxima", "minima", "fechamento",
        "fechamento_ajustado", "volume",
    ],
    "cadastro": [
        "ticker", "cnpj", "empresa", "setor", "subsetor", "tipo_acao",
        "unit_qtd_on", "unit_qtd_pn",
    ],
    "mudancas_ticker": ["ticker_antigo", "ticker_novo", "data_mudanca"],
    "balancos": [
        "cnpj", "tipo_documento", "data_inicio_periodo", "data_fim_periodo", "data_entrega",
        "receita_liquida", "lucro_bruto", "lucro_liquido", "patrimonio_liquido",
        "ebit", "depreciacao_amortizacao", "emprestimos_curto_prazo", "emprestimos_longo_prazo",
        "caixa", "aplicacoes_financeiras",
    ],
    "acoes_emitidas": ["cnpj", "data_referencia", "qtd_on", "qtd_pn"],
    "proventos": ["ticker", "tipo", "data_com", "valor_por_acao", "data_pagamento"],
    "indices": ["data", "indice", "valor"],
    "composicao_ibov": ["ticker", "vigencia_inicio", "vigencia_fim", "participacao", "fonte"],
}


class Noticia(Base):
    """Uma linha por (artigo, ticker). Filtro de qualidade e regras na Fase B (desenho 5.1)."""

    __tablename__ = f"{PREFIXO_TABELA}noticias"
    __table_args__ = (
        Index("ix_noticias_ticker_data_publicacao", "ticker", "data_publicacao"),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    data_hora_publicacao: Mapped[str | None] = mapped_column(String(32))
    data_hora_coleta: Mapped[str] = mapped_column(String(32), nullable=False)
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    fonte: Mapped[str | None] = mapped_column(String(255))
    link: Mapped[str | None] = mapped_column(Text)
    prob_positivo: Mapped[float | None] = mapped_column(Double)
    prob_neutro: Mapped[float | None] = mapped_column(Double)
    prob_negativo: Mapped[float | None] = mapped_column(Double)
    # Colunas extras (desenho 5.1)
    data_publicacao: Mapped[dt.date] = mapped_column(Date, nullable=False)
    momento_pregao: Mapped[str] = mapped_column(String(16), nullable=False)
    origem: Mapped[str] = mapped_column(String(16), nullable=False)
    papel: Mapped[str | None] = mapped_column(String(24))
    tipo_conteudo: Mapped[str | None] = mapped_column(String(24))
    evento: Mapped[str | None] = mapped_column(String(24))
    qualidade: Mapped[float | None] = mapped_column(Double)


class Preco(Base):
    __tablename__ = f"{PREFIXO_TABELA}precos"
    __table_args__ = (Index("ix_precos_ticker", "ticker"),)

    data: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    abertura: Mapped[float | None] = mapped_column(Double)
    maxima: Mapped[float | None] = mapped_column(Double)
    minima: Mapped[float | None] = mapped_column(Double)
    fechamento: Mapped[float | None] = mapped_column(Double)
    fechamento_ajustado: Mapped[float | None] = mapped_column(Double)
    volume: Mapped[int | None] = mapped_column(BigInteger)


class Cadastro(Base):
    __tablename__ = f"{PREFIXO_TABELA}cadastro"

    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    cnpj: Mapped[str | None] = mapped_column(String(14))
    empresa: Mapped[str] = mapped_column(String(128), nullable=False)
    setor: Mapped[str | None] = mapped_column(String(128))
    subsetor: Mapped[str | None] = mapped_column(String(128))
    tipo_acao: Mapped[str] = mapped_column(String(8), nullable=False)
    unit_qtd_on: Mapped[int | None] = mapped_column(SmallInteger)
    unit_qtd_pn: Mapped[int | None] = mapped_column(SmallInteger)


class MudancaTicker(Base):
    __tablename__ = f"{PREFIXO_TABELA}mudancas_ticker"

    ticker_antigo: Mapped[str] = mapped_column(String(12), primary_key=True)
    ticker_novo: Mapped[str] = mapped_column(String(12), nullable=False)
    data_mudanca: Mapped[dt.date | None] = mapped_column(Date)


class ComposicaoIbov(Base):
    """Um periodo continuo em que o ticker esteve no IBOV. `vigencia_fim` NULL = vigente.

    `participacao` e o peso (%) no inicio da vigencia; NULL quando a fonte nao traz peso.
    `fonte`: b3_api (coletor diario), paccafelipe_csv (carteira oficial) ou rebalanceamento.
    """

    __tablename__ = f"{PREFIXO_TABELA}composicao_ibov"

    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    vigencia_inicio: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    vigencia_fim: Mapped[dt.date | None] = mapped_column(Date)
    participacao: Mapped[float | None] = mapped_column(Double)
    fonte: Mapped[str] = mapped_column(String(32), nullable=False)


class Balanco(Base):
    __tablename__ = f"{PREFIXO_TABELA}balancos"

    cnpj: Mapped[str] = mapped_column(String(14), primary_key=True)
    tipo_documento: Mapped[str] = mapped_column(String(3), primary_key=True)
    data_inicio_periodo: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    data_fim_periodo: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    data_entrega: Mapped[dt.date | None] = mapped_column(Date)
    receita_liquida: Mapped[float | None] = mapped_column(Double)
    lucro_bruto: Mapped[float | None] = mapped_column(Double)
    lucro_liquido: Mapped[float | None] = mapped_column(Double)
    patrimonio_liquido: Mapped[float | None] = mapped_column(Double)
    ebit: Mapped[float | None] = mapped_column(Double)
    depreciacao_amortizacao: Mapped[float | None] = mapped_column(Double)
    emprestimos_curto_prazo: Mapped[float | None] = mapped_column(Double)
    emprestimos_longo_prazo: Mapped[float | None] = mapped_column(Double)
    caixa: Mapped[float | None] = mapped_column(Double)
    aplicacoes_financeiras: Mapped[float | None] = mapped_column(Double)


class AcoesEmitida(Base):
    __tablename__ = f"{PREFIXO_TABELA}acoes_emitidas"

    cnpj: Mapped[str] = mapped_column(String(14), primary_key=True)
    data_referencia: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    qtd_on: Mapped[int | None] = mapped_column(BigInteger)
    qtd_pn: Mapped[int | None] = mapped_column(BigInteger)


class Provento(Base):
    __tablename__ = f"{PREFIXO_TABELA}proventos"
    __table_args__ = (Index("ix_proventos_ticker", "ticker"),)

    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(10), primary_key=True)
    data_com: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    valor_por_acao: Mapped[float] = mapped_column(Double, nullable=False)
    data_pagamento: Mapped[dt.date | None] = mapped_column(Date)


class Indice(Base):
    __tablename__ = f"{PREFIXO_TABELA}indices"
    __table_args__ = (Index("ix_indices_indice", "indice"),)

    data: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    indice: Mapped[str] = mapped_column(String(8), primary_key=True)
    valor: Mapped[float] = mapped_column(Double, nullable=False)
