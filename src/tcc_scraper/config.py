"""Configuracao central do pipeline.

A carteira do IBOV muda a cada quadrimestre. Congelamos a composicao aqui de proposito:
um dataset de TCC precisa ser reproduzivel, e uma lista que muda sozinha tornaria
impossivel reproduzir um resultado meses depois. Para atualizar a carteira, edite
IBOV_TICKERS e rode `tcc-scraper validate-tickers` para confirmar que todos respondem.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Configuracao vinda do .env. Nenhum segredo tem default de producao."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://tcc:tcc_local_dev@localhost:5433/tcc_scraper"

    user_agent: str = "TCC-Scraper/1.0 (pesquisa academica)"

    # Segundos entre requisicoes ao mesmo dominio. O valor do Google News foi
    # escolhido por medicao: abaixo disso o risco de bloqueio cresce.
    throttle_google_news: float = 2.0
    throttle_yahoo: float = 1.0
    throttle_bcb: float = 0.5
    throttle_article_fetch: float = 1.5

    data_start: date = date(2024, 9, 1)
    data_end: date = date(2026, 9, 1)

    brapi_token: str = ""

    hf_cache_dir: Path = Field(default=PROJECT_ROOT / ".hf_cache")
    export_dir: Path = Field(default=PROJECT_ROOT / "exports")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------------------
# Universo de ativos
# --------------------------------------------------------------------------------------
# O nome da empresa nao e decorativo: e o termo principal da busca de noticias.
# O ticker sozinho ("PETR4") traz pouca materia; o nome ("Petrobras") traz a maioria.
# Codigos validados contra a Yahoo chart API em 2026-09-07. Reestruturacoes societarias
# ja aplicadas: Embraer EMBR3->EMBJ3, Eletrobras ELET3/6->AXIA3, JBS JBSS3->JBSS32,
# Copel CPLE6->CPLE3, Natura NTCO3->NATU3, Odontoprev ODPV3->SAUD3, Azul AZUL4->AZUL3,
# Petz PETZ3->AUAU3, CCR CCRO3->MOTV3, BRF+Marfrig -> MBRF3 (fusao).
# Removidas por deslistagem: CRFB3, STBP3, SRNA3.
# O Yahoo migra o historico para o codigo novo (EMBJ3/AXIA3/MBRF3/MOTV3 tem os 2 anos
# completos); NATU3 e a excecao, com serie a partir de 2025-06.
IBOV_TICKERS: dict[str, str] = {
    "ABEV3": "Ambev",
    "ALOS3": "Allos",
    "ASAI3": "Assai Atacadista",
    "AUAU3": "Uniao Pet Petz Cobasi",
    "AURE3": "Auren Energia",
    "AXIA3": "Axia Energia Eletrobras",
    "AZUL3": "Azul Linhas Aereas",
    "AZZA3": "Azzas 2154",
    "B3SA3": "B3 bolsa",
    "BBAS3": "Banco do Brasil",
    "BBDC3": "Bradesco",
    "BBDC4": "Bradesco",
    "BBSE3": "BB Seguridade",
    "BEEF3": "Minerva Foods",
    "BPAC11": "BTG Pactual",
    "BRAP4": "Bradespar",
    "BRAV3": "Brava Energia",
    "BRKM5": "Braskem",
    "CMIG4": "Cemig",
    "CMIN3": "CSN Mineracao",
    "COGN3": "Cogna Educacao",
    "CPFE3": "CPFL Energia",
    "CPLE3": "Copel",
    "CSAN3": "Cosan",
    "CSNA3": "CSN Siderurgica Nacional",
    "CVCB3": "CVC Viagens",
    "CYRE3": "Cyrela",
    "DIRR3": "Direcional Engenharia",
    "EGIE3": "Engie Brasil",
    "EMBJ3": "Embraer",
    "ENEV3": "Eneva",
    "ENGI11": "Energisa",
    "EQTL3": "Equatorial Energia",
    "FLRY3": "Fleury",
    "GGBR4": "Gerdau",
    "GMAT3": "Grupo Mateus",
    "GOAU4": "Metalurgica Gerdau",
    "HAPV3": "Hapvida",
    "HYPE3": "Hypera Pharma",
    "IGTI11": "Iguatemi",
    "IRBR3": "IRB Brasil Resseguros",
    "ISAE4": "ISA Energia Brasil",
    "ITSA4": "Itausa",
    "ITUB4": "Itau Unibanco",
    "JBSS32": "JBS",
    "KLBN11": "Klabin",
    "LREN3": "Lojas Renner",
    "LWSA3": "Locaweb",
    "MBRF3": "MBRF Global Foods BRF Marfrig",
    "MGLU3": "Magazine Luiza",
    "MOTV3": "Motiva",
    "MRVE3": "MRV Engenharia",
    "MULT3": "Multiplan",
    "NATU3": "Natura",
    "PCAR3": "Grupo Pao de Acucar",
    "PETR3": "Petrobras",
    "PETR4": "Petrobras",
    "POMO4": "Marcopolo",
    "PRIO3": "PRIO petroleo",
    "PSSA3": "Porto Seguro",
    "RADL3": "Raia Drogasil",
    "RAIL3": "Rumo logistica",
    "RAIZ4": "Raizen",
    "RAPT4": "Randon",
    "RDOR3": "Rede DOr",
    "RECV3": "PetroReconcavo",
    "RENT3": "Localiza",
    "SANB11": "Santander Brasil",
    "SAUD3": "Odontoprev",
    "SBSP3": "Sabesp",
    "SLCE3": "SLC Agricola",
    "SMTO3": "Sao Martinho",
    "SUZB3": "Suzano",
    "TAEE11": "Taesa",
    "TIMS3": "TIM Brasil",
    "TOTS3": "Totvs",
    "UGPA3": "Ultrapar",
    "USIM5": "Usiminas",
    "VALE3": "Vale",
    "VAMO3": "Vamos locacao",
    "VBBR3": "Vibra Energia",
    "VIVA3": "Vivara",
    "VIVT3": "Vivo Telefonica Brasil",
    "WEGE3": "WEG",
    "YDUQ3": "Yduqs",
}

# Qualificadores para empresas cujo nome e palavra comum em portugues.
#
# Medido no piloto: a busca ("Vale" OR VALE3) devolveu 100 itens -- SATURANDO o teto do
# Google News -- cheios de ruido ("apos resultado recorde, vale investir na Petrobras?").
# Ou seja, o nome generico simultaneamente traz lixo E faz perder noticia real, porque
# o teto corta o excedente. Com contexto setorial: 48 itens, zero panoramas de mercado.
#
# So entram aqui os nomes ambiguos. Empresas com nome distintivo (Petrobras, Embraer,
# Suzano) nao precisam e ficam de fora -- qualificar sem necessidade reduziria a cobertura.
SEARCH_QUALIFIERS: dict[str, str] = {
    "VALE3": "mineracao OR minerio OR mineradora",   # "vale" tambem e verbo
    "RENT3": "aluguel OR locadora OR carros",        # "localiza" tambem e verbo
    "MOTV3": "infraestrutura OR rodovias OR concessao",  # "motiva" tambem e verbo
    "DIRR3": "construtora OR imoveis OR incorporadora",  # "direcional" e adjetivo
    "PSSA3": "seguradora OR seguros",                # Porto Seguro tambem e cidade (BA)
    "SMTO3": "acucar OR etanol OR usina",            # Sao Martinho tambem e cidade
    "ASAI3": "atacarejo OR varejo OR supermercado",  # Assai tambem e cidade (PR)
    "BRAV3": "petroleo OR energia OR oleo",          # "brava" e adjetivo
    "GMAT3": "varejo OR supermercado OR atacarejo",  # "Mateus" e nome proprio comum
    "AURE3": "energia OR eletrica OR geracao",
    "SLCE3": "agricola OR agronegocio OR soja",
}

BENCHMARK_TICKER = "^BVSP"

# Series macro. Brent e dolar vem do Yahoo; Selic e PTAX do Banco Central.
MACRO_YAHOO: dict[str, str] = {"brent": "BZ=F", "usdbrl": "USDBRL=X"}
BCB_SGS_SERIES: dict[str, int] = {"selic": 432}


def yahoo_symbol(ticker: str) -> str:
    """Converte ticker da B3 para o simbolo do Yahoo (PETR4 -> PETR4.SA)."""
    if ticker.startswith("^") or "=" in ticker:
        return ticker
    return f"{ticker}.SA"


# --------------------------------------------------------------------------------------
# Veiculos liberados para extracao do corpo do artigo
# --------------------------------------------------------------------------------------
# Allowlist, nao blocklist: so raspamos o corpo de veiculos cujo robots.txt foi
# verificado manualmente e que nao tem paywall duro. Qualquer outro veiculo
# contribui apenas com a manchete.
ARTICLE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "infomoney.com.br",
        "moneytimes.com.br",
        "cnnbrasil.com.br",
        "agenciabrasil.ebc.com.br",
        "seudinheiro.com",
        "investnews.com.br",
        "suno.com.br",
    }
)

# RSS proprios: entregam URL real e, em varios casos, content:encoded com o texto
# completo -- sem depender do redirect opaco do Google News.
NATIVE_FEEDS: dict[str, str] = {
    "infomoney.com.br": "https://www.infomoney.com.br/feed/",
    "moneytimes.com.br": "https://www.moneytimes.com.br/feed/",
}

GOOGLE_NEWS_LOCALES: tuple[dict[str, str], ...] = (
    {"lang": "pt", "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"},
    {"lang": "en", "hl": "en-US", "gl": "US", "ceid": "US:en"},
)

# Teto duro de itens que o Google News RSS devolve por consulta. Confirmado por
# medicao: janela mensal satura em 100, semanal devolve ~52. Ao bater esse valor
# a janela e subdividida, senao ha perda silenciosa de noticias.
GOOGLE_NEWS_ITEM_CAP = 100
