"""Concordancia Jev x Gemini (`judge-news --concordancia N`), sem rede, sem banco.

Os fakes trocam sessao, populacao, amostragem, Jev e Gemini. O que se testa: alocacao por
estrato, kappa, metricas e decisao, SQL reprodutivel (compilado em MySQL), que nada vai para
staging nem tentativas, que as runs saem com finalidade `concordancia` e a run-resumo com o JSON,
e a CLI.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql
from typer.testing import CliRunner

from tcc_scraper.cli import app
from tcc_scraper.config import get_settings
from tcc_scraper.llm import juiza
from tcc_scraper.llm import juiza_concordancia as jc
from tcc_scraper.llm.jev_client import RespostaJev

runner = CliRunner()


def _proibido(*a, **k):  # pragma: no cover -- so dispara se a concordancia tocar staging
    raise AssertionError("concordancia nao pode gravar staging nem tentativas")


def _rotulo(mencao: bool) -> dict:
    return {"papel": "mencao" if mencao else "protagonista", "tipo_conteudo": "reportagem",
            "evento": "operacional", "tickers_adicionais": [], "qualidade": 0.8,
            "confianca": 0.9}


def _artigo(id_: int, dominio: str, ticker: str = "VALE3") -> dict:
    return {"id": id_, "ticker": ticker, "title": "Vale anuncia novo diretor",
            "source_domain": dominio, "origem": "google_news", "lang": "pt",
            "trecho": "trecho de teste", "published_at": None}


class GeminiFalso:
    def __init__(self, *, mencao=(), erro: Exception | None = None) -> None:
        self.mencao = {str(i) for i in mencao}
        self.erro = erro
        self.chamadas = 0

    def classificar(self, texto: str, schema: dict):
        self.chamadas += 1
        if self.erro is not None:
            raise self.erro
        itens = json.loads(texto)
        resposta = [{"id": str(i["id"]), "ticker": i["ticker"],
                     **_rotulo(str(i["id"]) in self.mencao)} for i in itens]
        return SimpleNamespace(texto=json.dumps({"itens": resposta}), modelo="gemini-falso",
                               latencia_ms=5, tokens_entrada=1000, tokens_saida=100)


class JevFalso:
    def __init__(self, *, mencao=(), erro: Exception | None = None) -> None:
        self.mencao = {str(i) for i in mencao}
        self.erro = erro
        self.chamadas = 0

    def classificar_lote(self, itens: list[dict]) -> RespostaJev:
        self.chamadas += 1
        if self.erro is not None:
            raise self.erro
        julgados = [{"id": str(i["id"]), "ticker": i["ticker"],
                     **_rotulo(str(i["id"]) in self.mencao), "confianca": 0.95}
                    for i in itens]
        return RespostaJev(itens=julgados, tokens_entrada=1000, latencia_ms=7,
                           bruto=[], erros=[])

    def close(self) -> None:
        pass


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "gemini_api_key", "chave-falsa")
    monkeypatch.setattr(s, "jev_enabled", True)
    monkeypatch.setattr(s, "jev_api_key", "chave-falsa")
    monkeypatch.setattr(s, "juiza_lote", 3)
    monkeypatch.setattr(s, "jev_lote", 2)
    monkeypatch.setattr(s, "juiza_confianca_minima", 0.5)
    return s


@pytest.fixture
def registro(monkeypatch):
    """Sessao, prompt e _gravar_run falsos. Devolve a lista de runs gravadas."""
    runs: list[dict] = []

    @contextlib.contextmanager
    def scope():
        yield None

    def gravar_run(s, **campos):
        runs.append(campos)
        return len(runs)

    monkeypatch.setattr(jc, "session_scope", scope)
    monkeypatch.setattr(jc, "garantir_prompt", lambda s, p: 7)
    monkeypatch.setattr(jc, "_gravar_run", gravar_run)
    monkeypatch.setattr(jc, "renderizar_prompt_juiza", lambda itens: SimpleNamespace(
        texto=json.dumps(itens), schema={}, template="", sha256="x", nome="juiza", versao=3))
    # Nada da concordancia pode chegar a staging nem a tentativas.
    monkeypatch.setattr(juiza, "bulk_upsert", _proibido)
    monkeypatch.setattr(juiza, "_gravar_staging", _proibido)
    monkeypatch.setattr(juiza, "_registrar_tentativas", _proibido)
    return runs


def _amostra_fixa(monkeypatch, estratos: dict[tuple[int, str], list[int]]) -> None:
    """Populacao e amostragem falsas: cada estrato devolve os ids pedidos e 1 descarte."""
    monkeypatch.setattr(jc, "_populacao", lambda: {
        chave: len(ids) for chave, ids in estratos.items()})

    def falso(ano, fonte, top, semente, k, nomes):
        return [_artigo(i, fonte) for i in estratos[(ano, fonte)][:k]], 1

    monkeypatch.setattr(jc, "_amostrar_estrato", falso)


ESTRATOS = {(2026, "valor.com.br"): [1, 2, 3, 4, 5, 6], (2026, "g1.com"): [7, 8, 9, 10]}


# --------------------------------------------------------------------------------------
# Alocacao e kappa
# --------------------------------------------------------------------------------------

def test_alocar_soma_n_e_respeita_o_tamanho_do_estrato() -> None:
    contagens = {(2026, "valor.com.br"): 500, (2026, "g1.com"): 300, (2026, "x.com"): 5,
                 (2025, "valor.com.br"): 50}
    a = jc.alocar(contagens, 40, top_fontes=2)

    assert sum(a.values()) == 40
    assert a[(2026, "outras")] == 1  # x.com cai no balde "outras" (fora do top 2)
    assert a[(2026, "outras")] <= 5  # nunca passa do tamanho do estrato
    assert a[(2025, "valor.com.br")] <= 50
    assert a[(2026, "valor.com.br")] <= 500


def test_alocar_minimo_um_por_estrato_nao_vazio() -> None:
    contagens = {(2026, "a.com"): 10000, (2026, "b.com"): 1, (2026, "c.com"): 1}
    a = jc.alocar(contagens, 10)

    assert a[(2026, "b.com")] == 1 and a[(2026, "c.com")] == 1
    assert sum(a.values()) == 10


def test_alocar_com_n_maior_que_o_total_devolve_tudo() -> None:
    contagens = {(2026, "a.com"): 3, (2025, "a.com"): 2}
    assert jc.alocar(contagens, 100) == {(2025, "a.com"): 2, (2026, "a.com"): 3}


def test_alocar_com_mais_estratos_que_n_pega_os_maiores() -> None:
    contagens = {(2026, "a.com"): 9, (2026, "b.com"): 5, (2026, "c.com"): 1}
    assert jc.alocar(contagens, 2) == {(2026, "a.com"): 1, (2026, "b.com"): 1}


def test_alocar_n_zero_ou_populacao_vazia_devolve_vazio() -> None:
    assert jc.alocar({(2026, "a.com"): 3}, 0) == {}
    assert jc.alocar({}, 5) == {}


def test_alocar_e_deterministica() -> None:
    contagens = {(2026, f"d{i}.com"): 7 + i for i in range(12)}
    assert jc.alocar(contagens, 30) == jc.alocar(dict(contagens), 30)


def test_kappa_perfeito_e_no_acaso() -> None:
    assert jc.kappa_cohen([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0
    assert jc.kappa_cohen([1, 1, 0, 0], [1, 0, 1, 0]) == pytest.approx(0.0)


def test_kappa_valor_conhecido_e_sem_pares() -> None:
    assert jc.kappa_cohen([1, 1, 1, 1, 0, 0, 0, 0], [1, 1, 1, 0, 0, 0, 0, 1]) == pytest.approx(0.5)
    assert jc.kappa_cohen([], []) is None


# --------------------------------------------------------------------------------------
# Metricas e decisao
# --------------------------------------------------------------------------------------

def _par(id_: int, gemini_mencao: bool, jev_mencao: bool, confianca_jev: float = 0.95,
         origem: str = "google_news") -> dict:
    return {"id": str(id_), "ticker": "VALE3", "origem": origem,
            "gemini": {**_rotulo(gemini_mencao), "id": str(id_), "ticker": "VALE3"},
            "jev": {**_rotulo(jev_mencao), "id": str(id_), "ticker": "VALE3",
                    "confianca": confianca_jev}}


def test_metricas_concordancia_total_decide_jev() -> None:
    pares = [_par(i, False, False) for i in range(4)]
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["papel"]["concordancia"] == 1.0
    assert m["tipo_conteudo"]["concordancia"] == 1.0
    assert m["entrega"]["concordancia"] == 1.0
    assert m["cobertura_jev"] == 1.0
    assert m["decisao"] == "jev_decide"


def test_metricas_discordancia_no_papel_decide_gemini() -> None:
    pares = [_par(i, False, i == 0) for i in range(4)]  # 1 de 4 discorda no papel
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["papel"]["concordancia"] == pytest.approx(0.75)
    assert m["papel"]["n"] == 4
    assert m["decisao"] == "gemini_decide"


def test_metricas_limiar_e_o_minimo_entre_papel_e_tipo() -> None:
    # Papel 100%, mas tipo_conteudo 75%: o minimo (0,75) fica abaixo de 0,85.
    pares = [_par(i, False, False) for i in range(4)]
    for par in pares:
        par["jev"]["tipo_conteudo"] = "reportagem"
    pares[0]["jev"]["tipo_conteudo"] = "opiniao"
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["papel"]["concordancia"] == 1.0
    assert m["tipo_conteudo"]["concordancia"] == pytest.approx(0.75)
    assert m["decisao"] == "gemini_decide"


def test_metricas_cobertura_e_confiancas_separadas() -> None:
    pares = [_par(1, False, False, confianca_jev=0.9), _par(2, False, True, 0.3)]
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["cobertura_jev"] == 0.5
    assert m["papel"]["n"] == 2
    assert m["papel"]["n_confiantes"] == 1
    assert m["papel"]["concordancia_confiantes"] == 1.0


def test_metricas_entrega_usa_o_filtro_da_juiza() -> None:
    # Jev e Gemini concordam em tudo, mas o tipo "opiniao" reprova a entrega.
    pares = [_par(1, False, False), _par(2, False, False)]
    for par in pares:
        par["jev"]["tipo_conteudo"] = "opiniao"
        par["gemini"]["tipo_conteudo"] = "opiniao"
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["entrega"]["concordancia"] == 1.0
    assert m["entrega"]["n"] == 2


def test_metricas_sem_pares_nao_decide_jev() -> None:
    m = jc.metricas([], confianca_minima=0.5)
    assert m["papel"]["concordancia"] is None
    assert m["cobertura_jev"] is None
    assert m["decisao"] == "gemini_decide"


# --------------------------------------------------------------------------------------
# SQL: reprodutivel por semente, compilado em MySQL
# --------------------------------------------------------------------------------------

def _sql(consulta) -> str:
    return str(consulta.compile(dialect=mysql.dialect(),
                                compile_kwargs={"literal_binds": True}))


def test_consulta_contagens_compila_em_mysql_e_exclui_cvm_ipe() -> None:
    sql = str(jc._consulta_contagens().compile(dialect=mysql.dialect()))

    assert "year(raw_articles.published_at)" in sql
    assert "GROUP BY" in sql
    assert "raw_articles.origem != " in sql


def test_consulta_amostra_usa_crc32_com_a_semente_e_e_reprodutivel() -> None:
    top = {"valor.com.br"}
    sql42 = _sql(jc._consulta_amostra(2026, "valor.com.br", top, 42))

    assert "crc32(concat(raw_articles.id, '|', staging_article_tickers.ticker, '|', 42))" in sql42
    assert "ORDER BY crc32(" in sql42
    assert "LIMIT 500" in sql42
    assert "raw_articles.origem != 'cvm_ipe'" in sql42
    assert sql42 == _sql(jc._consulta_amostra(2026, "valor.com.br", top, 42))
    assert sql42 != _sql(jc._consulta_amostra(2026, "valor.com.br", top, 7))


def test_consulta_amostra_do_balde_outras_exclui_o_top_e_keyset_compila() -> None:
    sql = _sql(jc._consulta_amostra(2026, "outras", {"b.com", "a.com"}, 42,
                                    apos=(123, 9, "VALE3")))

    assert "NOT IN ('a.com', 'b.com')" in sql
    assert "> (123, 9, 'VALE3')" in sql


# --------------------------------------------------------------------------------------
# medir_concordancia: fluxo com fakes
# --------------------------------------------------------------------------------------

def test_medir_grava_so_runs_de_concordancia_e_o_resumo(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    gemini = GeminiFalso(mencao=[2])
    jev = JevFalso(mencao=[3])

    r = jc.medir_concordancia(10, semente=42, cliente=gemini, jev=jev)

    assert r.amostra == 10  # n >= total da populacao falsa: entra tudo
    assert r.descartados_regra == 2
    chamadas = [run for run in registro if run["modelo"] != "concordancia"]
    assert chamadas, "as chamadas pagas devem gerar runs"
    assert all(run["finalidade"] == "concordancia" for run in registro)
    assert all(run["custo_usd"] is not None for run in chamadas)
    assert r.custo_usd == pytest.approx(sum(run["custo_usd"] for run in chamadas))
    # 10 pares: lotes de 2 no Jev (5 chamadas) e de 3 no Gemini (4 chamadas).
    assert jev.chamadas == 5 and gemini.chamadas == 4


def test_medir_resumo_leva_o_json_sem_custo(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    jc.medir_concordancia(10, semente=7, cliente=GeminiFalso(mencao=[2]),
                          jev=JevFalso(mencao=[3]))

    resumo = registro[-1]
    assert resumo["modelo"] == "concordancia"
    assert resumo["custo_usd"] is None
    assert resumo["finalidade"] == "concordancia"
    corpo = json.loads(resumo["resposta_bruta"])
    assert set(corpo) == {"semente", "alocacao", "modelos", "metricas", "pares"}
    assert corpo["semente"] == 7
    assert "2026|valor.com.br" in corpo["alocacao"]
    assert corpo["metricas"]["decisao"] == "gemini_decide"
    assert len(corpo["pares"]) == 10
    assert all({"id", "ticker", "origem", "gemini", "jev"} <= set(p) for p in corpo["pares"])


def test_medir_discordancia_no_papel_manda_ao_gemini(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, cliente=GeminiFalso(mencao=[2]), jev=JevFalso(mencao=[3]))

    assert r.metricas["papel"]["n"] == 10
    assert r.metricas["papel"]["concordancia"] == pytest.approx(0.8)
    assert r.decisao == "gemini_decide"


def test_medir_concordancia_total_decide_jev(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, cliente=GeminiFalso(), jev=JevFalso())

    assert r.decisao == "jev_decide"
    assert r.metricas["cobertura_jev"] == 1.0


def test_gemini_com_erro_de_api_gera_run_erro_sem_pares_comparaveis(
        monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, cliente=GeminiFalso(erro=RuntimeError("503")),
                              jev=JevFalso())

    erros = [run for run in registro if run["status"] == "erro_api"]
    assert erros and all(run["finalidade"] == "concordancia" for run in erros)
    assert r.metricas["ambos"] == 0
    assert r.metricas["papel"]["concordancia"] is None
    assert r.decisao == "gemini_decide"


def test_jev_com_erro_de_api_nao_derruba_a_medicao(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, cliente=GeminiFalso(), jev=JevFalso(erro=RuntimeError("x")))

    assert any(run["status"] == "erro_api" and run["modelo"].startswith("jev")
               for run in registro)
    assert r.metricas["com_jev"] == 0
    assert r.decisao == "gemini_decide"


def test_sem_jev_ou_sem_gemini_falha_antes_de_qualquer_chamada(monkeypatch, settings,
                                                                registro) -> None:
    monkeypatch.setattr(settings, "jev_enabled", False)
    with pytest.raises(RuntimeError, match="Jev"):
        jc.medir_concordancia(5, cliente=GeminiFalso())

    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    with pytest.raises(RuntimeError, match="Gemini"):
        jc.medir_concordancia(5, jev=JevFalso())
    assert registro == []


def test_n_nao_positivo_e_recusado() -> None:
    with pytest.raises(ValueError):
        jc.medir_concordancia(0, cliente=GeminiFalso(), jev=JevFalso())


def test_regra_barata_descarta_do_calculo(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, cliente=GeminiFalso(), jev=JevFalso())

    # Cada estrato descartou 1 par pela regra (fake), fora da amostra de 10.
    assert r.descartados_regra == 2
    assert r.amostra == 10


def test_resumo_formata_as_linhas_da_cli(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, cliente=GeminiFalso(), jev=JevFalso())
    linhas = r.resumo()

    assert linhas["decisao"] == "jev_decide"
    assert linhas["papel: concordancia"] == 1.0
    assert linhas["limiar_decisao"] == 0.85


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

class _ResultadoFalso:
    decisao = "jev_decide"

    def resumo(self) -> dict[str, object]:
        return {"amostra": 3, "decisao": "jev_decide"}


def test_cli_concordancia_repassa_n_e_semente_e_imprime_a_decisao(monkeypatch) -> None:
    chamadas: list[tuple] = []

    def falso(n, *, semente=42, **_):
        chamadas.append((n, semente))
        return _ResultadoFalso()

    monkeypatch.setattr("tcc_scraper.llm.juiza_concordancia.medir_concordancia", falso)
    r = runner.invoke(app, ["judge-news", "--concordancia", "10", "--semente", "7"])

    assert r.exit_code == 0, r.output
    assert chamadas == [(10, 7)]
    assert "decisao: jev_decide" in r.output


def test_cli_concordancia_padrao_de_semente_e_42(monkeypatch) -> None:
    chamadas: list[tuple] = []

    def falso(n, *, semente=42, **_):
        chamadas.append((n, semente))
        return _ResultadoFalso()

    monkeypatch.setattr("tcc_scraper.llm.juiza_concordancia.medir_concordancia", falso)
    runner.invoke(app, ["judge-news", "--concordancia", "4"])

    assert chamadas == [(4, 42)]


def test_cli_concordancia_nao_combina_com_status_nem_backlog(monkeypatch) -> None:
    monkeypatch.setattr("tcc_scraper.llm.juiza_concordancia.medir_concordancia",
                        lambda *a, **k: pytest.fail("nao deveria medir"))
    assert runner.invoke(app, ["judge-news", "--concordancia", "5", "--status"]).exit_code != 0
    assert runner.invoke(app, ["judge-news", "--concordancia", "5", "--backlog"]).exit_code != 0


def test_cli_concordancia_exige_n_positivo() -> None:
    assert runner.invoke(app, ["judge-news", "--concordancia", "0"]).exit_code != 0
