"""Agenda do daily (horario HH:MM no fuso de agora). Funcoes puras: sem banco nem relogio."""

from __future__ import annotations

from datetime import datetime, time, timedelta


def _hora(daily_hora: str) -> time:
    return datetime.strptime(daily_hora, "%H:%M").time()


def proxima_hora(agora_sp: datetime, daily_hora: str) -> datetime:
    """Proximo instante com o horario daily_hora, no fuso de agora_sp (hoje se ainda nao passou)."""
    candidato = datetime.combine(agora_sp.date(), _hora(daily_hora), tzinfo=agora_sp.tzinfo)
    if candidato < agora_sp:
        candidato += timedelta(days=1)
    return candidato


def deve_disparar_cron(agora_sp: datetime, daily_hora: str, ja_rodou_hoje: bool) -> bool:
    """True a partir do horario-alvo, se ainda nao existe execucao cron hoje."""
    if ja_rodou_hoje:
        return False
    return agora_sp.time() >= _hora(daily_hora)
