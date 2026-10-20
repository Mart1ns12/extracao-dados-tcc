"""Base dos coletores da CVM (dados abertos): download de zip, leitura de CSV e normalizacao.

Os arquivos de dados abertos da CVM sao zips de CSV separados por ";" e codificados em
latin-1. O download passa por Collector.fetch (throttle e retry); o timeout e longo porque
os zips anuais chegam a dezenas de MB.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal, InvalidOperation

from tcc_scraper.collectors.base import Collector, DomainThrottle

logger = logging.getLogger(__name__)

CVM_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA"
CVM_DOMINIO = "dados.cvm.gov.br"
TIMEOUT_ZIP = 300.0


def last_modified(collector: Collector, url: str) -> str:
    """Header Last-Modified de um arquivo (HEAD, sem baixar). Vazio em falha."""
    try:
        return collector.fetch(url, method="HEAD").headers.get("Last-Modified", "")
    except RuntimeError as exc:
        logger.warning("cvm: HEAD falhou em %s: %s", url, exc)
        return ""


def ler_csvs_do_zip(
    conteudo: bytes,
    sufixos: Iterable[str],
    manter: Callable[[str, dict], bool] | None = None,
) -> dict[str, list[dict]]:
    """Lê os CSVs do zip cujo nome contenha algum dos sufixos, linha a linha (streaming).

    A chave do resultado e o sufixo casado. Todo sufixo pedido aparece no resultado,
    mesmo sem arquivo correspondente (lista vazia), para o chamador nao precisar de KeyError.
    `manter(sufixo, linha)`, se dado, descarta a linha ao ler: os CSVs de ITR chegam a
    centenas de MB como dicts, e o universo e ~18% das linhas.
    """
    pedidos = list(sufixos)
    resultado: dict[str, list[dict]] = {sufixo: [] for sufixo in pedidos}
    with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
        for nome in zf.namelist():
            casado = next((sufixo for sufixo in pedidos if sufixo in nome), None)
            if casado is None:
                continue
            with zf.open(nome) as bruto:
                texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                for linha in csv.DictReader(texto, delimiter=";"):
                    if manter is None or manter(casado, linha):
                        resultado[casado].append(linha)
    return resultado


def cnpj_digitos(s: str) -> str:
    """Só os dígitos do CNPJ: "33.000.167/0001-01" vira "33000167000101"."""
    return re.sub(r"\D", "", s or "")


def data_cvm(s: str | None) -> date | None:
    """Data no formato AAAA-MM-DD da CVM. Vazia ou inválida vira None."""
    if s is None or not s.strip():
        return None
    try:
        return date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def decimal_cvm(s: str | None) -> Decimal | None:
    """Número da CVM, que usa ponto decimal e não tem separador de milhar. Inválido vira None."""
    if s is None or not s.strip():
        return None
    try:
        return Decimal(s.strip())
    except InvalidOperation:
        return None


class CvmCollector(Collector):
    """Base dos coletores CVM: configura o throttle do domínio dados.cvm.gov.br.

    Subclasses implementam work_units() e process(); esta classe continua abstrata.
    O delay e configurado no __init__ porque o throttle só existe depois do __init__
    da base (ver DomainThrottle.set_default).
    """

    def __init__(self, throttle: DomainThrottle | None = None) -> None:
        super().__init__(throttle)
        self.throttle.set_default(self.settings.throttle_cvm)
        self.throttle.configure(CVM_DOMINIO, self.settings.throttle_cvm)

    def baixar_zip(self, url: str) -> bytes:
        """Baixa um zip inteiro para a memória, com timeout de 300 s."""
        return self.fetch(url, timeout=TIMEOUT_ZIP).content
