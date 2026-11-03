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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Modelo da juiza quando JUIZA_MODELO esta vazio, por provedor.
MODELO_PADRAO_JUIZA: dict[str, str] = {
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-5.6-luna",
    "gemini": "gemini-3.1-flash-lite",
}


class Settings(BaseSettings):
    """Configuracao vinda do .env. Nenhum segredo tem default de producao."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # DATA_FIM_COLETA= (vazio) deve significar "ausente", nao erro de parse de data.
        env_ignore_empty=True,
    )

    # Unico banco do projeto: o MySQL 8 do .env. Nao ha banco local. A URL e montada
    # a partir destas variaveis (effective_url), para a senha ficar so no .env.
    ip_banco: str = ""
    user_banco: str = ""
    senha_banco: str = ""
    porta_banco: int = 3306
    nome_banco: str = "tcc_scraper"

    user_agent: str = "TCC-Scraper/1.0 (pesquisa academica)"

    # Segundos entre requisicoes ao mesmo dominio. Google News: 5,0 s desde 2026-10-28.
    # Com 2,0 s, o IP do executor levou HTTP 503 ("trafego incomum") apos ~180 consultas e o
    # bloqueio durou 40 min ou mais. A cada espera soma-se um acrescimo aleatorio de 0 a
    # throttle_google_news_jitter_s, para o ritmo nao virar batida regular.
    throttle_google_news: float = 5.0
    throttle_google_news_jitter_s: float = Field(2.0, ge=0.0)
    throttle_yahoo: float = 1.0
    throttle_bcb: float = 0.5
    throttle_article_fetch: float = 1.5

    # Resolucao do link opaco do Google News. Medido em 2026-09-21 sobre links
    # reais: com 0 s de intervalo a taxa de sucesso foi 100% (o proprio request
    # ja leva ~0,5 s), e com 1,5 s caiu para 96% sem ganho nenhum. 0,4 s e margem
    # de seguranca que quase nao cobra latencia, ja que o request demora mais que
    # isso de qualquer forma.
    throttle_link_resolve: float = 0.4

    # WordPress (InfoMoney, Money Times, Exame): API REST /wp-json/wp/v2/posts. O intervalo
    # de 1,0 s foi medido sem erro em 216 requisicoes em 2026-10-27. Abaixo disso nao foi
    # testado. WP_ENABLED=false desliga a coleta WordPress e devolve esses veiculos ao Google News.
    throttle_wordpress: float = 1.0
    wp_enabled: bool = True

    # Rede dos coletores (Collector.fetch): timeout por requisicao, tentativas em falha
    # transitoria (rede, 429, 5xx) e teto de CADA espera entre tentativas. O Retry-After do
    # servidor e o backoff 2**n nunca dormem mais que isso: pausa e cancelamento so sao
    # atendidos entre unidades.
    http_timeout_s: float = 30.0
    http_max_tentativas: int = 4
    http_espera_max_s: float = 120.0
    # Google News: unidades SEGUIDAS com falha (resposta sem RSS, rede) sao sinal de bloqueio.
    # Ao atingir o limite, a coleta PAUSA (google_news_pausas_min, em minutos, na ordem) em vez de
    # abortar. Depois da ultima pausa, sem nenhum sucesso entre elas, aborta. 0 desliga o disjuntor.
    google_news_max_falhas_seguidas: int = 10
    google_news_pausas_min: str = "30,60,120"
    # Idiomas do Google News: pt, en ou os dois, separados por virgula. en-US era 50% das
    # consultas e 12% das noticias; o bloqueio por IP veio junto. Catalogo: GOOGLE_NEWS_LOCALES.
    google_news_idiomas: str = "pt"

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

    # Analise diaria por LLM (comando analyze-llm). A chave NAO e validada aqui:
    # o scraper local roda sem ela; so o job exige e falha com mensagem clara.
    # Etapa `analise` do daily: desligada ate a troca do Gemini (sem credito desde 2026-10-24)
    # pela OpenAI. O comando analyze-llm continua rodando a mao.
    analise_enabled: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    llm_timeout_s: int = 180
    llm_max_attempts: int = 3
    # Fuso da data de referencia. O cron do Railway e UTC; 22:00 UTC = 19:00 em
    # Brasilia (o Brasil nao tem horario de verao desde 2019).
    llm_tz: str = "America/Sao_Paulo"

    hf_cache_dir: Path = Field(default=PROJECT_ROOT / ".hf_cache")
    export_dir: Path = Field(default=PROJECT_ROOT / "exports")

    # FinBERT: device = auto | cuda | cpu (aceita cuda:N). auto usa GPU se o torch
    # instalado tiver CUDA. Pedir cuda sem CUDA disponivel falha em vez de cair na CPU.
    # batch_size: com GPU, 128 costuma caber em 6 GB de VRAM.
    sentiment_device: str = "auto"
    sentiment_batch_size: int = 32

    # Executor (worker que atende o painel). Vazio = valor padrao do executor.
    painel_api_url: str = ""
    executor_token: str = ""
    executor_poll_s: int = 5
    executor_heartbeat_s: int = 15
    # Nome deste executor no painel. Vazio = nome da maquina (ver host_executor).
    executor_host: str = ""

    # Coleta diaria: horario-alvo (HH:MM no daily_tz), orcamento em minutos e hora limite
    # para publicar o dia.
    daily_hora: str = "08:00"
    daily_tz: str = "America/Sao_Paulo"
    daily_orcamento_min: int = 90
    daily_prazo_hora: int = 20

    # Segundos entre requisicoes ao mesmo dominio, para CVM e B3.
    throttle_cvm: float = 1.0
    throttle_b3: float = 1.0

    # Juiza de noticias (LLM). Modelo vazio = padrao do provedor (MODELO_PADRAO_JUIZA).
    juiza_provedor: Literal["anthropic", "openai", "gemini"] = "anthropic"
    juiza_modelo: str = ""
    juiza_max_itens_dia: int = 2000
    juiza_lote: int = 50
    juiza_confianca_minima: float = 0.5
    # Tentativas por par antes de desistir, pagina de pares por ciclo e precos (US$/MTok).
    # None = tabela do provedor (juiza_custo._PRECOS_PADRAO): anthropic 0.10/0.01/0.50,
    # openai 0.20/0.02/1.20, gemini 0.25/0.25/1.50.
    juiza_max_tentativas: int = 3
    juiza_pagina: int = 5000
    juiza_preco_entrada_usd_mtok: float | None = None
    juiza_preco_saida_usd_mtok: float | None = None
    juiza_preco_cache_usd_mtok: float | None = None
    # Teto mensal (US$) da juiza de producao e verba (US$) do backlog. Somam as runs do banco.
    juiza_orcamento_mensal_usd: float = 3.0
    juiza_backlog_verba_usd: float = 15.0
    # OBSOLETO: so vale se JUIZA_JEV_MODO nao foi definido (true -> decide).
    juiza_jev_decide: bool = False
    # Modo do Jev na juiza: triagem, decide ou entrega (padrao).
    juiza_jev_modo: Literal["triagem", "decide", "entrega"] = "entrega"
    # Confianca minima do Jev no modo entrega.
    juiza_jev_limiar_entrega: float = Field(0.9, gt=0.5, le=1.0)
    juiza_confianca_triagem: float = 0.9

    # JEV. Desligado por padrao.
    jev_enabled: bool = False
    jev_api_key: str = ""
    jev_modelo: str = "jev-1.13.0"
    jev_preco_entrada_usd_mtok: float = 0.042
    jev_lote: int = 50
    jev_concorrencia: int = 8
    jev_timeout_s: float = 30.0
    # Caracteres do corpo do artigo enviados ao Jev.
    jev_trecho: int = Field(1500, ge=300, le=8000)

    # Anthropic: provedor de fallback padrao da juiza (claude-haiku-5-5, effort medium).
    anthropic_api_key: str = ""
    anthropic_timeout_s: float = 120.0
    anthropic_max_attempts: int = 3
    anthropic_effort: str = "medium"
    anthropic_max_output_tokens: int = 16000

    # OpenAI: alternativa da juiza (gpt-5.6-luna, effort none por padrao).
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_s: float = 120.0
    openai_max_attempts: int = 3
    openai_reasoning_effort: str = "none"
    openai_max_output_tokens: int = 16000

    # Entrega: filtro pela juiza e pasta dos CSVs exportados.
    entrega_filtro_juiza: bool = False
    entrega_csv_dir: Path = Field(default=PROJECT_ROOT / "exports" / "entrega")

    # Painel: dias de retencao dos logs do executor.
    painel_logs_dias: int = 7


    @field_validator("google_news_idiomas")
    @classmethod
    def valida_idiomas(cls, valor: str) -> str:
        parse_idiomas(valor)  # levanta ValueError com a mensagem do erro
        return valor

    @field_validator("google_news_pausas_min")
    @classmethod
    def valida_pausas(cls, valor: str) -> str:
        parse_pausas(valor)  # levanta ValueError com a mensagem do erro
        return valor

    @property
    def google_news_locales_ativos(self) -> tuple[dict[str, str], ...]:
        """Locales do Google News usados na coleta, na ordem do catalogo GOOGLE_NEWS_LOCALES."""
        idiomas = parse_idiomas(self.google_news_idiomas)
        return tuple(loc for loc in GOOGLE_NEWS_LOCALES if loc["lang"] in idiomas)

    @property
    def google_news_pausas(self) -> tuple[float, ...]:
        """Pausas do disjuntor do Google News, em minutos, na ordem de uso."""
        return parse_pausas(self.google_news_pausas_min)

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
    def juiza_modelo_efetivo(self) -> str:
        """Modelo da juiza: juiza_modelo, ou o padrao do provedor se vazio."""
        return self.juiza_modelo or MODELO_PADRAO_JUIZA[self.juiza_provedor]

    @property
    def jev_modo_efetivo(self) -> str:
        """Modo do Jev na juiza. juiza_jev_decide so vale se juiza_jev_modo nao foi definido."""
        if "juiza_jev_modo" in self.model_fields_set:
            return self.juiza_jev_modo
        return "decide" if self.juiza_jev_decide else self.juiza_jev_modo

    @property
    def juiza_chave_llm(self) -> str:
        """Chave da API do provedor escolhido em juiza_provedor."""
        if self.juiza_provedor == "anthropic":
            return self.anthropic_api_key
        if self.juiza_provedor == "openai":
            return self.openai_api_key
        return self.gemini_api_key

    @property
    def effective_url(self) -> str:
        """URL que o SQLAlchemy realmente usa: sempre o MySQL do .env.

        Montada por URL.create, que escapa a senha sozinho (caracteres como @ e / a
        quebrariam dentro de uma string). charset=utf8mb4 nao e opcional: o `utf8` do
        MySQL guarda 3 bytes por caractere e corrompe emoji e parte dos acentos das
        manchetes.
        """
        faltando = [
            nome for nome, valor in (
                ("IP_BANCO", self.ip_banco),
                ("USER_BANCO", self.user_banco),
                ("SENHA_BANCO", self.senha_banco),
            )
            if not valor
        ]
        if faltando:
            raise ValueError(f"o .env precisa de {', '.join(faltando)} para conectar ao banco")

        url = URL.create(
            "mysql+pymysql",
            username=self.user_banco,
            password=self.senha_banco,
            host=self.ip_banco,
            port=self.porta_banco,
            database=self.nome_banco,
            query={"charset": "utf8mb4"},
        )
        return url.render_as_string(hide_password=False)


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


@dataclass(frozen=True)
class CorrecaoPrecoB3:
    """Janela em que o Yahoo erra o preco de um papel e a serie da B3 substitui a dele.

    codigos: codigos negociados na janela (COTAHIST, mercado a vista). isin: o do papel,
    para achar os grupamentos no suplemento. inicio e fim: datas inclusivas.
    """

    codigos: tuple[str, ...]
    isin: str
    inicio: date
    fim: date


# Papeis em que o Yahoo emenda ativos diferentes e fica corrompido (medido em 2026-10-26).
# AZUL3 (Yahoo: 41,25 -> 7,42 -> 341 -> 28 milhoes entre 2026-01-13 e 2026-04-17): nessa
# janela a ON negociou como AZUL53, cotada por lote de 1.000.000 de acoes. Fora dela, o
# Yahoo bate com a B3 e nao e corrigido. Outro papel com o mesmo problema precisa entrar
# aqui e ter o CSV gerado por `gerar-correcao-precos`.
CORRECOES_PRECO_B3: dict[str, CorrecaoPrecoB3] = {
    "AZUL3": CorrecaoPrecoB3(
        codigos=("AZUL53",),
        isin="BRAZULA01OR8",
        inicio=date(2026, 1, 13),
        fim=date(2026, 4, 17),
    ),
}


# Linhas de raw_cvm_acoes que a entrega anula (qtd NULL, situacao 'anulado'). Conferido no
# banco em 2026-11-02. Chave: (cnpj, dt_refer). Erro de digito 10x/100x NAO entra aqui
# (correcao automatica).
ACOES_CVM_ANULADAS: dict[tuple[str, date], str] = {
    ("24990777000109", date(2023, 6, 30)): "GMAT3: 0,21x dos vizinhos, sem evento",
    ("42500384000151", date(2021, 6, 30)): "SRNA3: holding pre-reorganizacao (10 acoes)",
    ("42500384000151", date(2021, 9, 30)): "SRNA3: holding pre-reorganizacao (100 acoes)",
    ("60543816000193", date(2021, 3, 31)): "IGTI11: base pre-reorganizacao 2021 (hipotese)",
    ("60543816000193", date(2021, 6, 30)): "IGTI11: base pre-reorganizacao 2021 (hipotese)",
    ("60543816000193", date(2021, 9, 30)): "IGTI11: base pre-reorganizacao 2021 (hipotese)",
    ("53153938000108", date(2023, 12, 31)): "AUAU3: pre-listagem (hipotese)",
    ("53153938000108", date(2024, 3, 31)): "AUAU3: pre-listagem (hipotese)",
    ("53153938000108", date(2024, 6, 30)): "AUAU3: pre-listagem (hipotese)",
    ("53153938000108", date(2024, 9, 30)): "AUAU3: pre-listagem (hipotese)",
    ("53153938000108", date(2024, 12, 31)): "AUAU3: pre-listagem (hipotese)",
}


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

GOOGLE_NEWS_LOCALES: tuple[dict[str, str], ...] = (
    {"lang": "pt", "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"},
    {"lang": "en", "hl": "en-US", "gl": "US", "ceid": "US:en"},
)


def parse_idiomas(valor: str) -> tuple[str, ...]:
    """Idiomas de GOOGLE_NEWS_IDIOMAS (ex.: "pt" ou "pt,en"), na ordem do catalogo.

    Lista vazia ou idioma fora do catalogo levantam ValueError com a mensagem a mostrar.
    """
    validos = [loc["lang"] for loc in GOOGLE_NEWS_LOCALES]
    pedidos = [parte.strip().lower() for parte in valor.split(",") if parte.strip()]
    if not pedidos:
        raise ValueError(
            f"GOOGLE_NEWS_IDIOMAS vazio: informe ao menos um de {', '.join(validos)}"
        )
    invalidos = [parte for parte in pedidos if parte not in validos]
    if invalidos:
        raise ValueError(
            f"GOOGLE_NEWS_IDIOMAS invalido: {', '.join(invalidos)} "
            f"(validos: {', '.join(validos)})"
        )
    return tuple(lang for lang in validos if lang in pedidos)


def parse_pausas(valor: str) -> tuple[float, ...]:
    """Pausas do disjuntor do Google News em minutos (GOOGLE_NEWS_PAUSAS_MIN, ex.: "30,60,120").

    Vazia, nao numerica ou com valor <= 0 levanta ValueError.
    """
    partes = [parte.strip() for parte in valor.split(",") if parte.strip()]
    if not partes:
        raise ValueError(
            "GOOGLE_NEWS_PAUSAS_MIN vazio: informe ao menos uma pausa em minutos, ex.: 30,60,120"
        )
    try:
        pausas = tuple(float(parte) for parte in partes)
    except ValueError as exc:
        raise ValueError(
            f"GOOGLE_NEWS_PAUSAS_MIN invalido: {valor!r} "
            "(minutos separados por virgula, ex.: 30,60,120)"
        ) from exc
    if any(not 0 < pausa < float("inf") for pausa in pausas):
        raise ValueError(
            f"GOOGLE_NEWS_PAUSAS_MIN invalido: cada pausa deve ser maior que zero ({valor!r})"
        )
    return pausas

# Teto duro de itens que o Google News RSS devolve por consulta. Confirmado por
# medicao: janela mensal satura em 100, semanal devolve ~52. Ao bater esse valor
# a janela e subdividida, senao ha perda silenciosa de noticias.
GOOGLE_NEWS_ITEM_CAP = 100

# --------------------------------------------------------------------------------------
# Veiculos com API WordPress (coletados direto, sem passar pelo Google News)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SiteWP:
    """Veiculo WordPress. nome: chave do site (raw_wp_posts.site). fonte: nome exibido
    (raw_articles.source_name). dominio: dominio canonico (source_domain). endpoint: API REST."""

    nome: str
    fonte: str
    dominio: str
    endpoint: str


WP_SITES: tuple[SiteWP, ...] = (
    SiteWP(
        nome="infomoney",
        fonte="InfoMoney",
        dominio="infomoney.com.br",
        endpoint="https://www.infomoney.com.br/wp-json/wp/v2/posts",
    ),
    SiteWP(
        nome="moneytimes",
        fonte="Money Times",
        dominio="moneytimes.com.br",
        endpoint="https://www.moneytimes.com.br/wp-json/wp/v2/posts",
    ),
    # A barra final e obrigatoria: sem ela o Exame responde 308.
    SiteWP(
        nome="exame",
        fonte="Exame",
        dominio="exame.com",
        endpoint="https://exame.com/wp-json/wp/v2/posts/",
    ),
)

# Dominios cobertos pela API WordPress. O Google News descarta esses itens quando wp_enabled.
DOMINIOS_WP: frozenset[str] = frozenset(s.dominio for s in WP_SITES)

# --------------------------------------------------------------------------------------
# Universo historico e entrega
# --------------------------------------------------------------------------------------
# Tickers deslistados: sem cadastro atual na B3. validate-cadastro trata a falha deles como aviso.
TICKERS_DESLISTADOS: frozenset[str] = frozenset({"CRFB3", "STBP3", "SRNA3"})

# Universo historico: (ticker, entrada, saida). None = antes/depois da janela.
# Datas de saida: spike S5 (TODO).
UNIVERSO_HISTORICO: list[tuple[str, date | None, date | None]] = [
    (t, None, None) for t in IBOV_TICKERS
] + [("CRFB3", None, None), ("STBP3", None, None), ("SRNA3", None, None)]


def universo_coleta() -> list[str]:
    """Todos os tickers que a coleta baixa, sem duplicatas e em ordem."""
    return sorted({t for t, _, _ in UNIVERSO_HISTORICO})


# Trocas de codigo (antigo, novo, data da troca). A data e o primeiro pregao com o codigo
# novo. Fontes: comunicados das companhias e avisos da B3, conferidos na imprensa (2026-10-23).
# A tabela e de troca de codigo, NAO de fusao: fusoes como BRF + Marfrig -> MBRF3 (dois
# codigos antigos para um novo) ficam de fora, porque nao renomeiam um ticker.
# Tres trocas passaram por codigo intermediario, que fica fora da tabela:
#   ELET6 -> AXIA6 (2025-11-10) -> AXIA3 (2026-06-08, conversao 1,1 ON por PN)
#   CPLE6 -> CPLE5 (2025-11-10) -> CPLE3 (2025-12-22, migracao ao Novo Mercado)
#   AZUL4 -> AZUL54/AZUL53 (jan/2026) -> AZUL3 (2026-04-20, grupamento 150.000:1)
MUDANCAS_TICKER: list[tuple[str, str, date | None]] = [
    ("EMBR3", "EMBJ3", date(2025, 11, 3)),
    ("ELET3", "AXIA3", date(2025, 11, 10)),
    ("ELET6", "AXIA3", date(2026, 6, 8)),
    ("JBSS3", "JBSS32", date(2025, 6, 9)),
    ("CPLE6", "CPLE3", date(2025, 12, 22)),
    ("NTCO3", "NATU3", date(2025, 7, 2)),
    ("ODPV3", "SAUD3", date(2026, 5, 5)),
    ("AZUL4", "AZUL3", date(2026, 4, 20)),
    ("PETZ3", "AUAU3", date(2026, 1, 5)),
    ("CCRO3", "MOTV3", date(2025, 5, 2)),
]

# Rebalanceamentos do IBOV depois da carteira oficial de set/2024 (a ultima do repositorio
# paccafelipe). Fonte: comunicados da B3 e imprensa, conferidos em 2026-10-23. Cada tupla e
# (vigencia a partir de, entram, saem). O numero no fim e a quantidade de papeis que a B3 diz.
# Trocas de MUDANCAS_TICKER que um evento explicito ja trata (o antigo sai ou o novo entra
# no mesmo par) sao ignoradas; ver composicao_ibov.eventos_trocas.
REBALANCEAMENTOS_IBOV: list[tuple[date, tuple[str, ...], tuple[str, ...]]] = [
    (date(2025, 1, 6), ("POMO4", "PSSA3"), ("ALPA4", "EZTC3")),  # 87
    (date(2025, 5, 5), ("DIRR3", "SMFT3"), ("AMOB3", "LWSA3")),  # 87
    (date(2025, 9, 1), ("CURY3", "CEAB3"), ("SMTO3", "PETZ3")),  # 84
    # AXIA7, RENT4 e CYRE4 entram por proventos em acoes (ver comunicado da data).
    (date(2026, 1, 5), ("CSMG3", "AXIA7", "RENT4", "CYRE4"), ("CVCB3",)),  # 85
    (date(2026, 5, 4), (), ("AXIA7", "CYRE4", "IRBR3", "RENT4")),  # 79
    (date(2026, 9, 8), ("TEND3",), ("RECV3", "SLCE3")),  # 76
]

# Eventos da carteira que nao sao rebalanceamento: fusoes, cisoes, recuperacoes, OPAs,
# deslistagens e trocas de codigo com mais de um papel. Mesmo formato de REBALANCEAMENTOS_IBOV.
# Fonte: comunicados da B3 e das companhias, conferidos na imprensa em 2026-10-23. A data e o
# primeiro pregao em que a mudanca vale. Papel que sai sem estar na carteira gera aviso.
EVENTOS_IBOV_EXTRAORDINARIOS: list[tuple[date, tuple[str, ...], tuple[str, ...]]] = [
    (date(2024, 9, 9), ("BRAV3",), ("RRRP3",)),  # 3R vira Brava: troca de codigo
    (date(2024, 11, 18), ("ISAE4",), ("TRPL4",)),  # ISA CTEEP vira ISA Energia: troca de codigo
    (date(2024, 12, 16), ("AMOB3",), ()),  # cisao da Vamos: inclusao automatica
    (date(2025, 5, 30), (), ("AZUL4",)),  # recuperacao judicial; exclusao no fechamento de 29/05
    (date(2025, 6, 2), (), ("CRFB3",)),  # deslistagem; ultimo pregao 30/05
    (date(2025, 6, 9), (), ("JBSS3",)),  # vira BDR JBSS32, que nao entra no indice
    (date(2025, 9, 23), ("MBRF3",), ("BRFS3", "MRFG3")),  # fusao BRF + Marfrig
    (date(2025, 10, 6), (), ("STBP3",)),  # OPA e deslistagem; ultimo pregao 03/10
    # ELET5 -> AXIA5 nao se aplica: ELET5 nunca esteve na base (set/2024).
    (date(2025, 11, 10), ("AXIA3", "AXIA6", "CPLE5"), ("ELET3", "ELET6", "CPLE6")),
    (date(2025, 12, 22), ("CPLE3",), ("CPLE5",)),  # migracao ao Novo Mercado
    (date(2026, 3, 12), (), ("PCAR3",)),  # recuperacao extrajudicial; ultimo pregao 11/03
    (date(2026, 3, 13), (), ("RAIZ4",)),  # ultimo pregao 12/03
    # AXIA6 convertida em AXIA3, que ja esta. AXIA5 nunca entrou (ELET5 nao estava na base).
    (date(2026, 6, 8), (), ("AXIA6",)),
    (date(2026, 8, 26), (), ("BRKM5",)),  # recuperacao extrajudicial; exclusao em 25/08
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
# Qual codigo vale para cada coluna e decidido pela descricao (builder.py). Emprestimos somam
# emprestimos (.01) e debentures (.02); o arrendamento (.03) fica fora, como pede a spec.
CONTAS_CVM: dict[str, dict[str, str]] = {
    "DRE": {
        "3.01": "receita_liquida",
        "3.03": "lucro_bruto",
        "3.05": "ebit",
        "3.09": "lucro_liquido",
        "3.11": "lucro_liquido",
    },
    "BPA": {"1.01.01": "caixa", "1.01.02": "aplicacoes_financeiras"},
    "BPP": {
        "2.03": "patrimonio_liquido",
        "2.08": "patrimonio_liquido",
        "2.01.04.01": "emprestimos_curto_prazo",
        "2.01.04.02": "emprestimos_curto_prazo",
        "2.02.01.01": "emprestimos_longo_prazo",
        "2.02.01.02": "emprestimos_longo_prazo",
    },
}
# Contas que vao ao raw sem entrar em CONTAS_CVM (so o filtro do coletor as ve). CSN, Sabesp,
# Rede D'Or, Magalu e CMIN lancam a divida so na conta-pai e deixam as subcontas zeradas; o
# arrendamento (.03) tira o que nao e emprestimo do pai. O builder decide o uso, nunca a soma.
CONTAS_CVM_AUXILIARES: dict[str, set[str]] = {
    "BPP": {"2.01.04", "2.02.01", "2.01.04.03", "2.02.01.03"},
}

# DFC metodo indireto: ajustes ao lucro (6.01.01.NN) nao tem codigo padronizado; a D&A e
# escolhida pela descricao no builder. So o 4o nivel, para nao somar pai e filho.
PREFIXO_AJUSTES_DFC = "6.01.01."


def conta_ajuste_dfc(cd_conta: str) -> bool:
    """True para 6.01.01.NN (4 niveis): as linhas de ajuste ao lucro da DFC indireta."""
    return cd_conta.startswith(PREFIXO_AJUSTES_DFC) and cd_conta.count(".") == 3


# Bancos e seguradoras (codigo CVM): ebit, D&A, emprestimos, caixa, aplicacoes e lucro_bruto
# ficam em branco (spec). Lista explicita porque a descricao nao basta: BBSE3 e IRBR3 tem
# "Caixa e Equivalentes de Caixa" como a industria, e PSSA3 usa o plano industrial inteiro
# (conferido no ITR 2025 da CVM). ITSA4 (holding) e B3SA3 (bolsa) usam o plano industrial.
CD_CVM_BANCOS_SEGURADORAS: frozenset[int] = frozenset({
    906,    # BBDC3/BBDC4 Bradesco
    1023,   # BBAS3 Banco do Brasil
    19348,  # ITUB4 Itau Unibanco
    20532,  # SANB11 Santander
    22616,  # BPAC11 BTG Pactual
    23159,  # BBSE3 BB Seguridade
    16659,  # PSSA3 Porto Seguro
    24180,  # IRBR3 IRB
})

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
