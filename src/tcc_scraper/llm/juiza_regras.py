"""Regras baratas do juiz: resolvem o obvio sem gastar token da LLM.

Funcoes puras: sem banco e sem rede. A saida usa os mesmos enums de `llm/juiza_schema.py`;
quem grava acrescenta `"modelo": "regra"`. Titulo que nao casa regra nenhuma devolve None
e segue para a LLM.
"""
from __future__ import annotations

import re
import unicodedata

from tcc_scraper.config import DOMINIOS_BLOQUEADOS, PADROES_PANORAMA

# Titulo com tantas empresas distintas (fora o proprio ticker) e panorama de mercado.
_MIN_EMPRESAS_PANORAMA = 3
# Termos com ate essa quantidade de letras so contam com inicial maiuscula no titulo
# original. Sao palavras comuns em portugues (Vale, Azul, Rumo, Vamos, Light).
_MAX_LETRAS_TERMO_CAPITAL = 5


def _sem_acento(texto: str) -> str:
    """Remove acentos e colapsa espacos, preservando maiusculas e minusculas."""
    decomposto = unicodedata.normalize("NFKD", texto)
    sem_marcas = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_marcas.split())


def normalizar(texto: str) -> str:
    """Minusculas, sem acento e com espacos colapsados."""
    return _sem_acento(texto).lower()


def nomes_por_ticker(nomes: dict[str, str], apelidos: dict[str, list[str]]) -> dict[str, list[str]]:
    """Ticker -> [nome, *apelidos], todos normalizados e sem repeticao."""
    saida: dict[str, list[str]] = {}
    for ticker in dict.fromkeys([*nomes, *apelidos]):
        brutos = [nomes[ticker]] if ticker in nomes else []
        brutos += apelidos.get(ticker, [])
        termos: list[str] = []
        for bruto in brutos:
            termo = normalizar(bruto)
            if termo and termo not in termos:
                termos.append(termo)
        if termos:
            saida[ticker] = termos
    return saida


def _termo_aparece(termo: str, titulo: str) -> bool:
    """Casa o termo como palavra inteira. Termo curto so vale com inicial maiuscula."""
    capital_obrigatorio = len(termo.replace(" ", "")) <= _MAX_LETRAS_TERMO_CAPITAL
    padrao = re.compile(r"\b" + re.escape(termo) + r"\b", re.IGNORECASE)
    for achado in padrao.finditer(titulo):
        if not capital_obrigatorio or achado.group(0)[0].isupper():
            return True
    return False


def contar_empresas_no_titulo(
    titulo: str, nomes_por_ticker: dict[str, list[str]], ignorar: str | None = None
) -> int:
    """Quantos tickers distintos (exceto `ignorar`) aparecem no titulo por nome ou apelido."""
    bruto = _sem_acento(titulo)
    contadas = 0
    for ticker, termos in nomes_por_ticker.items():
        if ticker == ignorar:
            continue
        if any(_termo_aparece(termo, bruto) for termo in termos):
            contadas += 1
    return contadas


def _panorama() -> dict:
    return {
        "papel": "mencao",
        "tipo_conteudo": "resumo_mercado",
        "evento": "macro",
        "qualidade": 0.1,
        "confianca": 0.9,
        "tickers_adicionais": [],
    }


def regra_barata(
    artigo: dict,
    ticker: str,
    nomes_por_ticker: dict[str, list[str]],
    *,
    dominios_bloqueados: frozenset[str] = DOMINIOS_BLOQUEADOS,
    padroes: tuple[str, ...] = PADROES_PANORAMA,
) -> dict | None:
    """Classificacao sem LLM para o caso obvio, ou None quando a LLM precisa decidir."""
    if artigo.get("origem") == "cvm_ipe":
        return {
            "papel": "protagonista",
            "tipo_conteudo": "reportagem",
            "evento": "fato_relevante",
            "qualidade": 1.0,
            "confianca": 1.0,
            "tickers_adicionais": [],
        }

    dominio = str(artigo.get("source_domain") or "").lower().removeprefix("www.")
    if any(dominio == d or dominio.endswith("." + d) for d in dominios_bloqueados):
        return {
            "papel": "mencao",
            "tipo_conteudo": "podcast_video",
            "evento": "outro",
            "qualidade": 0.0,
            "confianca": 1.0,
            "tickers_adicionais": [],
        }

    titulo = str(artigo.get("title") or "")
    titulo_norm = normalizar(titulo)
    if any(re.search(padrao, titulo_norm, re.IGNORECASE) for padrao in padroes):
        return _panorama()

    empresas = contar_empresas_no_titulo(titulo, nomes_por_ticker, ignorar=ticker)
    if empresas >= _MIN_EMPRESAS_PANORAMA:
        return _panorama()

    return None
