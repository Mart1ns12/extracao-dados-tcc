"""Testes do retry do Collector (Retry-After, teto de espera, max_attempts), do gancho
`inicio_unidade` e do AbortarColeta.

Sem rede e sem banco: cliente HTTP, sleep e checkpoint sao substituidos por monkeypatch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from tcc_scraper.collectors import base as base_mod
from tcc_scraper.collectors.base import (
    AbortarColeta,
    Collector,
    DomainThrottle,
    UnitResult,
    WorkUnit,
    retry_after_segundos,
)
from tcc_scraper.config import Settings

URL = "https://retry-teste.example/x"


class ColetorTeste(Collector):
    """Tres unidades; o comportamento de `process` e injetado por teste."""

    name = "teste_retry"

    def __init__(self, process_fn=None) -> None:
        super().__init__(throttle=DomainThrottle(0.0))
        self._process_fn = process_fn or (lambda unit: UnitResult())

    def work_units(self):
        return [WorkUnit(key=f"u{i}") for i in range(3)]

    def process(self, unit: WorkUnit) -> UnitResult:
        return self._process_fn(unit)


class ClienteFalso:
    def __init__(self, respostas: list[httpx.Response]) -> None:
        self.respostas = list(respostas)
        self.chamadas = 0

    def request(self, method, url, **kw):
        self.chamadas += 1
        return self.respostas.pop(0)


def _resp(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("GET", URL))


def _coletor_fetch(monkeypatch, respostas, *, espera_max=120.0, tentativas=4):
    c = ColetorTeste()
    c.settings = Settings(http_espera_max_s=espera_max, http_max_tentativas=tentativas)
    c.max_attempts = tentativas
    cliente = ClienteFalso(respostas)
    monkeypatch.setattr(c, "_client", cliente)
    monkeypatch.setattr(c.throttle, "wait", lambda url: None)
    esperas: list[float] = []
    monkeypatch.setattr(base_mod.time, "sleep", esperas.append)
    return c, cliente, esperas


# -- Retry-After -------------------------------------------------------------------------


def test_retry_after_em_segundos() -> None:
    assert retry_after_segundos({"retry-after": "7"}) == 7.0


def test_retry_after_em_data_http_relativa_ao_instante_informado() -> None:
    agora = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
    quando = agora + timedelta(seconds=10)
    cabecalho = {"retry-after": format_datetime(quando, usegmt=True)}

    assert retry_after_segundos(cabecalho, agora=agora) == 10.0


def test_retry_after_em_data_passada_vira_zero() -> None:
    agora = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
    quando = agora - timedelta(seconds=60)
    cabecalho = {"retry-after": format_datetime(quando, usegmt=True)}

    assert retry_after_segundos(cabecalho, agora=agora) == 0.0


def test_retry_after_invalido_ou_ausente_devolve_none() -> None:
    assert retry_after_segundos({"retry-after": "nao-e-data"}) is None
    assert retry_after_segundos({}) is None
    assert retry_after_segundos(None) is None


# -- retry em fetch ----------------------------------------------------------------------


def test_429_com_retry_after_espera_o_pedido_pelo_servidor(monkeypatch) -> None:
    c, cliente, esperas = _coletor_fetch(
        monkeypatch, [_resp(429, {"Retry-After": "5"}), _resp(200)]
    )

    resposta = c.fetch(URL)

    assert resposta.status_code == 200
    assert cliente.chamadas == 2
    assert esperas == [5.0]


def test_retry_after_maior_que_o_teto_e_limitado(monkeypatch) -> None:
    c, _, esperas = _coletor_fetch(
        monkeypatch, [_resp(429, {"Retry-After": "99999"}), _resp(200)], espera_max=120.0
    )

    c.fetch(URL)

    assert esperas == [120.0]


def test_sem_cabecalho_usa_backoff_exponencial(monkeypatch) -> None:
    c, _, esperas = _coletor_fetch(monkeypatch, [_resp(503), _resp(503), _resp(200)])

    c.fetch(URL)

    assert esperas == [2.0, 4.0]


def test_backoff_exponencial_tambem_respeita_o_teto(monkeypatch) -> None:
    c, _, esperas = _coletor_fetch(
        monkeypatch, [_resp(503), _resp(503), _resp(200)], espera_max=3.0
    )

    c.fetch(URL)

    assert esperas == [2.0, 3.0]


def test_esgota_max_attempts_e_levanta_runtime_error(monkeypatch) -> None:
    c, cliente, esperas = _coletor_fetch(
        monkeypatch, [_resp(500), _resp(500)], tentativas=2
    )

    with pytest.raises(RuntimeError, match="falha ao buscar"):
        c.fetch(URL)

    assert cliente.chamadas == 2
    assert esperas == [2.0]


# -- gancho inicio_unidade e AbortarColeta -----------------------------------------------


class ObservadorEventos:
    def __init__(self) -> None:
        self.eventos: list[str] = []

    def plano(self, total: int) -> None:
        self.eventos.append(f"plano:{total}")

    def inicio_unidade(self, key: str) -> None:
        self.eventos.append(f"inicio:{key}")

    def unidade(self, key, *, ok, itens, saturada, erro) -> None:
        self.eventos.append(f"unidade:{key}")

    def deve_parar(self) -> bool:
        return False


def _coletor_run(monkeypatch, process_fn=None):
    c = ColetorTeste(process_fn)
    monkeypatch.setattr(c, "completed_keys", lambda: set())
    registros: list[tuple] = []
    monkeypatch.setattr(
        c, "_record", lambda unit, result, error: registros.append((unit.key, result, error))
    )
    return c, registros


def test_inicio_unidade_e_chamado_antes_de_cada_unidade(monkeypatch) -> None:
    c, _ = _coletor_run(monkeypatch)
    obs = ObservadorEventos()

    c.run(observador=obs)

    assert obs.eventos == [
        "plano:3",
        "inicio:u0", "unidade:u0",
        "inicio:u1", "unidade:u1",
        "inicio:u2", "unidade:u2",
    ]


def test_abortar_coleta_propaga_e_grava_a_unidade_com_erro(monkeypatch) -> None:
    processadas: list[str] = []

    def processar(unit: WorkUnit) -> UnitResult:
        processadas.append(unit.key)
        if unit.key == "u1":
            raise AbortarColeta("bloqueio")
        return UnitResult()

    c, registros = _coletor_run(monkeypatch, processar)

    with pytest.raises(AbortarColeta, match="bloqueio"):
        c.run()

    assert processadas == ["u0", "u1"]  # u2 nao e processada
    assert [r[0] for r in registros] == ["u0", "u1"]
    assert registros[0][2] is None  # u0 ok
    assert registros[1][1] is None and registros[1][2] == "bloqueio"  # u1 erro
