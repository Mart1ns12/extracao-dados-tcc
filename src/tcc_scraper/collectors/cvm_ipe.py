"""Fatos relevantes e comunicados do IPE (CVM) como noticias de origem 'cvm_ipe'.

O IPE e o cadastro de documentos entregues pelas companhias abertas (fato relevante,
comunicado ao mercado, aviso aos acionistas). Cada protocolo vira uma linha em
v1-raw_ipe_docs e uma materia em raw_articles, vinculada a todos os tickers da companhia.

O download do zip anual passa por CvmCollector.baixar_zip (throttle e retry da base).
O Codigo_CVM do CSV pode vir com zeros a esquerda: a comparacao e sempre feita como int.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select

from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.collectors.cvm_base import (
    CVM_BASE,
    CvmCollector,
    cnpj_digitos,
    data_cvm,
    last_modified,
    ler_csvs_do_zip,
)
from tcc_scraper.config import IPE_CATEGORIAS, TICKER_CODE_CVM
from tcc_scraper.db.models import Article, ArticleTicker, url_hash
from tcc_scraper.db.models_fontes import RawIpeDoc
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")
DOMINIO_RAD = "rad.cvm.gov.br"


@dataclass(frozen=True)
class IpeDocumento:
    protocolo: str
    cnpj: str
    cd_cvm: int
    nome: str
    categoria: str
    tipo: str
    especie: str
    assunto: str
    data_referencia: dt.date | None
    data_entrega: dt.date
    tipo_apresentacao: str
    versao: int | None
    link: str


class CvmIpeCollector(CvmCollector):
    """Coleta os IPEs anuais da CVM e grava os documentos das companhias do universo."""

    name = "cvm_ipe"

    def __init__(self, anos: list[int] | None = None, **kw) -> None:
        super().__init__(**kw)
        if anos is None:
            inicio = self.settings.inicio_noticias.year
            anos = list(range(inicio, self.settings.fim_coleta.year + 1))
        self.anos = anos

    def work_units(self) -> Iterable[WorkUnit]:
        hoje = dt.date.today().isoformat()
        for ano in self.anos:
            url = f"{CVM_BASE}/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip"
            lm = last_modified(self, url)
            yield WorkUnit(key=f"ipe|{ano}|{lm or hoje}", payload={"ano": ano, "url": url})

    def process(self, unit: WorkUnit) -> UnitResult:
        ano = unit.payload["ano"]
        conteudo = self.baixar_zip(unit.payload["url"])
        nome_csv = f"ipe_cia_aberta_{ano}.csv"
        linhas = ler_csvs_do_zip(conteudo, (nome_csv,))[nome_csv]
        docs = parse_ipe(
            linhas,
            set(TICKER_CODE_CVM.values()),
            IPE_CATEGORIAS,
            desde=self.settings.inicio_noticias,
        )
        n = _persist(docs)
        return UnitResult(items_found=n)


def parse_ipe(
    linhas: list[dict],
    cd_cvm_universo: set[int],
    categorias: frozenset[str],
    desde: dt.date,
) -> list[IpeDocumento]:
    """Filtra as linhas do IPE: categoria na lista, companhia no universo, entrega >= desde."""
    docs: list[IpeDocumento] = []
    for linha in linhas:
        if _texto(linha, "Categoria") not in categorias:
            continue
        cd_cvm = _inteiro(linha.get("Codigo_CVM"))
        if cd_cvm is None or cd_cvm not in cd_cvm_universo:
            continue
        data_entrega = data_cvm(linha.get("Data_Entrega"))
        if data_entrega is None or data_entrega < desde:
            continue
        protocolo = _texto(linha, "Protocolo_Entrega")
        link = _texto(linha, "Link_Download")
        if not protocolo or not link:
            continue
        docs.append(
            IpeDocumento(
                protocolo=protocolo,
                cnpj=cnpj_digitos(_texto(linha, "CNPJ_Companhia")),
                cd_cvm=cd_cvm,
                nome=_texto(linha, "Nome_Companhia"),
                categoria=_texto(linha, "Categoria"),
                tipo=_texto(linha, "Tipo"),
                especie=_texto(linha, "Especie"),
                assunto=_texto(linha, "Assunto"),
                data_referencia=data_cvm(linha.get("Data_Referencia")),
                data_entrega=data_entrega,
                tipo_apresentacao=_texto(linha, "Tipo_Apresentacao"),
                versao=_inteiro(linha.get("Versao")),
                link=link,
            )
        )
    return docs


def artigo_de_ipe(doc: IpeDocumento, agora: dt.datetime) -> dict:
    """Linha de raw_articles para um documento do IPE.

    A data de entrega vira o inicio do dia no fuso de Brasilia (published_precision='date').
    """
    inicio_do_dia = dt.datetime.combine(doc.data_entrega, dt.time.min, tzinfo=FUSO_BRASIL)
    return {
        "url": doc.link,
        "url_hash": url_hash(doc.link),
        "google_id": None,
        "resolved_url": doc.link,
        "title": doc.assunto or f"{doc.categoria}: {doc.nome}",
        "source_name": "CVM",
        "source_domain": DOMINIO_RAD,
        "published_at": inicio_do_dia.astimezone(dt.UTC),
        "lang": "pt",
        "published_precision": "date",
        "origem": "cvm_ipe",
        "collected_at": agora,
    }


def tickers_do_cd_cvm(cd_cvm: int) -> list[str]:
    """Tickers da companhia (inverso de TICKER_CODE_CVM), ordenados."""
    return sorted(ticker for ticker, codigo in TICKER_CODE_CVM.items() if codigo == cd_cvm)


def _persist(docs: list[IpeDocumento]) -> int:
    """Grava documentos, artigos e vinculos em lote, sem RETURNING (ver google_news._persist)."""
    if not docs:
        return 0
    agora = dt.datetime.now(dt.UTC)

    # O mesmo protocolo ou link repetido no CSV e resolvido aqui: o lote nao pode trazer
    # a mesma chave duas vezes (ver db/upsert.deduplicate).
    por_protocolo = {doc.protocolo: doc for doc in docs}
    por_hash = {url_hash(doc.link): doc for doc in por_protocolo.values()}

    linhas_raw = [
        {
            "protocolo_entrega": doc.protocolo,
            "cnpj": doc.cnpj,
            "nome_companhia": doc.nome,
            "cd_cvm": doc.cd_cvm,
            "data_referencia": doc.data_referencia,
            "categoria": doc.categoria,
            "tipo": doc.tipo,
            "especie": doc.especie,
            "assunto": doc.assunto,
            "data_entrega": doc.data_entrega,
            "tipo_apresentacao": doc.tipo_apresentacao,
            "versao": doc.versao,
            "link_download": doc.link,
            "fetched_at": agora,
        }
        for doc in por_protocolo.values()
    ]
    artigos = [artigo_de_ipe(doc, agora) for doc in por_hash.values()]

    with session_scope() as session:
        bulk_upsert(
            session,
            RawIpeDoc.__table__,
            linhas_raw,
            index_elements=["protocolo_entrega"],
            update_columns=None,
        )
        bulk_upsert(
            session,
            Article.__table__,
            artigos,
            index_elements=["url_hash"],
            update_columns=None,
        )
        session.flush()

        ids_por_hash = {
            h: aid
            for aid, h in session.execute(
                select(Article.id, Article.url_hash).where(Article.url_hash.in_(list(por_hash)))
            ).all()
        }

        vinculos = [
            {"article_id": ids_por_hash[h], "ticker": ticker}
            for h, doc in por_hash.items()
            if h in ids_por_hash
            for ticker in tickers_do_cd_cvm(doc.cd_cvm)
        ]
        bulk_upsert(
            session,
            ArticleTicker.__table__,
            vinculos,
            index_elements=["article_id", "ticker"],
            update_columns=None,
        )
    return len(docs)


def _texto(linha: dict, campo: str) -> str:
    return (linha.get(campo) or "").strip()


def _inteiro(valor: str | None) -> int | None:
    """Inteiro da CVM (aceita zeros a esquerda). Vazio ou invalido vira None."""
    try:
        return int((valor or "").strip())
    except ValueError:
        return None
