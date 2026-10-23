"""Base dos coletores da B3 (listedCompaniesProxy): URL do endpoint, parsing e hash de registro.

O endpoint e interno do site da B3, sem contrato publicado. Por isso get_json nunca
levanta: devolve None e registra aviso. Quem chama decide se pula a unidade ou a marca erro.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tcc_scraper.collectors.base import Collector, DomainThrottle

logger = logging.getLogger(__name__)

B3_BASE = "https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall"
B3_DOMINIO = "sistemaswebb3-listados.b3.com.br"
# O endpoint responde a navegador; o User-Agent do projeto e recusado.
USER_AGENT_B3 = "Mozilla/5.0"


def url_b3(metodo: str, params: dict, base: str = B3_BASE) -> str:
    """URL do endpoint: os parâmetros vão como JSON compacto, codificados em base64 no caminho.

    `base` troca o proxy da B3 (ex.: indexStatisticsProxy, dos índices); o padrão é o
    listedCompaniesProxy.
    """
    bruto = json.dumps(params, separators=(",", ":")).encode("utf-8")
    codificado = base64.b64encode(bruto).decode("ascii")
    return f"{base}/{metodo}/{codificado}"


def registro_hash(d: dict) -> str:
    """SHA-256 do registro em forma canônica: estável independente da ordem das chaves."""
    canonico = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def decimal_b3(s: str | None) -> Decimal | None:
    """Número no formato brasileiro ("1.234,56") para Decimal. Vazio ou inválido vira None."""
    if s is None or not s.strip():
        return None
    normalizado = s.strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(normalizado)
    except InvalidOperation:
        return None


def data_b3(s: str | None) -> date | None:
    """Data no formato dd/mm/aaaa da B3. Vazia ou inválida vira None."""
    if s is None or not s.strip():
        return None
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


class B3Collector(Collector):
    """Base dos coletores B3: configura o throttle do domínio do listedCompaniesProxy."""

    def __init__(self, throttle: DomainThrottle | None = None) -> None:
        super().__init__(throttle)
        self.throttle.set_default(self.settings.throttle_b3)
        self.throttle.configure(B3_DOMINIO, self.settings.throttle_b3)

    def get_json(self, metodo: str, params: dict, base: str = B3_BASE) -> Any | None:
        """Chama um método do endpoint e devolve o JSON, ou None em falha, HTTP de erro ou HTML."""
        url = url_b3(metodo, params, base)
        try:
            resposta = self.fetch(url, headers={"User-Agent": USER_AGENT_B3})
        except RuntimeError as exc:
            logger.warning("b3: %s falhou: %s", metodo, exc)
            return None
        if resposta.status_code >= 400:
            logger.warning("b3: %s devolveu HTTP %d", metodo, resposta.status_code)
            return None
        try:
            return resposta.json()
        except ValueError:
            # HTML (página de bloqueio ou erro) cai aqui: não é JSON.
            logger.warning("b3: %s nao devolveu JSON: %r", metodo, resposta.text[:80])
            return None
