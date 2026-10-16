"""Resolve os links de redirecionamento do Google (vertexaisearch) para a URL real.

Os links que o Gemini devolve EXPIRAM, por isso a resolucao acontece logo apos a chamada:
um HEAD sem seguir redirects devolve 302 com o header `location` = URL da materia.
Toda requisicao passa pelo DomainThrottle do projeto. Falha devolve None (degrada).
"""
from __future__ import annotations

import logging

import httpx

from tcc_scraper.collectors.base import DomainThrottle
from tcc_scraper.config import get_settings

logger = logging.getLogger(__name__)

_THROTTLE = DomainThrottle(default_delay=0.3)
_STATUS_REDIRECT = {301, 302, 303, 307, 308}


def resolver_redirect_http(url: str) -> str | None:
    """Devolve o `location` do redirect, ou None em qualquer falha ou status inesperado."""
    _THROTTLE.wait(url)
    try:
        r = httpx.head(
            url, follow_redirects=False, timeout=15,
            headers={"User-Agent": get_settings().user_agent},
        )
    except Exception as exc:  # noqa: BLE001 -- qualquer falha de rede degrada para None
        logger.warning("llm: falha ao resolver redirect %s: %s", url, exc)
        return None
    if r.status_code not in _STATUS_REDIRECT:
        logger.warning("llm: redirect %s respondeu status %s", url, r.status_code)
        return None
    location = r.headers.get("location")
    if not location:
        logger.warning("llm: redirect %s sem header location", url)
    return location or None
