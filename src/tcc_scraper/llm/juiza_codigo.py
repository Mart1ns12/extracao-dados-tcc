"""Classificador por codigo das noticias WordPress (plano hibrido, fase F5).

Decide SEM LLM o par (post, ticker) que a evidencia do casador ja resolve. O que sobra vai para a
juiza (regra barata -> Jev -> Luna), marcado pela camada "R" (revisavel) ou pelas camadas com
julgamento None ("T", "K4", "K7").

Funcoes puras: sem banco e sem rede. O julgamento tem o MESMO formato que
`llm/juiza_regras.regra_barata` (papel, tipo_conteudo, evento, qualidade, confianca,
tickers_adicionais), para a gravacao seguir o mesmo caminho (`juiza._linha_staging`).
Os valores de papel, tipo_conteudo e evento sao os de `llm/juiza_schema.py`.

Mapeamento de evento (palavra-chave no titulo normalizado, singular e plural; vale o primeiro
grupo que casar, na ordem abaixo; sem casamento = outro):
  resultado       <- lucro | prejuizo | balanco | resultado | ebitda | receita liquida | 1t25..4t25
  dividendo_jcp   <- dividendo(s) | jcp | juros sobre (o) capital | proventos | bonificacao
  fusao_aquisicao <- aquisicao | adquire | fusao | incorporacao
                     | venda de (ativo|participacao|fatia)
                     | opa | oferta publica | follow-on | ipo | desinvestimento
  recomendacao    <- recomenda* | preco.alvo | rebaixa | carteira | outperform | underperform
  guidance        <- guidance | projecao | projecoes | meta de
  governanca      <- ceo | cfo | presidente | conselho | diretor | renuncia
                     | assembleia | controlador
  juridico        <- justica | processo | stf | stj | cade | multa | investigacao | condena* |
                     recuperacao judicial
  regulatorio     <- aneel | anatel | anp | cvm | ans | antt | regulador* | tarifa | concessao
  operacional     <- producao | contrato | investimento | entregas? | vendas | expansao | fabrica |
                     usina | lancamento
  outro           <- nenhum dos anteriores

Camadas, na ordem (a primeira que casar decide):
  R   par nao vincula, mas vincularia sem E1-E4/E6 -> julgamento None (juiza)
  K1  URL com segmento conteudo-de-marca, patrocinado, publieditorial ou branded-content
      -> mencao / patrocinado / outro, 0.0 / 1.0
  K2  regra_barata devolve julgamento (panorama, podcast/video) -> o proprio julgamento;
      senao o titulo casa PADROES_LISTA -> mencao / lista / outro, 0.1 / 0.9
  T   titulo casa TIPO_TITULO (opiniao, lista, resumo de mercado, agenda) -> None (juiza)
  K3  n_titulo >= 1 e no maximo 1 outra empresa no titulo -> protagonista / reportagem /
      evento por palavra-chave, 0.7 / 0.9
  K4  n_titulo >= 1 com 2 ou mais outras empresas no titulo -> None (juiza)
  K6  n_titulo == 0, n_corpo == MIN_CORPO e primeiro bloco >= LEAD -> mencao / reportagem /
      outro, 0.2 / 0.85
  K7  o resto -> None (juiza)

Camadas com julgamento de codigo: CAMADAS_ATIVAS. As demais devolvem None, mas a camada
continua informada na tupla. Os rotulos gravados em `modelo` seguem `rotulo_modelo`.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from tcc_scraper.enrich.casador import MIN_CORPO, EvidenciaPar, sem_acento
from tcc_scraper.llm.juiza_regras import regra_barata

REGRAS_VERSAO = 1
CAMADAS_ATIVAS = frozenset({"K1", "K2", "K3", "K6"})

# Primeiro bloco com mencao a partir do qual o corpo fraco (K6) e rejeitado.
LEAD = 3

_SEGMENTOS_MARCA = frozenset(
    {"conteudo-de-marca", "patrocinado", "publieditorial", "branded-content"}
)

# Titulo de lista ou calendario (K2, depois do regra_barata). Titulo ja normalizado.
PADROES_LISTA: tuple[str, ...] = (
    r"acoes para acompanhar",
    r"e outros destaques",
    r"e mais acoes",
    r"mini ?(dolar|indice)",
    r"ibovespa futuro",
    r"dividendos? (nesta|na) semana",
    r"calendario de (dividendos|proventos)",
    r"\d+ coisas para saber",
)

# Titulo que so duvidoso: opiniao, lista, resumo de mercado e agenda (camada T -> juiza).
TIPO_TITULO: tuple[str, ...] = (
    # opiniao
    r"^day trade",
    r"^swing trade",
    r"\bcompre\b",
    r"preco.alvo",
    r"\brecomenda(cao|coes|m)?\b",
    r"(eleva|corta|rebaixa|reitera|mantem|inicia) (a )?(recomendacao|cobertura)",
    r"\b(podem?|deve|devem) (subir|cair|disparar|deixar|valorizar|dobrar)",
    r"vale a pena",
    r"\baposta\b",
    (
        r"segundo (a |o )?(agora|xp|btg|itau bba|bradesco bbi|safra|genial|ubs|"
        r"goldman|jpmorgan|morgan stanley)"
    ),
    # lista
    r"mais lidas",
    r"\be mais( \d+)? (acoes|empresas|destaques|nomes)",
    r"\be outras? \d+ (acoes|empresas)",
    r"\b\d+ acoes\b",
    r"(melhores|piores) acoes",
    r"acoes (mais )?indicadas",
    r"carteira (recomendada|semanal|mensal|de dividendos)",
    r"\b(top|ranking) \d+",
    r"^de .+ (a|ao) .+[:;]",
    r"^do .+ (a|ao) .+[:;]",
    # resumo de mercado
    r"^(tempo real|ibovespa|ibov|bolsa|fechamento|minidolar|mini indice)\b",
    (
        r"\bibovespa (fecha|sobe|cai|recua|avanca|abre|tomba|despenca|dispara|reage|"
        r"devolve|dribla|opera|renova|zera|ensaia|surpreende|afunda)"
    ),
    r"\b(fecham|fecha|fechou) (em |com )?(alta|queda|baixa)",
    r"destaques? (desta|deste|do dia|da semana|de hoje)",
    r"sao (alguns dos|os) destaques",
    r"mexe(r)? com (os )?mercados?",
    r"\d+ (coisas|assuntos) para saber",
    r"(bolsa|mercado|acoes) hoje",
    r"radar do mercado",
    # agenda
    r"(divulga|divulgam) (seus )?(balanco|balancos|resultados)",
    r"\bagenda\b",
    r"calendario",
    r"o que esperar",
)

# Evento por palavra-chave, na ordem de prioridade (ver o mapeamento no docstring).
_EVENTOS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (evento, re.compile(padrao))
    for evento, padrao in (
        (
            "resultado",
            (
                r"\b(?:lucros?|prejuizos?|balancos?|resultados?|ebitda|"
                r"receitas? liquidas?|[1-4]t\d{2})\b"
            ),
        ),
        (
            "dividendo_jcp",
            (
                r"\b(?:dividendos?|jcp|juros sobre (?:o )?capital|proventos|"
                r"bonificac(?:ao|oes))\b"
            ),
        ),
        (
            "fusao_aquisicao",
            (
                r"\b(?:aquisic(?:ao|oes)|adquire|fusoes?|incorporac(?:ao|oes)|"
                r"venda de (?:ativo|participacao|fatia)s?|opas?|ofertas? publicas?|"
                r"follow-ons?|ipos?|desinvestimentos?)\b"
            ),
        ),
        (
            "recomendacao",
            r"\b(?:recomenda\w*|precos?.alvos?|rebaixa|carteiras?|outperform|underperform)\b",
        ),
        ("guidance", r"\b(?:guidance|projec(?:ao|oes)|metas? de)\b"),
        (
            "governanca",
            (
                r"\b(?:ceos?|cfos?|presidentes?|conselhos?|diretor(?:es)?|"
                r"renuncias?|assembleias?|controladores?)\b"
            ),
        ),
        (
            "juridico",
            (
                r"\b(?:justicas?|processos?|stf|stj|cade|multas?|investigac(?:ao|oes)|"
                r"condena\w*|recuperac(?:ao|oes) judicial)\b"
            ),
        ),
        (
            "regulatorio",
            (
                r"\b(?:aneel|anatel|anp|cvm|ans|antt|regulador\w*|"
                r"tarifas?|concess(?:ao|oes))\b"
            ),
        ),
        (
            "operacional",
            (
                r"\b(?:producao|producoes|contratos?|investimentos?|entregas?|vendas?|"
                r"expans(?:ao|oes)|fabricas?|usinas?|lancamentos?)\b"
            ),
        ),
    )
)


def rotulo_modelo(camada: str) -> str:
    """Valor gravado em `modelo`. Cabe em String(64) de v1-staging_noticias_julgamento."""
    return f"codigo/r{REGRAS_VERSAO}/{camada}"


def _julgamento(papel: str, tipo: str, evento: str, qualidade: float, confianca: float) -> dict:
    return {
        "papel": papel,
        "tipo_conteudo": tipo,
        "evento": evento,
        "qualidade": qualidade,
        "confianca": confianca,
        "tickers_adicionais": [],
    }


def _evento(titulo_norm: str) -> str:
    for evento, padrao in _EVENTOS:
        if padrao.search(titulo_norm):
            return evento
    return "outro"


def _patrocinado(url: str) -> bool:
    segmentos = sem_acento(urlsplit(url).path).split("/")
    return any(seg in _SEGMENTOS_MARCA for seg in segmentos)


def _decidir(
    ev: EvidenciaPar, artigo: dict, nomes: dict[str, list[str]]
) -> tuple[str, dict | None]:
    """Camada e julgamento para um par que vincula. Sem filtro de CAMADAS_ATIVAS."""
    titulo_norm = sem_acento(str(artigo.get("title") or ""))

    if _patrocinado(str(artigo.get("url") or "")):
        return "K1", _julgamento("mencao", "patrocinado", "outro", 0.0, 1.0)

    regra = regra_barata(artigo, ev.ticker, nomes)
    if regra is not None:
        return "K2", regra
    if any(re.search(padrao, titulo_norm) for padrao in PADROES_LISTA):
        return "K2", _julgamento("mencao", "lista", "outro", 0.1, 0.9)

    if any(re.search(padrao, titulo_norm) for padrao in TIPO_TITULO):
        return "T", None

    if ev.n_titulo >= 1 and ev.outras_empresas_titulo <= 1:
        return "K3", _julgamento("protagonista", "reportagem", _evento(titulo_norm), 0.7, 0.9)
    if ev.n_titulo >= 1:
        return "K4", None

    if (
        ev.n_titulo == 0
        and ev.n_corpo == MIN_CORPO
        and ev.primeiro_bloco is not None
        and ev.primeiro_bloco >= LEAD
    ):
        return "K6", _julgamento("mencao", "reportagem", "outro", 0.2, 0.85)

    return "K7", None


def classificar_par(
    ev: EvidenciaPar, artigo: dict, nomes: dict[str, list[str]]
) -> tuple[str, dict | None]:
    """(camada, julgamento) para o par (post, ticker).

    `artigo` = {"title", "source_domain", "origem", "url"}. `nomes` = saida de
    `llm/juiza_regras.nomes_por_ticker`. Julgamento None = vai para a juiza.
    Par que nao vincula nem e revisavel levanta ValueError: nao deve chegar aqui.
    """
    if not ev.vincula:
        if ev.revisavel:
            return "R", None
        raise ValueError(f"par sem vinculo nem revisavel: {ev.ticker}")

    camada, julgamento = _decidir(ev, artigo, nomes)
    if camada not in CAMADAS_ATIVAS:
        return camada, None
    return camada, julgamento
