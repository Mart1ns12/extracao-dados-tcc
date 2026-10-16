"""Contrato da resposta estruturada do Gemini (nomes pt-BR snake_case, como o dataset).

extra="ignore": um campo a mais inventado pelo modelo nao derruba a analise do dia.
Enum invalido FALHA de proposito -- e o sinal de que prompt ou modelo mudou.
"""
from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Setor(StrEnum):
    bancos_financeiro = "bancos_financeiro"
    seguros = "seguros"
    petroleo_gas = "petroleo_gas"
    mineracao_siderurgia = "mineracao_siderurgia"
    energia_eletrica = "energia_eletrica"
    saneamento = "saneamento"
    varejo_consumo = "varejo_consumo"
    alimentos_agro = "alimentos_agro"
    saude = "saude"
    educacao = "educacao"
    construcao_imobiliario = "construcao_imobiliario"
    transporte_logistica = "transporte_logistica"
    telecom_tecnologia = "telecom_tecnologia"
    papel_celulose = "papel_celulose"
    bens_industriais = "bens_industriais"
    outro = "outro"


class Commodity(StrEnum):
    petroleo = "petroleo"
    gas_natural = "gas_natural"
    minerio_de_ferro = "minerio_de_ferro"
    aco = "aco"
    cobre = "cobre"
    ouro = "ouro"
    soja = "soja"
    milho = "milho"
    acucar = "acucar"
    etanol = "etanol"
    celulose = "celulose"
    carne = "carne"
    cafe = "cafe"
    cambio_dolar = "cambio_dolar"
    juros_selic = "juros_selic"
    juros_fed = "juros_fed"
    outro = "outro"


class Sentimento(StrEnum):
    positivo = "positivo"
    neutro = "neutro"
    negativo = "negativo"


class DirecaoEsperada(StrEnum):
    alta = "alta"
    baixa = "baixa"
    neutra = "neutra"


class DirecaoCommodity(StrEnum):
    alta = "alta"
    baixa = "baixa"
    incerta = "incerta"
    nao_se_aplica = "nao_se_aplica"


class Escopo(StrEnum):
    global_ = "global"
    brasil = "brasil"


class TipoMercado(StrEnum):
    indice = "indice"
    commodity = "commodity"
    moeda = "moeda"
    setor = "setor"


def _ticker_limpo(v: Any) -> Any:
    """' petr4.sa ' -> 'PETR4'. O filtro de universo vem depois, em parse.py."""
    if isinstance(v, str):
        v = v.strip().upper()
        return v[:-3] if v.endswith(".SA") else v
    return v


def _data_ou_none(v: Any) -> Any:
    """Data invalida ('ontem', '') vira None em vez de derrubar a analise inteira."""
    if v in (None, "") or isinstance(v, date):
        return v or None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


class _Item(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NoticiaEmpresa(_Item):
    ticker: str = Field(description="Codigo B3 do universo informado, ex.: PETR4")
    titulo: str = Field(min_length=1)
    resumo: str = Field(description="2-3 frases em pt-BR")
    fonte: str | None = Field(default=None, description="Nome do veiculo")
    url: str | None = Field(default=None, description="URL da materia citada")
    publicado_em: date | None = Field(default=None, description="YYYY-MM-DD ou null")
    setor: Setor
    sentimento: Sentimento
    relevancia: float = Field(ge=0, le=1)

    @field_validator("ticker", mode="before")
    @classmethod
    def limpar_ticker(cls, v: Any) -> Any:
        return _ticker_limpo(v)

    @field_validator("publicado_em", mode="before")
    @classmethod
    def data_ou_none(cls, v: Any) -> Any:
        return _data_ou_none(v)


class NoticiaMacro(_Item):
    escopo: Escopo
    titulo: str = Field(min_length=1)
    resumo: str
    fonte: str | None = None
    url: str | None = None
    publicado_em: date | None = Field(default=None, description="YYYY-MM-DD ou null")
    commodities_afetadas: list[Commodity] = Field(default_factory=list)
    direcao_commodity: DirecaoCommodity = DirecaoCommodity.nao_se_aplica
    setores_afetados: list[Setor] = Field(default_factory=list)

    @field_validator("publicado_em", mode="before")
    @classmethod
    def data_ou_none(cls, v: Any) -> Any:
        return _data_ou_none(v)


class AcaoInfluenciada(_Item):
    ticker: str
    direcao_esperada: DirecaoEsperada
    confianca: float = Field(ge=0, le=1)
    motivo: str
    setor: Setor
    noticias_relacionadas: list[str] = Field(
        default_factory=list,
        description="Titulos exatos das noticias acima que sustentam o sinal",
    )

    @field_validator("ticker", mode="before")
    @classmethod
    def limpar_ticker(cls, v: Any) -> Any:
        return _ticker_limpo(v)


class MercadoEmAlta(_Item):
    nome: str = Field(
        min_length=1,
        description="Ibovespa, S&P 500, Brent, minerio de ferro, dolar...",
    )
    tipo: TipoMercado
    variacao_pct: float | None = Field(default=None, description="Variacao do dia em %, ou null")
    motivo: str


class AnaliseDiaria(_Item):
    data_referencia: date
    resumo_geral: str = Field(description="3-5 frases sobre o dia nos mercados, em pt-BR")
    noticias_empresas: list[NoticiaEmpresa] = Field(default_factory=list)
    noticias_macro: list[NoticiaMacro] = Field(default_factory=list)
    acoes_influenciadas: list[AcaoInfluenciada] = Field(default_factory=list)
    mercados_em_alta: list[MercadoEmAlta] = Field(default_factory=list)


def schema_para_gemini(modelo: type[BaseModel] = AnaliseDiaria) -> dict:
    """model_json_schema() ajustado ao subconjunto que a API do Gemini aceita.

    Remove `title` (ruido) e converte `anyOf: [X, {type: null}]` em `X` + `nullable: true`.
    Se o spike mostrar que a API aceita o schema cru, o ajuste continua inocuo.
    """
    return _limpar(modelo.model_json_schema())


def _limpar(node: Any) -> Any:
    if isinstance(node, list):
        return [_limpar(n) for n in node]
    if not isinstance(node, dict):
        return node
    node = {k: v for k, v in node.items() if k != "title"}
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        outro = next(n for n in any_of if n != {"type": "null"})
        node = {
            **{k: v for k, v in node.items() if k != "anyOf"},
            **outro,
            "nullable": True,
        }
    # Todo campo de objeto vira obrigatorio NO SCHEMA ENVIADO. Medido em 2026-10-15: com
    # listas de default vazio fora de `required`, o Gemini omitia acoes_influenciadas e
    # mercados_em_alta inteiras. Campo opcional continua aceitando null via `nullable`;
    # a validacao local (pydantic) segue tolerante.
    if isinstance(node.get("properties"), dict):
        node["required"] = list(node["properties"])
    return {k: _limpar(v) for k, v in node.items()}
