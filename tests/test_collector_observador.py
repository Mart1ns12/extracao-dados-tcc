"""Testes do observador de progresso, do reprocessamento de erros, do HEAD e do throttle.

Sem banco e sem rede: o checkpoint (`_record`, `completed_keys`, `erro_keys`) e o cliente
HTTP sao substituidos por monkeypatch.
"""

from __future__ import annotations

from types import SimpleNamespace

from tcc_scraper.collectors import base as base_mod
from tcc_scraper.collectors.base import Collector, DomainThrottle, UnitResult, WorkUnit


class ColetorTeste(Collector):
    """Tres unidades; a segunda falha. Usado para observar `run` sem rede nem banco."""

    name = "teste_observador"

    def work_units(self):
        return [WorkUnit(key=f"u{i}") for i in range(3)]

    def process(self, unit: WorkUnit) -> UnitResult:
        if unit.key == "u1":
            raise RuntimeError("falhou")
        return UnitResult(items_found=2, hit_cap=unit.key == "u0")


class Observador:
    def __init__(self, parar_na_chamada: int | None = None) -> None:
        self.planos: list[int] = []
        self.unidades: list[tuple] = []
        self.chamadas_parar = 0
        self.parar_na_chamada = parar_na_chamada

    def plano(self, total: int) -> None:
        self.planos.append(total)

    def unidade(self, key, *, ok, itens, saturada, erro) -> None:
        self.unidades.append((key, ok, itens, saturada, erro))

    def deve_parar(self) -> bool:
        self.chamadas_parar += 1
        return self.parar_na_chamada is not None and self.chamadas_parar >= self.parar_na_chamada


def _coletor(monkeypatch, erros: set[str] | None = None) -> ColetorTeste:
    c = ColetorTeste(throttle=DomainThrottle(0.0))
    monkeypatch.setattr(c, "_record", lambda unit, result, error: None)
    monkeypatch.setattr(c, "completed_keys", lambda: set())
    monkeypatch.setattr(c, "erro_keys", lambda: set(erros or ()))
    return c


def test_observador_recebe_plano_e_resultado_de_cada_unidade(monkeypatch) -> None:
    c = _coletor(monkeypatch)
    obs = Observador()

    stats = c.run(observador=obs)

    assert obs.planos == [3]
    assert obs.unidades == [
        ("u0", True, 2, True, None),
        ("u1", False, 0, False, "falhou"),
        ("u2", True, 2, False, None),
    ]
    assert stats == {
        "total": 3, "ok": 2, "erro": 1, "itens": 4, "saturadas": 1, "interrompido": 0,
    }


def test_deve_parar_interrompe_entre_unidades(monkeypatch) -> None:
    c = _coletor(monkeypatch)
    obs = Observador(parar_na_chamada=2)  # 1a consulta: segue; 2a consulta: para

    stats = c.run(observador=obs)

    assert [u[0] for u in obs.unidades] == ["u0"]
    assert stats["interrompido"] == 1
    assert stats["ok"] == 1 and stats["erro"] == 0


def test_apenas_erros_reexecuta_so_as_unidades_com_erro(monkeypatch) -> None:
    c = _coletor(monkeypatch, erros={"u1"})
    obs = Observador()

    stats = c.run(apenas_erros=True, observador=obs)

    assert obs.planos == [1]
    assert [u[0] for u in obs.unidades] == ["u1"]
    assert stats["total"] == 1 and stats["erro"] == 1


def test_fetch_aceita_metodo_head(monkeypatch) -> None:
    chamadas: list[tuple] = []

    class ClienteFalso:
        def request(self, method, url, **kw):
            chamadas.append((method, url, kw))
            return SimpleNamespace(status_code=200)

    c = _coletor(monkeypatch)
    monkeypatch.setattr(c, "_client", ClienteFalso())
    monkeypatch.setattr(c.throttle, "wait", lambda url: None)

    c.fetch("https://exemplo.test/arquivo", method="HEAD", timeout=5)

    assert chamadas == [("HEAD", "https://exemplo.test/arquivo", {"timeout": 5})]


def test_dois_throttles_no_mesmo_dominio_compartilham_o_relogio(monkeypatch) -> None:
    relogio = [100.0]
    dormidas: list[float] = []

    def fake_sleep(segundos: float) -> None:
        dormidas.append(segundos)
        relogio[0] += segundos

    monkeypatch.setattr(base_mod.time, "monotonic", lambda: relogio[0])
    monkeypatch.setattr(base_mod.time, "sleep", fake_sleep)

    url = "https://dominio-compartilhado-teste.example/x"
    DomainThrottle(1.0).wait(url)  # primeira: sem espera
    assert dormidas == []
    DomainThrottle(1.0).wait(url)  # outra instancia, mesmo dominio, mesmo instante: dorme
    assert dormidas == [1.0]


def test_sono_acontece_fora_do_lock(monkeypatch) -> None:
    """Dormir sob o lock travaria os outros dominios do processo."""
    relogio = [200.0]
    travado_durante_sono: list[bool] = []

    def fake_sleep(segundos: float) -> None:
        travado_durante_sono.append(DomainThrottle._lock.locked())
        relogio[0] += segundos

    monkeypatch.setattr(base_mod.time, "monotonic", lambda: relogio[0])
    monkeypatch.setattr(base_mod.time, "sleep", fake_sleep)

    url = "https://dominio-lock-teste.example/x"
    DomainThrottle(1.0).wait(url)
    DomainThrottle(1.0).wait(url)

    assert travado_durante_sono == [False]
