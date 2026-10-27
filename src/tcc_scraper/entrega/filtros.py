"""Filtro de qualidade das noticias para a entrega (desenho 5.1 e 6.2).

Unico ponto de decisao: trocar a regra de entrega e mexer so em `aprovado_para_entrega`.
"""

from __future__ import annotations

from dataclasses import dataclass

PAPEIS_ENTREGUES = frozenset({"protagonista", "citada_com_fato"})
TIPO_ENTREGUE = "reportagem"


@dataclass(frozen=True)
class Julgamento:
    """Decisao da juiza para um par (artigo, ticker)."""

    papel: str
    tipo_conteudo: str
    evento: str
    qualidade: float
    tickers_adicionais: tuple[str, ...]


def entra_por_rotulos(papel: str, tipo_conteudo: str) -> bool:
    return tipo_conteudo == TIPO_ENTREGUE and papel in PAPEIS_ENTREGUES


def aprovado_para_entrega(julgamento: Julgamento | None, origem: str, exigir_juiza: bool) -> bool:
    """Documentos do IPE entram sempre. Sem exigir a juiza, tudo entra.

    Com a juiza exigida, so entra reportagem de protagonista ou citada com fato.
    Artigo sem julgamento fica de fora.
    """
    if origem == "cvm_ipe":
        return True
    if not exigir_juiza:
        return True
    if julgamento is None:
        return False
    return entra_por_rotulos(julgamento.papel, julgamento.tipo_conteudo)
