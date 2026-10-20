"""Configuracao central do pipeline.

A carteira do IBOV muda a cada quadrimestre. Congelamos a composicao aqui de proposito:
um dataset de TCC precisa ser reproduzivel, e uma lista que muda sozinha tornaria
impossivel reproduzir um resultado meses depois. Para atualizar a carteira, edite
IBOV_TICKERS e rode `tcc-scraper validate-tickers` para confirmar que todos respondem.

Duas janelas de tempo convivem aqui e nao se misturam:
- janela FIXA (data_start/data_end): o dataset v1 do TCC. Congelada para reprodutibilidade.
- fim ROLANTE (fim_coleta): a coleta diaria avanca ate hoje. Nao muda a janela do dataset.
"""

from __future__ import annotations

import socket
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Configuracao vinda do .env. Nenhum segredo tem default de producao."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # DATA_FIM_COLETA= (vazio) deve significar "ausente", nao erro de parse de data.
        env_ignore_empty=True,
    )

    # Alvo do banco. "postgres" usa database_url; "mysql" monta a URL a partir de
    # ip_banco/user_banco/senha_banco -- assim a senha do servidor fica em um lugar
    # so no .env, em vez de repetida dentro de uma URL.
    db_target: str = "postgres"

    database_url: str = "postgresql+psycopg://tcc:tcc_local_dev@localhost:5433/tcc_scraper"

    ip_banco: str = ""
    user_banco: str = ""
    senha_banco: str = ""
    porta_banco: int = 3306
    nome_banco: str = "tcc_scraper"

    user_agent: str = "TCC-Scraper/1.0 (pesquisa academica)"

    # Segundos entre requisicoes ao mesmo dominio. O valor do Google News foi
    # escolhido por medicao: abaixo disso o risco de bloqueio cresce.
    throttle_google_news: float = 2.0
    throttle_yahoo: float = 1.0
    throttle_bcb: float = 0.5
    throttle_article_fetch: float = 1.5

    # Resolucao do link opaco do Google News. Medido em 2026-09-21 sobre links
    # reais: com 0 s de intervalo a taxa de sucesso foi 100% (o proprio request
    # ja leva ~0,5 s), e com 1,5 s caiu para 96% sem ganho nenhum. 0,4 s e margem
    # de seguranca que quase nao cobra latencia, ja que o request demora mais que
    # isso de qualquer forma.
    throttle_link_resolve: float = 0.4

    # Workers do enriquecimento. O ganho vem de uma assimetria: todo artigo passa
    # pelo news.google.com (que o throttle por dominio serializa), mas o download
    # do corpo se espalha por varios veiculos e corre em paralelo. Acima de ~8 o
    # ganho satura, porque o resolve vira o gargalo.
    enrich_workers: int = 6

    # Janela do DATASET (e da coleta de noticias, que e o custo dominante).
    data_start: date = date(2025, 9, 1)
    data_end: date = date(2026, 9, 1)

    # Os precos sao coletados ANTES de data_start por esta margem. 400 dias corridos
    # (~1 ano) cobrem os 252 pregoes exigidos pela spec, com folga para feriados.
    price_warmup_days: int = 400

    # Fim ROLANTE da coleta diaria. None = hoje (no fuso daily_tz). Fixar uma data
    # reproduz a coleta de um dia especifico; a janela do dataset (data_end) nao muda.
    data_fim_coleta: date | None = None

    brapi_token: str = ""

    # Analise diaria por LLM (comando analyze-llm). A chave NAO e validada aqui:
    # o scraper local roda sem ela; so o job exige e falha com mensagem clara.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    llm_timeout_s: int = 180
    llm_max_attempts: int = 3
    # Fuso da data de referencia. O cron do Railway e UTC; 22:00 UTC = 19:00 em
    # Brasilia (o Brasil nao tem horario de verao desde 2019).
    llm_tz: str = "America/Sao_Paulo"

    hf_cache_dir: Path = Field(default=PROJECT_ROOT / ".hf_cache")
    export_dir: Path = Field(default=PROJECT_ROOT / "exports")

    # Executor (worker que atende o painel). Vazio = valor padrao do executor.
    painel_api_url: str = ""
    executor_token: str = ""
    executor_poll_s: int = 5
    executor_heartbeat_s: int = 15
    # Nome deste executor no painel. Vazio = nome da maquina (ver host_executor).
    executor_host: str = ""

    # Coleta diaria: horario-alvo (HH:MM no daily_tz), orcamento em minutos e hora limite
    # para publicar o dia.
    daily_hora: str = "18:30"
    daily_tz: str = "America/Sao_Paulo"
    daily_orcamento_min: int = 90
    daily_prazo_hora: int = 20

    # Segundos entre requisicoes ao mesmo dominio, para CVM e B3.
    throttle_cvm: float = 1.0
    throttle_b3: float = 1.0

    # Juiza de noticias (LLM).
    juiza_modelo: str = "gemini-3.1-flash-lite"
    juiza_max_itens_dia: int = 2000
    juiza_lote: int = 50
    juiza_confianca_minima: float = 0.5

    # JEV. Desligado por padrao.
    jev_enabled: bool = False
    jev_api_key: str = ""
    jev_modelo: str = "jev-1.13.0"

    # Entrega: filtro pela juiza e pasta dos CSVs exportados.
    entrega_filtro_juiza: bool = False
    entrega_csv_dir: Path = Field(default=PROJECT_ROOT / "exports" / "entrega")

    # Painel: dias de retencao dos logs do executor.
    painel_logs_dias: int = 7


    @property
    def fim_coleta(self) -> date:
        """Fim da coleta diaria: data_fim_coleta se fixada, senao hoje em daily_tz."""
        if self.data_fim_coleta is not None:
            return self.data_fim_coleta
        return datetime.now(ZoneInfo(self.daily_tz)).date()

    @property
    def inicio_noticias(self) -> date:
        """Inicio da coleta de noticias: 31 dias antes de data_start."""
        return self.data_start - timedelta(days=31)

    @property
    def host_executor(self) -> str:
        """Nome deste executor: executor_host, ou o nome da maquina se vazio."""
        return self.executor_host or socket.gethostname()

    @property
    def price_start(self) -> date:
        """Data inicial da coleta de precos: data_start menos o aquecimento."""
        return self.data_start - timedelta(days=self.price_warmup_days)

    @property
    def effective_url(self) -> str:
        """URL que o SQLAlchemy realmente usa.

        Com db_target=mysql, monta a partir das credenciais separadas. A senha passa
        por quote_plus porque caracteres como @ e / a quebrariam dentro da URL.
        charset=utf8mb4 nao e opcional: o `utf8` do MySQL guarda 3 bytes por
        caractere e corrompe emoji e parte dos acentos das manchetes.
        """
        if self.db_target.lower() != "mysql":
            return self.database_url

        faltando = [
            nome for nome, valor in (
                ("IP_BANCO", self.ip_banco),
                ("USER_BANCO", self.user_banco),
                ("SENHA_BANCO", self.senha_banco),
            )
            if not valor
        ]
        if faltando:
            raise ValueError(
                f"DB_TARGET=mysql exige {', '.join(faltando)} no .env"
            )

        senha = quote_plus(self.senha_banco)
        return (
            f"mysql+pymysql://{self.user_banco}:{senha}"
            f"@{self.ip_banco}:{self.porta_banco}/{self.nome_banco}?charset=utf8mb4"
        )


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

