"""Infraestrutura comum de coleta: throttle, retry e checkpoint.

Todo coletor herda daqui. O motivo e concreto: a coleta de noticias sao ~17 mil
requisicoes ao longo de ~10 h. Sem throttle, o Google bloqueia; sem checkpoint,
qualquer queda obriga a recomecar do zero.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from tcc_scraper.config import get_settings
from tcc_scraper.db.models import CollectionRun
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

logger = logging.getLogger(__name__)


class DomainThrottle:
    """Garante um intervalo minimo entre requisicoes ao MESMO dominio.

    O limite dos servidores e por host, nao global: serializar tudo desperdicaria
    tempo quando alternamos entre Yahoo, BCB e Google.
    """

    def __init__(self, default_delay: float = 1.0) -> None:
        self._default = default_delay
        self._delays: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def configure(self, domain: str, delay: float) -> None:
        self._delays[domain] = delay

    def set_default(self, delay: float) -> None:
        """Ajusta o intervalo dos dominios sem configuracao propria.

        Existe porque subclasses so conhecem o valor certo depois do __init__ da
        base, que ja criou o throttle -- atribuir `self.default_delay` depois disso
        nao surtia efeito nenhum.
        """
        self._default = delay

    def wait(self, url: str) -> None:
        domain = urlsplit(url).netloc.lower()
        delay = self._delays.get(domain, self._default)
        with self._lock:
            elapsed = time.monotonic() - self._last.get(domain, 0.0)
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last[domain] = time.monotonic()


@dataclass
class WorkUnit:
    """Menor pedaco de trabalho retomavel de um coletor.

    key identifica a unidade no checkpoint (ex.: "PETR4|2024-06-03|pt") e precisa ser
    estavel entre execucoes -- se mudar, o trabalho ja feito seria refeito.
    """

    key: str
    payload: dict = field(default_factory=dict)


@dataclass
class UnitResult:
    items_found: int = 0
    # True quando a resposta bateu o teto da fonte: sinaliza perda silenciosa se a
    # janela nao for subdividida.
    hit_cap: bool = False


class Collector(ABC):
    """Base de todos os coletores.

    Subclasses implementam apenas `work_units()` e `process(unit)`. Throttle, retry,
    checkpoint e contabilidade ficam aqui.
    """

    name: str = "base"
    default_delay: float = 1.0
    max_attempts: int = 4

    def __init__(self, throttle: DomainThrottle | None = None) -> None:
        self.settings = get_settings()
        self.throttle = throttle or DomainThrottle(self.default_delay)
        self._client = httpx.Client(
            headers={"User-Agent": self.settings.user_agent},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )

    # -- rede ---------------------------------------------------------------------
    def fetch(self, url: str, **kwargs) -> httpx.Response:
        """GET com throttle por dominio e retry exponencial.

        Repete apenas em falha transitoria (rede, 429, 5xx). Um 404 e resposta
        definitiva -- insistir so gastaria a cota.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.throttle.wait(url)
            try:
                response = self._client.get(url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt == self.max_attempts:
                    break
                backoff = 2.0**attempt
                logger.warning(
                    "%s: tentativa %d/%d falhou (%s); aguardando %.0fs",
                    self.name, attempt, self.max_attempts, exc, backoff,
                )
                time.sleep(backoff)
        raise RuntimeError(f"{self.name}: falha ao buscar {url}") from last_exc

    # -- checkpoint ---------------------------------------------------------------
    def completed_keys(self) -> set[str]:
        with session_scope() as session:
            rows = session.execute(
                select(CollectionRun.unit_key).where(
                    CollectionRun.collector == self.name,
                    CollectionRun.status == "ok",
                )
            ).scalars()
            return set(rows)

    def _record(self, unit: WorkUnit, result: UnitResult | None, error: str | None) -> None:
        """Grava o checkpoint com upsert: reexecutar uma unidade nao duplica linhas."""
        with session_scope() as session:
            bulk_upsert(
                session,
                CollectionRun.__table__,
                [
                    {
                        "collector": self.name,
                        "unit_key": unit.key,
                        "status": "ok" if error is None else "error",
                        "items_found": result.items_found if result else 0,
                        "hit_cap": result.hit_cap if result else False,
                        "error": error,
                        "finished_at": datetime.now(UTC),
                    }
                ],
                index_elements=["collector", "unit_key"],
                update_columns=["status", "items_found", "hit_cap", "error", "finished_at"],
            )

    # -- contrato das subclasses --------------------------------------------------
    @abstractmethod
    def work_units(self) -> Iterable[WorkUnit]:
        """Enumera todo o trabalho, inclusive o ja concluido. `run` filtra o resto."""

    @abstractmethod
    def process(self, unit: WorkUnit) -> UnitResult:
        """Executa uma unidade e persiste o que coletou."""

    # -- orquestracao -------------------------------------------------------------
    def run(self, *, resume: bool = True, limit: int | None = None) -> dict[str, int]:
        """Executa as unidades pendentes.

        Com resume=True (padrao), pula o que ja foi concluido -- e o que torna a
        coleta de 10 h interrompivel a qualquer momento.
        """
        done = self.completed_keys() if resume else set()
        units = [u for u in self.work_units() if u.key not in done]
        if limit is not None:
            units = units[:limit]

        stats = {"total": len(units), "ok": 0, "erro": 0, "itens": 0, "saturadas": 0}
        logger.info(
            "%s: %d unidades pendentes (%d ja concluidas)",
            self.name, len(units), len(done),
        )

        for unit in units:
            try:
                result = self.process(unit)
            except Exception as exc:
                logger.error("%s: unidade %s falhou: %s", self.name, unit.key, exc)
                self._record(unit, None, str(exc)[:500])
                stats["erro"] += 1
                continue
            self._record(unit, result, None)
            stats["ok"] += 1
            stats["itens"] += result.items_found
            if result.hit_cap:
                stats["saturadas"] += 1
        return stats

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
