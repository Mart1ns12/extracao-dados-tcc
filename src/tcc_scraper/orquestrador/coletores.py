"""Catalogo unico dos coletores do executor: id do painel -> alvo, checkpoint e parametros.

Usado pela carga historica (`historico._passos`) e pelo comando `coletar` (`coletar.py`).
Os ids sao contrato com o painel: nao renomear sem combinar. Os alvos ("modulo:nome") sao
resolvidos so na hora de executar: importar este modulo nao carrega torch nem a LLM, e os
testes trocam `resolver` por um duble.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tcc_scraper.config import Settings


def resolver(alvo: str) -> Any:
    modulo, nome = alvo.split(":")
    return getattr(importlib.import_module(modulo), nome)


# Faixas de `anos` aceitas: espelham o padrao do construtor de cada coletor.
def _anos_precos(s: Settings) -> tuple[int, int]:  # IndicesBcbCollector, B3IndicesCollector
    return s.price_start.year, s.fim_coleta.year


def _anos_balancos(s: Settings) -> tuple[int, int]:  # CvmDemonstracoesCollector
    return s.data_start.year - 1, s.fim_coleta.year


def _anos_noticias(s: Settings) -> tuple[int, int]:  # CvmIpeCollector
    return s.inicio_noticias.year, s.fim_coleta.year


@dataclass(frozen=True)
class ColetorDef:
    id: str
    nome: str
    alvo: str
    # True: classe de coletor (ganha `run`). False: funcao (recebe `observador=`, salvo abaixo).
    classe: bool = True
    # Checkpoint ligado por padrao (carga historica e `coletar` sem `refazer`).
    resume: bool = True
    # Parametros aceitos pelo comando `coletar`: subconjunto de anos, tickers, desde, ate,
    # e os proprios das acoes avulsas (aplicar, limite, n, semente).
    aceita: frozenset[str] = frozenset()
    janela_anos: Callable[[Settings], tuple[int, int]] | None = None
    # False: a funcao nao aceita `observador=` (corrigir_horas, juiza_concordancia).
    recebe_observador: bool = True

    def executar(
        self,
        observador: Any,
        *,
        args: dict | None = None,
        resume: bool | None = None,
        apenas_erros: bool = False,
        resolver_alvo: Callable[[str], Any] | None = None,
    ) -> Any:
        """Roda o coletor (ou a funcao). `resume=None` usa o padrao do catalogo."""
        alvo = (resolver_alvo or resolver)(self.alvo)
        kwargs = dict(args or {})
        if not self.classe:
            if not self.recebe_observador:
                return alvo(**kwargs)
            return alvo(observador=observador, **kwargs)
        if apenas_erros:
            execucao: dict = {"apenas_erros": True}
        else:
            execucao = {"resume": self.resume if resume is None else resume}
        with alvo(**kwargs) as coletor:
            return coletor.run(observador=observador, **execucao)


_P = "tcc_scraper."
_DEFS = (
    ColetorDef("yahoo", "Preços (Yahoo)", _P + "collectors.prices_yahoo:YahooPriceCollector",
               resume=False, aceita=frozenset({"tickers"})),
    ColetorDef("b3_indices", "Índices B3 (IDIV, SMLL, IFIX)",
               _P + "collectors.b3_indices:B3IndicesCollector",
               aceita=frozenset({"anos"}), janela_anos=_anos_precos),
    ColetorDef("b3_carteira_ibov", "Carteira do IBOV (B3)",
               _P + "collectors.b3_carteira_ibov:B3CarteiraIbovCollector", resume=False),
    ColetorDef("bcb", "Selic e PTAX (BCB)", _P + "collectors.macro_bcb:BcbCollector"),
    ColetorDef("indices_bcb", "CDI e IPCA (BCB)", _P + "collectors.indices:IndicesBcbCollector",
               aceita=frozenset({"anos"}), janela_anos=_anos_precos),
    ColetorDef("cvm_cadastro", "Cadastro CVM",
               _P + "collectors.cvm_cadastro:CvmCadastroCollector"),
    ColetorDef("b3_cadastro", "Cadastro B3", _P + "collectors.b3_cadastro:B3CadastroCollector"),
    ColetorDef("b3_proventos", "Proventos B3",
               _P + "collectors.b3_proventos:B3ProventosCollector", resume=False),
    ColetorDef("cvm_itr_dfp", "ITR e DFP (CVM)",
               _P + "collectors.cvm_demonstracoes:CvmDemonstracoesCollector",
               aceita=frozenset({"anos"}), janela_anos=_anos_balancos),
    ColetorDef("google_news", "Notícias (Google News)",
               _P + "collectors.google_news:GoogleNewsCollector",
               aceita=frozenset({"tickers", "desde", "ate"})),
    ColetorDef("wordpress", "Notícias WordPress (InfoMoney, Money Times, Exame)",
               _P + "collectors.wordpress:WordPressCollector",
               aceita=frozenset({"desde", "ate"})),
    ColetorDef("processar_wp", "Processamento WordPress (casamento e histórias)",
               _P + "enrich.processar_wp:processar_wp", classe=False,
               aceita=frozenset({"desde", "ate"})),
    ColetorDef("cvm_ipe", "Fatos relevantes (CVM IPE)", _P + "collectors.cvm_ipe:CvmIpeCollector",
               aceita=frozenset({"anos"}), janela_anos=_anos_noticias),
    ColetorDef("enriquecimento", "Corpo dos artigos",
               _P + "enrich.article_fetcher:ArticleBodyCollector", resume=False),
    ColetorDef("finbert", "Sentimento (FinBERT)", _P + "sentiment.scoring:score_pending",
               classe=False),
    ColetorDef("juiza", "Juíza de notícias", _P + "llm.juiza:julgar_pendentes", classe=False,
               aceita=frozenset({"anos"}), janela_anos=_anos_noticias),
    ColetorDef("build_entrega", "Montagem da entrega", _P + "entrega.builder:build_entrega",
               classe=False),
    # Mesma funcao da CLI build-dataset, com os padroes dela (persist, export, sem keep_unlabeled).
    ColetorDef("build_dataset", "Dataset 1.0 (features_dataset_v1)",
               _P + "features.build:build_dataset_v1", classe=False, recebe_observador=False),
    # Acoes avulsas do painel: so no `coletar`, fora do daily e da carga historica (_passos).
    ColetorDef("corrigir_horas", "Corrigir horas (Google News)",
               _P + "enrich.article_fetcher:corrigir_horas_publicacao",
               classe=False, resume=False, recebe_observador=False,
               aceita=frozenset({"aplicar", "limite"})),
    ColetorDef("juiza_concordancia", "Concordância Jev × referência",
               _P + "llm.juiza_concordancia:medir_concordancia",
               classe=False, resume=False, recebe_observador=False,
               aceita=frozenset({"n", "semente"})),
)
CATALOGO: dict[str, ColetorDef] = {d.id: d for d in _DEFS}


def args_fixos(coletor: str, settings: Settings) -> dict:
    """Argumentos que nao vem do painel: a juiza na carga roda em modo backlog (verba em US$)."""
    if coletor == "juiza":
        return {"modo": "backlog"}
    return {}
