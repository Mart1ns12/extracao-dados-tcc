"""Agenda do daily: funcoes puras, sem banco."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tcc_scraper.executor.agenda import deve_disparar_cron, proxima_hora

SP = ZoneInfo("America/Sao_Paulo")


def _sp(hora: str) -> datetime:
    return datetime.strptime(f"2026-10-19 {hora}", "%Y-%m-%d %H:%M").replace(tzinfo=SP)


def test_1829_nao_dispara():
    assert deve_disparar_cron(_sp("18:29"), "18:30", ja_rodou_hoje=False) is False


def test_1830_dispara():
    assert deve_disparar_cron(_sp("18:30"), "18:30", ja_rodou_hoje=False) is True


def test_1831_com_ja_rodou_hoje_nao_repete():
    assert deve_disparar_cron(_sp("18:31"), "18:30", ja_rodou_hoje=True) is False


def test_proxima_hora_hoje_se_ainda_nao_passou():
    assert proxima_hora(_sp("15:30"), "18:30") == _sp("18:30")


def test_proxima_hora_amanha_se_ja_passou():
    esperado = datetime(2026, 10, 20, 18, 30, tzinfo=SP)
    assert proxima_hora(_sp("18:31"), "18:30") == esperado
