"""Resolucao do link opaco do Google News para a URL real do veiculo.

O <link> do RSS do Google News NAO e uma URL disfarcada: e um id opaco
(`CBMi...` -> `AU_yqL...`) que nao contem o endereco do artigo. Confirmado:
seguir redirects fica no proprio news.google.com e decodificar o base64 nao
revela URL nenhuma. A unica via e o endpoint interno `batchexecute`.

Medicao do spike (2026-09-07, 42 links reais de 3 janelas distintas): 42/42 = 100%.
O plano exigia >= 70% para manter o enriquecimento hibrido -- criterio atendido.

Como e um endpoint interno e nao documentado, pode mudar sem aviso. Por isso
`resolve` devolve None em vez de levantar excecao: o pipeline degrada para
manchete-apenas em vez de parar.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

_SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_TIMESTAMP_RE = re.compile(r'data-n-a-ts="([^"]+)"')

# Envelope que o front-end do Google News envia. Os literais "X" sao placeholders
# que o proprio servico ignora; o que importa sao id, timestamp e assinatura.
_REQUEST_SHELL = [
    "garturlreq",
    [
        [
            "X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
            None, None, None, None, None, 0, 1,
        ],
        "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
    ],
]


def extract_google_id(google_url: str) -> str:
    """Extrai o id do artigo da URL do Google News."""
    return google_url.rstrip("/").split("/")[-1].split("?")[0]


def resolve(client: httpx.Client, google_url: str) -> str | None:
    """Devolve a URL real do artigo, ou None se nao for possivel resolver.

    Nunca levanta: uma falha aqui significa apenas que o artigo entra no dataset
    com a manchete, nao que a coleta deve parar.
    """
    try:
        article_id = extract_google_id(google_url)
        page = client.get(google_url).text

        signature = _SIGNATURE_RE.search(page)
        timestamp = _TIMESTAMP_RE.search(page)
        if not (signature and timestamp):
            logger.debug("assinatura ausente para %s", article_id)
            return None

        inner = json.dumps(
            [*_REQUEST_SHELL, article_id, int(timestamp.group(1)), signature.group(1)]
        )
        payload = json.dumps([[["Fbv4je", inner, None, "generic"]]])

        response = client.post(
            BATCHEXECUTE_URL,
            data={"f.req": payload},
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        return _parse_batchexecute(response.text)
    except Exception as exc:
        logger.debug("falha ao resolver %s: %s", google_url, exc)
        return None


def _parse_batchexecute(body: str) -> str | None:
    """Extrai a URL da resposta do batchexecute.

    O corpo vem como linhas prefixadas por contagem, cada uma um JSON aninhado
    cuja carga util e ela propria uma string JSON.
    """
    for line in body.splitlines():
        if "garturlres" not in line:
            continue
        try:
            url = json.loads(json.loads(line)[0][2])[1]
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None
