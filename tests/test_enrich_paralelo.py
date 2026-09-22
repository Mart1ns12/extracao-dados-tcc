"""Testes do enriquecimento paralelo.

O ganho da paralelizacao vem de uma assimetria: todo artigo passa pelo
`news.google.com` para resolver o link, mas o download do corpo se espalha por
varios veiculos. Como o DomainThrottle e por dominio, workers concorrentes
serializam naturalmente no Google e paralelizam nos veiculos.

Sem rede: o cliente HTTP e falso. O que se testa aqui e a concorrencia e o
respeito ao throttle, nao o conteudo baixado.
"""

from __future__ import annotations

import threading
import time

import pytest

from tcc_scraper.collectors.base import DomainThrottle


class ThrottleEspiao(DomainThrottle):
    """Registra cada dominio em que houve espera, preservando o comportamento real."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.chamadas: list[str] = []
        self._reg_lock = threading.Lock()

    def wait(self, url: str) -> None:
        from urllib.parse import urlsplit

        with self._reg_lock:
            self.chamadas.append(urlsplit(url).netloc.lower())
        super().wait(url)


# --------------------------------------------------------------------------------------
# DomainThrottle sob concorrencia
# --------------------------------------------------------------------------------------
def test_throttle_serializa_o_mesmo_dominio_entre_threads() -> None:
    """Dois workers no mesmo dominio respeitam o intervalo; e o que protege o Google."""
    t = DomainThrottle(default_delay=0.25)
    marcas: list[float] = []
    lock = threading.Lock()

    def bate():
        t.wait("https://news.google.com/x")
        with lock:
            marcas.append(time.monotonic())

    threads = [threading.Thread(target=bate) for _ in range(3)]
    inicio = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    marcas.sort()
    assert marcas[-1] - inicio >= 0.5, "3 chamadas ao mesmo dominio deveriam levar >= 2 intervalos"


def test_throttle_nao_serializa_dominios_diferentes() -> None:
    """Veiculos distintos devem baixar em paralelo -- e daqui vem o ganho."""
    t = DomainThrottle(default_delay=0.4)
    dominios = [f"https://veiculo{i}.com.br/materia" for i in range(4)]

    inicio = time.monotonic()
    threads = [threading.Thread(target=t.wait, args=(d,)) for d in dominios]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    decorrido = time.monotonic() - inicio

    assert decorrido < 0.3, (
        f"dominios distintos nao deveriam esperar uns pelos outros ({decorrido:.2f}s)"
    )


# --------------------------------------------------------------------------------------
# resolve() passa a respeitar o throttle
# --------------------------------------------------------------------------------------
class ClienteFalso:
    """Responde ao fluxo do batchexecute sem tocar na rede."""

    def __init__(self, atraso: float = 0.0):
        self.atraso = atraso
        self.gets = 0
        self.posts = 0

    class _Resp:
        def __init__(self, text):
            self.text = text

    def get(self, url, **kw):
        self.gets += 1
        if self.atraso:
            time.sleep(self.atraso)
        return self._Resp('<div data-n-a-sg="assinatura" data-n-a-ts="1700000000"></div>')

    def post(self, url, **kw):
        self.posts += 1
        if self.atraso:
            time.sleep(self.atraso)
        payload = '[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://www.infomoney.com.br/materia\\"]"]]'
        return self._Resp(payload)


def test_resolve_respeita_o_throttle_quando_recebe_um() -> None:
    """Antes o resolve chamava o cliente cru, sem throttle nenhum.

    Com 60 artigos no piloto passou despercebido; com ~11 mil, e martelar o
    news.google.com sem limite.
    """
    from tcc_scraper.enrich.link_resolver import resolve

    espiao = ThrottleEspiao(default_delay=0.0)
    resolve(ClienteFalso(), "https://news.google.com/rss/articles/ABC", throttle=espiao)

    assert espiao.chamadas, "resolve nao passou pelo throttle"
    assert all(d == "news.google.com" for d in espiao.chamadas)
    assert len(espiao.chamadas) >= 2, "GET da pagina e POST do batchexecute devem ser contados"


def test_resolve_sem_throttle_continua_funcionando() -> None:
    """O parametro e opcional: nao quebra quem chama sem ele."""
    from tcc_scraper.enrich.link_resolver import resolve

    assert resolve(ClienteFalso(), "https://news.google.com/rss/articles/ABC") is not None


def test_resolve_devolve_none_em_vez_de_levantar() -> None:
    """Endpoint interno pode mudar; o pipeline degrada para manchete, nao para."""
    from tcc_scraper.enrich.link_resolver import resolve

    class ClienteQuebrado(ClienteFalso):
        def get(self, url, **kw):
            raise RuntimeError("500")

    assert resolve(ClienteQuebrado(), "https://news.google.com/x") is None


# --------------------------------------------------------------------------------------
# Configuracao dos throttles do coletor
# --------------------------------------------------------------------------------------
def test_coletor_configura_os_throttles_no_construtor() -> None:
    """Bug corrigido: `self.default_delay = ...` depois do super() nao tinha efeito.

    O DomainThrottle ja havia sido criado com o valor de classe, entao o
    throttle_article_fetch do .env era ignorado.
    """
    from tcc_scraper.config import get_settings
    from tcc_scraper.enrich.article_fetcher import ArticleBodyCollector

    s = get_settings()
    with ArticleBodyCollector() as c:
        assert c.throttle._delays.get("news.google.com") == pytest.approx(s.throttle_link_resolve)
        assert c.throttle._default == pytest.approx(s.throttle_article_fetch)


def test_workers_vem_da_configuracao() -> None:
    from tcc_scraper.config import get_settings
    from tcc_scraper.enrich.article_fetcher import ArticleBodyCollector

    with ArticleBodyCollector() as c:
        assert c.workers == get_settings().enrich_workers
    with ArticleBodyCollector(workers=3) as c:
        assert c.workers == 3


# --------------------------------------------------------------------------------------
# Processamento paralelo
# --------------------------------------------------------------------------------------
def test_lote_e_processado_em_paralelo(monkeypatch) -> None:
    """O lote inteiro nao pode custar a soma dos tempos individuais."""
    from tcc_scraper.collectors.base import WorkUnit
    from tcc_scraper.enrich import article_fetcher as af

    ATRASO = 0.2
    concorrencia_max = 0
    ativos = 0
    lock = threading.Lock()

    def fake_resolve(client, url, throttle=None):
        nonlocal ativos, concorrencia_max
        with lock:
            ativos += 1
            concorrencia_max = max(concorrencia_max, ativos)
        time.sleep(ATRASO)
        with lock:
            ativos -= 1
        return f"https://veiculo{url[-1]}.com.br/materia"

    monkeypatch.setattr(af, "resolve", fake_resolve)
    monkeypatch.setattr(af, "extract_with_trafilatura", lambda html: "x" * 300)

    with af.ArticleBodyCollector(workers=6) as c:
        monkeypatch.setattr(c, "fetch", lambda url, **kw: type("R", (), {"text": "<html/>"})())
        monkeypatch.setattr(c, "_persist", lambda bodies: None)

        artigos = [(i, f"https://news.google.com/rss/articles/{i}", "infomoney.com.br")
                   for i in range(6)]
        t0 = time.perf_counter()
        resultado = c.process(WorkUnit(key="t", payload={"artigos": artigos}))
        decorrido = time.perf_counter() - t0

    assert resultado.items_found == 6
    assert concorrencia_max > 1, "nada rodou em paralelo"
    assert decorrido < 6 * ATRASO * 0.7, f"parece sequencial ({decorrido:.2f}s)"


def test_falha_em_um_artigo_nao_derruba_o_lote(monkeypatch) -> None:
    """Um veiculo fora do ar nao pode custar os outros 199 artigos do lote."""
    from tcc_scraper.collectors.base import WorkUnit
    from tcc_scraper.enrich import article_fetcher as af

    def fake_resolve(client, url, throttle=None):
        if url.endswith("2"):
            raise RuntimeError("veiculo fora do ar")
        return "https://infomoney.com.br/materia"

    monkeypatch.setattr(af, "resolve", fake_resolve)
    monkeypatch.setattr(af, "extract_with_trafilatura", lambda html: "x" * 300)

    with af.ArticleBodyCollector(workers=4) as c:
        monkeypatch.setattr(c, "fetch", lambda url, **kw: type("R", (), {"text": "<html/>"})())
        monkeypatch.setattr(c, "_persist", lambda bodies: None)

        artigos = [(i, f"https://news.google.com/rss/articles/{i}", "infomoney.com.br")
                   for i in range(4)]
        resultado = c.process(WorkUnit(key="t", payload={"artigos": artigos}))

    assert resultado.items_found == 3, "os artigos sadios deveriam ter sido extraidos"


# --------------------------------------------------------------------------------------
# Intercalamento por dominio
# --------------------------------------------------------------------------------------
def test_lote_intercala_dominios() -> None:
    """Sem intercalar, a paralelizacao nao rende nada.

    Medido com dados reais: um lote de 24 artigos trazia 17 do mesmo veiculo, e os
    6 workers ficavam todos esperando na fila do throttle daquele dominio -- o
    paralelo saiu MAIS LENTO que o sequencial (38 s contra 32 s), so pagando o
    custo das threads. Intercalando, workers simultaneos pegam veiculos distintos.
    """
    from tcc_scraper.enrich.article_fetcher import intercalar_por_dominio

    pendentes = (
        [(i, f"u{i}", "infomoney.com.br") for i in range(5)]
        + [(10 + i, f"u{10+i}", "cnnbrasil.com.br") for i in range(5)]
        + [(20 + i, f"u{20+i}", "moneytimes.com.br") for i in range(2)]
    )

    ordenado = intercalar_por_dominio(pendentes)

    assert len(ordenado) == len(pendentes), "nenhum artigo pode se perder"
    assert {a[0] for a in ordenado} == {a[0] for a in pendentes}
    # Os 3 primeiros devem cobrir os 3 dominios: e o que da paralelismo real.
    assert len({a[2] for a in ordenado[:3]}) == 3


def test_intercalamento_preserva_tudo_com_um_unico_dominio() -> None:
    """Caso degenerado: com um veiculo so, nao ha o que intercalar."""
    from tcc_scraper.enrich.article_fetcher import intercalar_por_dominio

    pendentes = [(i, f"u{i}", "infomoney.com.br") for i in range(4)]
    assert len(intercalar_por_dominio(pendentes)) == 4


def test_intercalamento_de_lista_vazia() -> None:
    from tcc_scraper.enrich.article_fetcher import intercalar_por_dominio

    assert intercalar_por_dominio([]) == []
