"""Coletor da carteira atual do IBOV (GetPortfolioDay, indexProxy) e das mudancas na composicao.

Compara a carteira que a B3 publica agora com as vigencias abertas (vigencia_fim NULL) em
v1-composicao_ibov e grava so a diferenca, com a data do header da resposta (dd/mm/aa):
- papel que entrou: vigencia nova a partir dessa data, fonte b3_api, com o peso do dia;
- papel que saiu: a vigencia aberta recebe vigencia_fim = dia anterior ao header.

Sem vigencia aberta (o build-entrega ainda nao rodou), nao grava nada: a referencia estaria
vazia e todos os papeis pareceriam novos. Unidade unica, sem checkpoint (resume=False): o
daily precisa olhar a carteira de novo a cada rodada.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from tcc_scraper.collectors.b3_base import B3Collector, decimal_b3
from tcc_scraper.collectors.base import UnitResult, WorkUnit
from tcc_scraper.db.models_entrega import ComposicaoIbov
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.entrega.composicao_ibov import FONTE_API

logger = logging.getLogger(__name__)

INDEX_BASE = "https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall"
METODO_CARTEIRA = "GetPortfolioDay"
PARAMS_CARTEIRA = {"language": "pt-br", "pageNumber": 1, "pageSize": 120, "index": "IBOV",
                   "segment": "1"}


class B3CarteiraIbovCollector(B3Collector):
    """Carteira atual do IBOV: grava as entradas e saidas em relacao as vigencias abertas."""

    name = "b3_carteira_ibov"

    # -- contrato do Collector ----------------------------------------------------
    def work_units(self) -> Iterable[WorkUnit]:
        yield WorkUnit(key="ibov|carteira_atual", payload={})

    def process(self, unit: WorkUnit) -> UnitResult:
        corpo = self.get_json(METODO_CARTEIRA, PARAMS_CARTEIRA, base=INDEX_BASE)
        if not isinstance(corpo, dict):
            # Sem JSON nao ha o que comparar: levantar marca a unidade como erro no checkpoint.
            raise RuntimeError("B3 nao devolveu a carteira do IBOV")

        data, carteira = parse_carteira(corpo)
        abertas = _vigencias_abertas()
        if not abertas:
            logger.warning(
                "b3_carteira_ibov: nenhuma vigencia aberta em v1-composicao_ibov; "
                "rode build-entrega antes. Nada gravado."
            )
            return UnitResult(items_found=0)

        linhas = mudancas_da_carteira(abertas, carteira, data)
        self._persist(linhas)
        logger.info(
            "b3_carteira_ibov: carteira de %s com %d papeis; %d mudancas gravadas",
            data.isoformat(), len(carteira), len(linhas),
        )
        return UnitResult(items_found=len(linhas))

    # -- escrita ------------------------------------------------------------------
    def _persist(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        with session_scope() as session:
            bulk_upsert(
                session,
                ComposicaoIbov.__table__,
                linhas,
                index_elements=["ticker", "vigencia_inicio"],
                update_columns=["vigencia_fim", "participacao", "fonte"],
            )


def parse_carteira(corpo: dict[str, Any]) -> tuple[dt.date, dict[str, float | None]]:
    """Data do header e carteira (codigo -> participacao em %) de uma resposta GetPortfolioDay.

    Levanta se a resposta nao traz todos os papeis que ela mesma anuncia (totalRecords): uma
    pagina faltando seria lida como saida em massa. Peso vazio vira None.
    """
    data = _data_header(corpo["header"]["date"])
    resultados = corpo.get("results") or []
    total = (corpo.get("page") or {}).get("totalRecords")
    if total is not None and int(total) != len(resultados):
        raise ValueError(f"carteira incompleta: {len(resultados)} de {total} papeis")

    carteira: dict[str, float | None] = {}
    for registro in resultados:
        codigo = (registro.get("cod") or "").strip()
        if not codigo:
            raise ValueError("papel sem codigo na carteira do IBOV")
        if codigo in carteira:
            raise ValueError(f"codigo {codigo} repetido na carteira do IBOV")
        peso = decimal_b3(registro.get("part"))
        carteira[codigo] = float(peso) if peso is not None else None
    return data, carteira


def _data_header(texto: str) -> dt.date:
    """Data do header da B3, que vem com ano de dois digitos ("23/10/26") ou quatro."""
    for formato in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(texto.strip(), formato).date()
        except ValueError:
            continue
    raise ValueError(f"data do header da carteira do IBOV invalida: {texto!r}")


def mudancas_da_carteira(
    abertas: list[dict], carteira: dict[str, float | None], data: dt.date
) -> list[dict]:
    """Linhas a gravar para levar as vigencias abertas a carteira de `data`.

    `abertas`: linhas com vigencia_fim NULL (ticker, vigencia_inicio, participacao, fonte).
    Entrada vira linha nova; saida devolve a propria linha aberta, com vigencia_fim = data - 1.
    Papel que saiu no mesmo dia em que entrou nao tem como fechar (fim antes do inicio): fica de
    fora, com aviso.
    """
    por_papel = {linha["ticker"]: linha for linha in abertas}
    linhas: list[dict] = []
    for papel in sorted(carteira.keys() - por_papel.keys()):
        linhas.append({
            "ticker": papel,
            "vigencia_inicio": data,
            "vigencia_fim": None,
            "participacao": carteira[papel],
            "fonte": FONTE_API,
        })
    for papel in sorted(por_papel.keys() - carteira.keys()):
        linha = dict(por_papel[papel])
        if linha["vigencia_inicio"] >= data:
            logger.warning("b3_carteira_ibov: %s sai em %s, mesmo dia do inicio; ignorado",
                           papel, data)
            continue
        linha["vigencia_fim"] = data - dt.timedelta(days=1)
        linhas.append(linha)
    return linhas


def _vigencias_abertas() -> list[dict]:
    with session_scope() as session:
        consulta = select(
            ComposicaoIbov.ticker, ComposicaoIbov.vigencia_inicio,
            ComposicaoIbov.vigencia_fim, ComposicaoIbov.participacao, ComposicaoIbov.fonte,
        ).where(ComposicaoIbov.vigencia_fim.is_(None))
        return [dict(linha) for linha in session.execute(consulta).mappings()]
