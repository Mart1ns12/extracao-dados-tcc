"""Coletor de proventos e desdobramentos da B3 (listedCompaniesProxy).

Duas chamadas por empresa, ambas do mesmo endpoint da B3:
- GetListedCashDividends: historico paginado de proventos (dividendo, JCP, rendimento...).
- GetListedSupplementCompany: suplemento com eventos em dinheiro (cashDividends) e em
  acoes (stockDividends, ex.: desdobramento). Subscricoes sao ignoradas.

Gravacao em raw_b3_proventos e raw_b3_suplemento com chave = hash do registro original.
A camada raw e append-only: update_columns=None preserva a linha ja existente.

Nome de pregao: o parametro tradingName do endpoint espera o nome exato da B3 (ex.:
"KLABIN S.A."), que pode diferir do tradingName do cadastro ("KLABIN S/A"). Por isso
tentamos o trading_name do cadastro e, se nao vier resultado, o companyName do payload.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from tcc_scraper.collectors.b3_base import B3Collector, data_b3, decimal_b3, registro_hash
from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.db.models_fontes import RawB3Cadastro, RawB3Proventos, RawB3Suplemento
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)

PAGE_SIZE = 120
MAX_PAGINAS = 50


class B3ProventosCollector(B3Collector):
    """Proventos e suplemento por empresa (issuing_company), a partir do cadastro B3."""

    name = "b3_proventos"

    def __init__(self, empresas: list[tuple[str, str]] | None = None, **kw) -> None:
        """empresas: [(issuing_company, trading_name)]. Sem valor, le raw_b3_cadastro."""
        super().__init__(**kw)
        self._empresas = empresas

    # -- contrato do Collector ----------------------------------------------------
    def work_units(self) -> Iterable[WorkUnit]:
        if self._empresas is not None:
            alvo = [
                {"issuing_company": ic, "trading_name": tn, "company_name": None}
                for ic, tn in self._empresas
            ]
        else:
            alvo = self._ler_universo()
        for empresa in alvo:
            yield WorkUnit(key=f"prov|{empresa['issuing_company']}", payload=empresa)

    def process(self, unit: WorkUnit) -> UnitResult:
        ic = unit.payload["issuing_company"]
        nome, paginas = self._buscar_proventos(_nomes_candidatos(unit.payload))
        if nome is None:
            logger.warning("b3_proventos: %s sem trading_name nem companyName", ic)
        supl = self.get_json(
            "GetListedSupplementCompany", {"issuingCompany": ic, "language": "pt-br"}
        )
        if supl is None:
            logger.warning("b3_proventos: %s sem suplemento (GetListedSupplementCompany)", ic)

        regs = parse_cash_dividends(paginas, ic, nome)
        sup = parse_suplemento(supl, ic)
        self._persist(regs, sup)

        acoes = sorted({r["corporate_action"] for r in regs if r["corporate_action"]})
        logger.info(
            "b3_proventos: %s: %d proventos, %d suplementos; corporate_action=%s",
            ic, len(regs), len(sup), acoes,
        )
        return UnitResult(items_found=len(regs) + len(sup))

    # -- leitura ------------------------------------------------------------------
    def _ler_universo(self) -> list[dict]:
        """Empresas de raw_b3_cadastro (ja restritas ao universo pelo B3CadastroCollector)."""
        with session_scope() as session:
            rows = session.execute(
                select(
                    RawB3Cadastro.issuing_company,
                    RawB3Cadastro.trading_name,
                    RawB3Cadastro.payload,
                ).where(RawB3Cadastro.issuing_company.is_not(None))
            ).all()
        empresas = []
        for ic, tn, payload in rows:
            company_name = payload.get("companyName") if isinstance(payload, dict) else None
            empresas.append(
                {"issuing_company": ic, "trading_name": tn, "company_name": company_name}
            )
        return empresas

    def _buscar_proventos(self, candidatos: list[str]) -> tuple[str | None, list[dict]]:
        """Primeiro nome candidato que devolver resultado; senao, o primeiro e a busca vazia."""
        paginas: list[dict] = []
        for nome in candidatos:
            paginas = self._paginar(nome)
            if any(pagina.get("results") for pagina in paginas):
                return nome, paginas
        return (candidatos[0] if candidatos else None), paginas

    def _paginar(self, trading_name: str) -> list[dict]:
        """Paginas de GetListedCashDividends. Para na pagina curta, vazia, None ou no teto."""
        paginas: list[dict] = []
        for numero in range(1, MAX_PAGINAS + 1):
            pagina = self.get_json(
                "GetListedCashDividends",
                {
                    "language": "pt-br",
                    "pageNumber": numero,
                    "pageSize": PAGE_SIZE,
                    "tradingName": trading_name,
                },
            )
            if not isinstance(pagina, dict):
                logger.warning(
                    "b3_proventos: sem dados para %r na pagina %d", trading_name, numero
                )
                break
            paginas.append(pagina)
            if len(pagina.get("results") or []) < PAGE_SIZE:
                break
        else:
            logger.warning(
                "b3_proventos: teto de %d paginas atingido para %r", MAX_PAGINAS, trading_name
            )
        return paginas

    # -- escrita ------------------------------------------------------------------
    def _persist(self, regs: list[dict], sup: list[dict]) -> None:
        with session_scope() as session:
            bulk_upsert(
                session,
                RawB3Proventos.__table__,
                regs,
                index_elements=["registro_hash"],
                update_columns=None,
            )
            bulk_upsert(
                session,
                RawB3Suplemento.__table__,
                sup,
                index_elements=["registro_hash"],
                update_columns=None,
            )


def _nomes_candidatos(payload: dict) -> list[str]:
    """trading_name e companyName do cadastro, sem vazios nem repetidos, nessa ordem."""
    nomes: list[str] = []
    for chave in ("trading_name", "company_name"):
        nome = (payload.get(chave) or "").strip()
        if nome and nome not in nomes:
            nomes.append(nome)
    return nomes


def parse_cash_dividends(
    paginas: list[dict], issuing_company: str, trading_name: str | None
) -> list[dict]:
    """Uma linha por registro de GetListedCashDividends, em todas as paginas.

    O registro_hash inclui a empresa. Mudar o conteudo hasheado exige recoleta: as linhas
    gravadas com o hash antigo nao sao reescritas pelo upsert (raw e append-only).
    """
    agora = datetime.now(UTC)
    linhas: list[dict] = []
    for pagina in paginas:
        for registro in pagina.get("results") or []:
            valor = decimal_b3(registro.get("valueCash"))
            linhas.append(
                {
                    "registro_hash": registro_hash(
                        {**registro, "issuing_company": issuing_company}
                    ),
                    "issuing_company": issuing_company,
                    "trading_name": trading_name,
                    "type_stock": registro.get("typeStock"),
                    "corporate_action": registro.get("corporateAction"),
                    "value_cash": float(valor) if valor is not None else None,
                    "last_date_prior_ex": data_b3(registro.get("lastDatePriorEx")),
                    "date_approval": data_b3(registro.get("dateApproval")),
                    "ratio": registro.get("ratio"),
                    "quoted_per_shares": registro.get("quotedPerShares"),
                    "payload": registro,
                    "fetched_at": agora,
                }
            )
    return linhas


def parse_suplemento(payload: Any, issuing_company: str) -> list[dict]:
    """cashDividends viram tipo 'cash'; stockDividends viram 'stock'. Subscricoes ficam de fora.

    O endpoint devolve um objeto, mas o JSON capturado tambem vem como lista de 1 objeto.
    """
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        return []
    agora = datetime.now(UTC)
    linhas: list[dict] = []
    for chave, tipo in (("cashDividends", "cash"), ("stockDividends", "stock")):
        for item in payload.get(chave) or []:
            linhas.append(
                {
                    "registro_hash": registro_hash({**item, "issuing_company": issuing_company}),
                    "issuing_company": issuing_company,
                    "tipo": tipo,
                    "payload": item,
                    "fetched_at": agora,
                }
            )
    return linhas
