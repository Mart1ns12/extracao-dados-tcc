"""Adaptador do Jev (TypeSafe AI). Desligado: a chamada real so existe apos aprovacao do dono."""
from __future__ import annotations


class JevClient:
    """Adaptador do Jev (TypeSafe AI).

    Desligado até aprovação do dono (desenho §6.3): JEV_ENABLED=false.

    Contrato futuro de `classificar_lote`:
    - entrada: lista de `{id, ticker, titulo, corpo}`;
    - saída: lista de `{id, ticker, papel, tipo_conteudo, evento, qualidade, confianca}`;
    - endpoint: `POST https://api.typesafe.ai/v1/systemone` (state + questions Choice/Score).
    """

    def __init__(self, api_key: str, modelo: str = "jev-1.13.0", timeout_s: float = 30.0) -> None:
        if not api_key:
            raise ValueError("JEV_API_KEY ausente")
        self.api_key, self.modelo, self.timeout_s = api_key, modelo, timeout_s

    def classificar_lote(self, itens: list[dict]) -> list[dict]:
        raise NotImplementedError("Jev aguarda aprovação do dono; JEV_ENABLED=false")
