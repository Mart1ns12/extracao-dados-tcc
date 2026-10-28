"""Testes da causa na mensagem de falha de `fetch` e do jitter do `DomainThrottle`.

Sem rede e sem banco: cliente HTTP e sleep sao substituidos por monkeypatch.
"""

from __future__ import annotations

import random

import httpx
import pytest

from tcc_scraper.collectors import base as base_mod
from tcc_scraper.collectors.base import Collector, DomainThrottle, FalhaBusca, UnitResult, WorkUnit

URL = "https://motivo-teste.example/x"


class ColetorFetch(Collector):
    name = "teste_motivo"

    def work_units(self):
        return []

    def process(self, unit: WorkUnit) -> UnitResult:
        return UnitResult()


class ClienteQueFalha:
    """Cada requisicao devolve a resposta ou levanta a excecao dada."""

    def __init__(self, resultado) -> None:
        self.resultado = resultado

    def request(self, method, url, **kw):
        if isinstance(self.resultado, Exception):
            raise self.resultado
        return self.resultado


def _coletor_que_falha(monkeypatch, resultado) -> ColetorFetch:
    c = ColetorFetch(throttle=DomainThrottle(0.0))
    c.max_attempts = 1  # uma tentativa: o teste so quer a mensagem final
    monkeypatch.setattr(c, "_client", ClienteQueFalha(resultado))
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: None)
    return c


# -- causa na mensagem de falha ----------------------------------------------------------


def test_falha_por_status_traz_http_503_na_mensagem_e_no_motivo(monkeypatch) -> None:
    resposta = httpx.Response(503, request=httpx.Request("GET", URL))
    c = _coletor_que_falha(monkeypatch, resposta)

    with pytest.raises(FalhaBusca, match=r"falha ao buscar .* \(HTTP 503\)") as info:
        c.fetch(URL)

    assert info.value.motivo == "HTTP 503"
    assert isinstance(info.value.__cause__, httpx.HTTPStatusError)  # from last_exc mantido


def test_falha_de_rede_traz_o_nome_da_excecao(monkeypatch) -> None:
    erro = httpx.ReadTimeout("lento", request=httpx.Request("GET", URL))
    c = _coletor_que_falha(monkeypatch, erro)

    with pytest.raises(FalhaBusca, match=r"\(ReadTimeout\)") as info:
        c.fetch(URL)

    assert info.value.motivo == "ReadTimeout"
    assert info.value.__cause__ is erro


def test_falha_continua_sendo_runtime_error_para_quem_ja_captura(monkeypatch) -> None:
    c = _coletor_que_falha(monkeypatch, httpx.ConnectError("sem rota", request=None))

    with pytest.raises(RuntimeError, match="falha ao buscar"):
        c.fetch(URL)


# -- jitter do throttle ------------------------------------------------------------------


def test_jitter_fica_entre_o_intervalo_e_intervalo_mais_jitter() -> None:
    t = DomainThrottle(0.0, rng=random.Random(11))
    t.configure("jitter-faixa.invalid", 5.0, jitter=2.0)

    atrasos = [t._atraso_de("jitter-faixa.invalid") for _ in range(300)]

    assert all(5.0 <= a <= 7.0 for a in atrasos)
    assert len(set(atrasos)) > 1  # de fato varia, nao e um valor fixo


def test_dominio_sem_jitter_mantem_o_intervalo_exato() -> None:
    t = DomainThrottle(1.0, rng=random.Random(11))
    t.configure("sem-jitter.invalid", 3.0)  # jitter padrao 0

    assert {t._atraso_de("sem-jitter.invalid") for _ in range(50)} == {3.0}
    assert {t._atraso_de("nao-configurado.invalid") for _ in range(50)} == {1.0}


def test_jitter_de_um_dominio_nao_afeta_os_outros() -> None:
    t = DomainThrottle(1.0, rng=random.Random(11))
    t.configure("com-jitter.invalid", 5.0, jitter=2.0)

    assert {t._atraso_de("outro-dominio.invalid") for _ in range(50)} == {1.0}


def test_jitter_semeado_e_reproduzivel() -> None:
    a = DomainThrottle(0.0, rng=random.Random(42))
    b = DomainThrottle(0.0, rng=random.Random(42))
    a.configure("semente.invalid", 5.0, jitter=2.0)
    b.configure("semente.invalid", 5.0, jitter=2.0)

    assert [a._atraso_de("semente.invalid") for _ in range(10)] == [
        b._atraso_de("semente.invalid") for _ in range(10)
    ]


def test_wait_aplica_o_jitter_sorteado_na_espera(monkeypatch) -> None:
    relogio = [100.0]
    dormidas: list[float] = []
    monkeypatch.setattr(base_mod.time, "monotonic", lambda: relogio[0])
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: dormidas.append(s))

    dominio = "https://wait-jitter-teste.example/x"
    t = DomainThrottle(0.0, rng=random.Random(3))
    t.configure("wait-jitter-teste.example", 5.0, jitter=2.0)
    t.wait(dominio)  # primeira: reserva o horario, sem espera
    t.wait(dominio)  # segunda, no mesmo instante: dorme o intervalo sorteado

    assert len(dormidas) == 1
    assert 5.0 <= dormidas[0] <= 7.0
