"""Relogio e sono de mentira, para testar pausas e throttle sem esperar de verdade.

`dormir` avanca o relogio pelo tempo pedido e registra a duracao. Quem recebe `relogio`
(ou `relogio.dormir`) como `_relogio` / `_dormir` do coletor segue o mesmo fluxo de tempo.
"""

from __future__ import annotations


class RelogioFalso:
    def __init__(self, inicio: float = 1000.0) -> None:
        self.agora = inicio
        self.dormidas: list[float] = []

    def __call__(self) -> float:
        return self.agora

    def dormir(self, segundos: float) -> None:
        self.dormidas.append(segundos)
        self.agora += segundos