# Setor de cada ticker do IBOV, usado no prompt da analise por LLM. Os valores sao
# as strings do enum llm.schema.Setor; config NAO importa de llm (evita ciclo).
IBOV_SECTORS: dict[str, str] = {
    "ABEV3": "alimentos_agro",  # bebidas
    "ALOS3": "construcao_imobiliario",  # shoppings
    "ASAI3": "varejo_consumo",
    "AUAU3": "varejo_consumo",  # Petz, pet shops
    "AURE3": "energia_eletrica",
    "AXIA3": "energia_eletrica",  # ex-Eletrobras
    "AZUL3": "transporte_logistica",
    "AZZA3": "varejo_consumo",  # moda
    "B3SA3": "bancos_financeiro",  # bolsa B3, financeiro
    "BBAS3": "bancos_financeiro",
    "BBDC3": "bancos_financeiro",
    "BBDC4": "bancos_financeiro",
    "BBSE3": "seguros",
    "BEEF3": "alimentos_agro",
    "BPAC11": "bancos_financeiro",
    "BRAP4": "mineracao_siderurgia",  # holding da Vale
    "BRAV3": "petroleo_gas",  # Brava Energia, oleo e gas
    "BRKM5": "petroleo_gas",  # Braskem, petroquimica
    "CMIG4": "energia_eletrica",
    "CMIN3": "mineracao_siderurgia",
    "COGN3": "educacao",
    "CPFE3": "energia_eletrica",
    "CPLE3": "energia_eletrica",
    "CSAN3": "petroleo_gas",  # Cosan, combustiveis
    "CSNA3": "mineracao_siderurgia",
    "CVCB3": "varejo_consumo",  # turismo
    "CYRE3": "construcao_imobiliario",
    "DIRR3": "construcao_imobiliario",
    "EGIE3": "energia_eletrica",
    "EMBJ3": "bens_industriais",  # Embraer
    "ENEV3": "energia_eletrica",
    "ENGI11": "energia_eletrica",
    "EQTL3": "energia_eletrica",
    "FLRY3": "saude",
    "GGBR4": "mineracao_siderurgia",
    "GMAT3": "varejo_consumo",  # Grupo Mateus
    "GOAU4": "mineracao_siderurgia",
    "HAPV3": "saude",
    "HYPE3": "saude",  # Hypera, farmaceutica
    "IGTI11": "construcao_imobiliario",  # shoppings
    "IRBR3": "seguros",  # IRB Brasil RE, resseguro
    "ISAE4": "energia_eletrica",
    "ITSA4": "bancos_financeiro",
    "ITUB4": "bancos_financeiro",
    "JBSS32": "alimentos_agro",  # JBS, proteina
    "KLBN11": "papel_celulose",
    "LREN3": "varejo_consumo",
    "LWSA3": "telecom_tecnologia",  # Locaweb, software
    "MBRF3": "alimentos_agro",  # ex-Marfrig/BRF
    "MGLU3": "varejo_consumo",
    "MOTV3": "transporte_logistica",  # Motiva, rodovias
    "MRVE3": "construcao_imobiliario",
    "MULT3": "construcao_imobiliario",  # shoppings
    "NATU3": "varejo_consumo",  # Natura, cosmeticos
    "PCAR3": "varejo_consumo",  # Pao de Acucar
    "PETR3": "petroleo_gas",
    "PETR4": "petroleo_gas",
    "POMO4": "bens_industriais",  # Marcopolo, onibus
    "PRIO3": "petroleo_gas",
    "PSSA3": "seguros",  # Porto Seguro
    "RADL3": "saude",  # Raia Drogasil
    "RAIL3": "transporte_logistica",  # Rumo, ferrovias
    "RAIZ4": "petroleo_gas",  # Raizen, combustiveis
    "RAPT4": "bens_industriais",  # Randon
    "RDOR3": "saude",
    "RECV3": "petroleo_gas",
    "RENT3": "transporte_logistica",  # Localiza, locacao
    "SANB11": "bancos_financeiro",
    "SAUD3": "saude",
    "SBSP3": "saneamento",
    "SLCE3": "alimentos_agro",
    "SMTO3": "alimentos_agro",  # Sao Martinho, acucar e etanol
    "SUZB3": "papel_celulose",
    "TAEE11": "energia_eletrica",
    "TIMS3": "telecom_tecnologia",
    "TOTS3": "telecom_tecnologia",  # Totvs, software
    "UGPA3": "petroleo_gas",  # Ultrapar, combustiveis
    "USIM5": "mineracao_siderurgia",
    "VALE3": "mineracao_siderurgia",
    "VAMO3": "transporte_logistica",  # Vamos, locacao de caminhoes
    "VBBR3": "petroleo_gas",  # Vibra, distribuicao
    "VIVA3": "varejo_consumo",  # Vivara, joias
    "VIVT3": "telecom_tecnologia",
    "WEGE3": "bens_industriais",
    "YDUQ3": "educacao",
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

# --------------------------------------------------------------------------------------
# Universo historico e entrega
# --------------------------------------------------------------------------------------
# Universo historico: (ticker, entrada, saida). None = antes/depois da janela.
# Datas de saida: spike S5 (TODO).
UNIVERSO_HISTORICO: list[tuple[str, date | None, date | None]] = [
    (t, None, None) for t in IBOV_TICKERS
] + [("CRFB3", None, None), ("STBP3", None, None), ("SRNA3", None, None)]


def universo_coleta() -> list[str]:
    """Todos os tickers que a coleta baixa, sem duplicatas e em ordem."""
    return sorted({t for t, _, _ in UNIVERSO_HISTORICO})


# Trocas de codigo (antigo, novo, data da troca). None ate o levantamento nos avisos da
# B3 (spike S5). A tabela e de troca de codigo, NAO de fusao: fusoes como BRF + Marfrig ->
# MBRF3 (dois codigos antigos para um novo) ficam de fora, porque nao renomeiam um ticker.
MUDANCAS_TICKER: list[tuple[str, str, date | None]] = [
    ("EMBR3", "EMBJ3", None),
    ("ELET3", "AXIA3", None),
    ("ELET6", "AXIA3", None),
    ("JBSS3", "JBSS32", None),
    ("CPLE6", "CPLE3", None),
    ("NTCO3", "NATU3", None),
    ("ODPV3", "SAUD3", None),
    ("AZUL4", "AZUL3", None),
    ("PETZ3", "AUAU3", None),
    ("CCRO3", "MOTV3", None),
]

# (qtd ON, qtd PN) por unit. Conferir no RI (spike S6). So units do universo.
UNITS: dict[str, tuple[int, int]] = {
    "KLBN11": (1, 4),
    "BPAC11": (1, 2),
    "ENGI11": (1, 2),
    "IGTI11": (1, 2),
    "SANB11": (1, 1),
    "TAEE11": (1, 2),
}

# Nome curto e legivel de cada ticker do universo. E o termo de busca principal de quem
# le o painel, por isso sem qualificadores e em caixa normal.
NOMES_EMPRESA: dict[str, str] = {
    "ABEV3": "Ambev",
    "ALOS3": "Allos",
    "ASAI3": "Assai",
    "AUAU3": "Petz",
    "AURE3": "Auren Energia",
    "AXIA3": "Axia Energia",
    "AZUL3": "Azul",
    "AZZA3": "Azzas 2154",
    "B3SA3": "B3",
    "BBAS3": "Banco do Brasil",
    "BBDC3": "Bradesco",
    "BBDC4": "Bradesco",
    "BBSE3": "BB Seguridade",
    "BEEF3": "Minerva",
    "BPAC11": "BTG Pactual",
    "BRAP4": "Bradespar",
    "BRAV3": "Brava Energia",
    "BRKM5": "Braskem",
    "CMIG4": "Cemig",
    "CMIN3": "CSN Mineracao",
    "COGN3": "Cogna",
    "CPFE3": "CPFL Energia",
    "CPLE3": "Copel",
    "CSAN3": "Cosan",
    "CSNA3": "CSN",
    "CVCB3": "CVC",
    "CYRE3": "Cyrela",
    "DIRR3": "Direcional",
    "EGIE3": "Engie",
    "EMBJ3": "Embraer",
    "ENEV3": "Eneva",
    "ENGI11": "Energisa",
    "EQTL3": "Equatorial",
    "FLRY3": "Fleury",
    "GGBR4": "Gerdau",
    "GMAT3": "Grupo Mateus",
    "GOAU4": "Metalurgica Gerdau",
    "HAPV3": "Hapvida",
    "HYPE3": "Hypera",
    "IGTI11": "Iguatemi",
    "IRBR3": "IRB Brasil RE",
    "ISAE4": "ISA Energia",
    "ITSA4": "Itausa",
    "ITUB4": "Itau Unibanco",
    "JBSS32": "JBS",
    "KLBN11": "Klabin",
    "LREN3": "Lojas Renner",
    "LWSA3": "Locaweb",
    "MBRF3": "MBRF",
    "MGLU3": "Magazine Luiza",
    "MOTV3": "Motiva",
    "MRVE3": "MRV",
    "MULT3": "Multiplan",
    "NATU3": "Natura",
    "PCAR3": "Pao de Acucar",
    "PETR3": "Petrobras",
    "PETR4": "Petrobras",
    "POMO4": "Marcopolo",
    "PRIO3": "PRIO",
    "PSSA3": "Porto Seguro",
    "RADL3": "Raia Drogasil",
    "RAIL3": "Rumo",
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
    "TIMS3": "TIM",
    "TOTS3": "Totvs",
    "UGPA3": "Ultrapar",
    "USIM5": "Usiminas",
    "VALE3": "Vale",
    "VAMO3": "Vamos",
    "VBBR3": "Vibra",
    "VIVA3": "Vivara",
    "VIVT3": "Vivo",
    "WEGE3": "WEG",
    "YDUQ3": "Yduqs",
    # Removidas por deslistagem (ver UNIVERSO_HISTORICO).
    "CRFB3": "Carrefour Brasil",
    "STBP3": "Santos Brasil",
    "SRNA3": "Serena Energia",
}

# Marcas e subsidiarias que aparecem no noticiario no lugar do nome da empresa.
APELIDOS: dict[str, list[str]] = {
    "ABEV3": ["Skol", "Brahma", "Antarctica"],
    "AZZA3": ["Arezzo", "Schutz"],
    "BBDC4": ["Bradesco"],
    "COGN3": ["Anhanguera", "Kroton"],
    "CRFB3": ["Atacadao", "Carrefour"],
    "ITUB4": ["Itau", "Itau Unibanco"],
    "JBSS32": ["Seara", "Friboi"],
    "MBRF3": ["Sadia", "Perdigao"],
    "MGLU3": ["Magalu"],
    "PCAR3": ["Extra", "Pao de Acucar"],
    "PETR4": ["Petrobras"],
    "PSSA3": ["Porto"],
    "RADL3": ["Drogasil", "Droga Raia"],
    "RENT3": ["Localiza"],
    "UGPA3": ["Ipiranga", "Ultragaz"],
    "VALE3": ["Vale"],
    "VBBR3": ["BR Mania"],
    "VIVT3": ["Vivo", "Telefonica Brasil"],
    "YDUQ3": ["Estacio"],
}

# Codigo CVM de cada ticker. Gerado por scripts/gerar_mapas_b3_cvm.py (T7), com dois
# ajustes manuais: STBP3 (17892) e SRNA3 (26441) casam pelo nome e pelo issuingCompany da
# B3, mas ja nao trazem otherCodes no detalhe (deslistados). CRFB3 nao foi encontrado na
# busca da B3 e fica de fora (xfail em tests/test_config.py).
TICKER_CODE_CVM: dict[str, int] = {
    "ABEV3": 23264,
    "ALOS3": 22357,
    "ASAI3": 25372,
    "AUAU3": 27766,
    "AURE3": 26620,
    "AXIA3": 2437,
    "AZUL3": 24112,
    "AZZA3": 22349,
    "B3SA3": 21610,
    "BBAS3": 1023,
    "BBDC3": 906,
    "BBDC4": 906,
    "BBSE3": 23159,
    "BEEF3": 20931,
    "BPAC11": 22616,
    "BRAP4": 18724,
    "BRAV3": 25291,
    "BRKM5": 4820,
    "CMIG4": 2453,
    "CMIN3": 25585,
    "COGN3": 17973,
    "CPFE3": 18660,
    "CPLE3": 14311,
    "CSAN3": 19836,
    "CSNA3": 4030,
    "CVCB3": 23310,
    "CYRE3": 14460,
    "DIRR3": 21350,
    "EGIE3": 17329,
    "EMBJ3": 20087,
    "ENEV3": 21237,
    "ENGI11": 15253,
    "EQTL3": 20010,
    "FLRY3": 21881,
    "GGBR4": 3980,
    "GMAT3": 25186,
    "GOAU4": 8656,
    "HAPV3": 24392,
    "HYPE3": 21431,
    "IGTI11": 8672,
    "IRBR3": 24180,
    "ISAE4": 18376,
    "ITSA4": 7617,
    "ITUB4": 19348,
    "JBSS32": 80233,
    "KLBN11": 12653,
    "LREN3": 8133,
    "LWSA3": 24910,
    "MBRF3": 20788,
    "MGLU3": 22470,
    "MOTV3": 18821,
    "MRVE3": 20915,
    "MULT3": 20982,
    "NATU3": 19550,
    "PCAR3": 14826,
    "PETR3": 9512,
    "PETR4": 9512,
    "POMO4": 8451,
    "PRIO3": 22187,
    "PSSA3": 16659,
    "RADL3": 5258,
    "RAIL3": 17450,
    "RAIZ4": 25917,
    "RAPT4": 14109,
    "RDOR3": 24821,
    "RECV3": 25780,
    "RENT3": 19739,
    "SANB11": 20532,
    "SAUD3": 20125,
    "SBSP3": 14443,
    "SLCE3": 20745,
    "SMTO3": 20516,
    "SRNA3": 26441,
    "STBP3": 17892,
    "SUZB3": 13986,
    "TAEE11": 20257,
    "TIMS3": 24929,
    "TOTS3": 19992,
    "UGPA3": 18465,
    "USIM5": 14320,
    "VALE3": 4170,
    "VAMO3": 24716,
    "VBBR3": 24295,
    "VIVA3": 24805,
    "VIVT3": 17671,
    "WEGE3": 5410,
    "YDUQ3": 21016,
}

IVVB11 = "IVVB11"

# Indices: Yahoo e BCB SGS (12 = CDI, 433 = IPCA).
INDICES_YAHOO: dict[str, str] = {"IDIV": "IDIV.SA", "SMLL": "SMLL.SA", "IFIX": "IFIX.SA"}
INDICES_BCB: dict[str, int] = {"cdi": 12, "ipca": 433}

# Series macro gravadas na entrega, com o nome que vai na coluna.
MACRO_ENTREGA: dict[str, str] = {"^BVSP": "IBOV", "USDBRL=X": "USDBRL", "BZ=F": "BRENT"}

# Contas das demonstracoes CVM usadas na entrega: demonstracao -> codigo -> coluna.
# Bancos usam outro plano: lucro em 3.09 (3.11 nao existe) e PL em 2.08 (2.03 sao passivos).
# Qual codigo vale para cada coluna e decidido pela descricao (builder.py).
CONTAS_CVM: dict[str, dict[str, str]] = {
    "DRE": {
        "3.01": "receita_liquida",
        "3.03": "lucro_bruto",
        "3.09": "lucro_liquido",
        "3.11": "lucro_liquido",
    },
    "BPP": {"2.03": "patrimonio_liquido", "2.08": "patrimonio_liquido"},
}

# Categorias de documentos IPE (CVM) que entram na entrega.
IPE_CATEGORIAS = frozenset({"Fato Relevante", "Comunicado ao Mercado", "Aviso aos Acionistas"})

# Dominios de midia sem texto (video ou audio): nao ha corpo de artigo para extrair.
DOMINIOS_BLOQUEADOS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "spotify.com",
        "open.spotify.com",
        "podcasts.apple.com",
        "tiktok.com",
    }
)

# Padroes de manchete que sao panorama de mercado, nao noticia sobre a empresa.
PADROES_PANORAMA: tuple[str, ...] = (
    r"\bibovespa\s+(abre|fecha|sobe|cai|recua|opera|avan[cç]a)",
    r"\bagenda do dia\b",
    r"\bcarteira(s)? recomendada",
    r"\ba[cç][oõ]es para (comprar|investir)",
    r"\bo que esperar\b",
    r"\bresumo do mercado\b",
    r"\bveja as a[cç][oõ]es\b",
    r"\bfechamento\b.*\bbolsa\b",
    r"\bmaiores (altas|baixas|quedas)\b",
)

# Coletores que rodam na carga historica, na ordem de execucao.
COLETORES_HISTORICO: tuple[str, ...] = (
    "google_news",
    "enriquecimento",
    "finbert",
    "yahoo",
    "cvm_ipe",
    "cvm_itr_dfp",
    "b3_proventos",
)
