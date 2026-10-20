"""Orquestrador do daily: catalogo das 9 etapas, progresso e persistencia no painel.

Importar o pacote nao carrega as fases pesadas (torch, Gemini): elas entram dentro das funcoes.
"""

from __future__ import annotations

from tcc_scraper.orquestrador.daily import Daily, ExecucaoEmAndamento
from tcc_scraper.orquestrador.etapas import ETAPAS, POR_ID, EtapaDef, ResultadoEtapa
from tcc_scraper.orquestrador.repositorio import Repositorio, RepositorioBanco, RepositorioMemoria

__all__ = [
    "ETAPAS",
    "POR_ID",
    "Daily",
    "EtapaDef",
    "ExecucaoEmAndamento",
    "Repositorio",
    "RepositorioBanco",
    "RepositorioMemoria",
    "ResultadoEtapa",
]
