"""Cliente HTTP do executor para a API do painel: fila de comandos, ack e heartbeat.

Nao e coleta, entao nao passa pelo throttle do `Collector`. Nunca levanta para quem chama:
falha de rede ou de HTTP vira aviso no log e um retorno neutro (None ou False).

Contrato (interface-acompanhamento/docs/contrato-api.md): as tres rotas sao POST.
  POST /api/executor/comandos       {}                              -> {"comandos": [...]}
  POST /api/executor/comandos/ack   {"id", "ok", "erro"}            -> 204 / 404 / 409
  POST /api/executor/heartbeat      {"host", "versao", "ocupado_com"} -> 204
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Comando:
    id: str
    tipo: str
    parametros: dict
    criado_em: str


class PainelClient:
    def __init__(
        self,
        url: str,
        token: str,
        timeout_s: float = 6.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout_s
        self._http = client or httpx.Client(timeout=timeout_s)

    def comandos(self) -> list[Comando] | None:
        """Comandos ainda nao confirmados, em ordem. None em falha (de rede ou HTTP)."""
        try:
            resp = self._http.post(
                f"{self._url}/api/executor/comandos",
                json={},
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            itens = resp.json()["comandos"]
            return [
                Comando(
                    id=str(c["id"]),
                    tipo=str(c["tipo"]),
                    parametros=dict(c.get("parametros") or {}),
                    criado_em=str(c.get("criado_em", "")),
                )
                for c in itens
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("painel: falha ao buscar comandos: %s", exc)
            return None

    def ack(self, comando_id: str, ok: bool, erro: str | None) -> bool:
        """Confirma o comando. True em sucesso, se ja estava confirmado (409) ou se o painel
        nao o conhece mais (404): nos dois ultimos casos nao ha o que reenviar."""
        try:
            resp = self._http.post(
                f"{self._url}/api/executor/comandos/ack",
                json={"id": comando_id, "ok": ok, "erro": erro},
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("painel: falha ao confirmar comando %s: %s", comando_id, exc)
            return False
        if resp.is_success:
            return True
        if resp.status_code in (404, 409):
            logger.info(
                "painel: ack do comando %s ignorado (HTTP %s)", comando_id, resp.status_code
            )
            return True
        logger.warning(
            "painel: ack do comando %s recusado (HTTP %s)", comando_id, resp.status_code
        )
        return False

    def heartbeat(self, host: str, versao: str, ocupado_com: str | None) -> bool:
        try:
            resp = self._http.post(
                f"{self._url}/api/executor/heartbeat",
                json={"host": host, "versao": versao, "ocupado_com": ocupado_com},
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("painel: falha ao enviar heartbeat: %s", exc)
            return False
        if not resp.is_success:
            logger.warning("painel: heartbeat recusado (HTTP %s)", resp.status_code)
            return False
        return True
