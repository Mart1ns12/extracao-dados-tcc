"""Executor: o worker que atende o painel (fila de comandos, ack, heartbeat e agenda).

O painel nunca chama o scraper. Ele grava comandos numa fila e este processo os busca por HTTP.
"""

from __future__ import annotations
