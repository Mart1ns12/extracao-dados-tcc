"""Concordancia Jev x referencia gravada (`judge-news --concordancia N`), sem rede, sem banco.

A referencia e o julgamento do Gemini ja gravado (nenhuma LLM e chamada). Os fakes trocam
sessao, populacao, amostragem e Jev. O que se testa: alocacao por estrato, kappa, metricas,
entrega segura e limiar recomendado, SQL reprodutivel (compilado em MySQL) com o JOIN na
referencia, que nada vai para staging nem tentativas, que as runs saem com finalidade
`concordancia` e a run-resumo com o JSON, e a CLI.
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
from tcc_scraper.llm.jev_client import RespostaJev, rotulo_modelo_jev
from tcc_scraper.llm.juiza_prompt import VERSAO

runner = CliRunner()
REF = jc.REFERENCIA_PADRAO


def _proibido(*a, **k):  # pragma: no cover -- so dispara se a concordancia tocar staging
    raise AssertionError("concordancia nao pode gravar staging nem tentativas")


def _rotulo(mencao: bool) -> dict:
    return {"papel": "mencao" if mencao else "protagonista", "tipo_conteudo": "reportagem",
            "evento": "operacional", "tickers_adicionais": [], "qualidade": 0.8,
            "confianca": 0.9}


def _artigo(id_: int, dominio: str, ticker: str = "VALE3", *, mencao: bool = False) -> dict:
    return {"id": id_, "ticker": ticker, "title": "Vale anuncia novo diretor",
            "source_domain": dominio, "origem": "google_news", "lang": "pt",
            "trecho": "trecho de teste", "published_at": None,
            "referencia": _rotulo(mencao)}


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
                     **_rotulo(str(i["id"]) in self.mencao), "confianca": 0.95,
                     "p_entra": 0.9, "p_nao_entra": 0.05, "prob_fonte": "papel"}
                    for i in itens]
        return RespostaJev(itens=julgados, tokens_entrada=1000, latencia_ms=7,
                           bruto=[], erros=[])

    def close(self) -> None:
        pass


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "jev_enabled", True)
    monkeypatch.setattr(s, "jev_api_key", "chave-falsa")
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


def _amostra_fixa(monkeypatch, estratos: dict[tuple[int, str], list[int]],
                  mencao_ref=()) -> None:
    """Populacao e amostragem falsas: cada estrato devolve os ids pedidos e 1 descarte."""
    monkeypatch.setattr(jc, "_populacao", lambda referencia=REF: {
        chave: len(ids) for chave, ids in estratos.items()})

    def falso(ano, fonte, top, semente, k, nomes, *, referencia=REF):
        ids = estratos[(ano, fonte)][:k]
        return [_artigo(i, fonte, mencao=i in mencao_ref) for i in ids], 1

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
# Metricas e decisao (papel, tipo, entrega)
# --------------------------------------------------------------------------------------

def _par(id_: int, referencia_mencao: bool, jev_mencao: bool, confianca_jev: float = 0.95,
         origem: str = "google_news") -> dict:
    return {"id": str(id_), "ticker": "VALE3", "origem": origem,
            "referencia": {**_rotulo(referencia_mencao), "id": str(id_), "ticker": "VALE3"},
            "jev": {**_rotulo(jev_mencao), "id": str(id_), "ticker": "VALE3",
                    "confianca": confianca_jev, "p_entra": 0.9, "p_nao_entra": 0.05,
                    "prob_fonte": "papel"}}


def test_metricas_concordancia_total_decide_jev() -> None:
    pares = [_par(i, False, False) for i in range(4)]
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["papel"]["concordancia"] == 1.0
    assert m["tipo_conteudo"]["concordancia"] == 1.0
    assert m["entrega"]["concordancia"] == 1.0
    assert m["cobertura_jev"] == 1.0
    assert m["decisao_decide"] == "jev_decide"
    assert m["decisao"] == "triagem"  # 4 decididos: abaixo do N minimo


def test_metricas_discordancia_no_papel_decide_llm() -> None:
    pares = [_par(i, False, i == 0) for i in range(4)]  # 1 de 4 discorda no papel
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["papel"]["concordancia"] == pytest.approx(0.75)
    assert m["papel"]["n"] == 4
    assert m["decisao_decide"] == "llm_decide"


def test_metricas_limiar_e_o_minimo_entre_papel_e_tipo() -> None:
    # Papel 100%, mas tipo_conteudo 75%: o minimo (0,75) fica abaixo de 0,85.
    pares = [_par(i, False, False) for i in range(4)]
    for par in pares:
        par["jev"]["tipo_conteudo"] = "reportagem"
    pares[0]["jev"]["tipo_conteudo"] = "opiniao"
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["papel"]["concordancia"] == 1.0
    assert m["tipo_conteudo"]["concordancia"] == pytest.approx(0.75)
    assert m["decisao_decide"] == "llm_decide"


def test_metricas_cobertura_e_confiancas_separadas() -> None:
    pares = [_par(1, False, False, confianca_jev=0.9), _par(2, False, True, 0.3)]
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["cobertura_jev"] == 0.5
    assert m["papel"]["n"] == 2
    assert m["papel"]["n_confiantes"] == 1
    assert m["papel"]["concordancia_confiantes"] == 1.0


def test_metricas_entrega_usa_o_filtro_da_juiza() -> None:
    # Jev e referencia concordam em tudo, mas o tipo "opiniao" reprova a entrega.
    pares = [_par(1, False, False), _par(2, False, False)]
    for par in pares:
        par["jev"]["tipo_conteudo"] = "opiniao"
        par["referencia"]["tipo_conteudo"] = "opiniao"
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["entrega"]["concordancia"] == 1.0
    assert m["entrega"]["n"] == 2


def test_metricas_sem_pares_nao_decide() -> None:
    m = jc.metricas([], confianca_minima=0.5)
    assert m["papel"]["concordancia"] is None
    assert m["cobertura_jev"] is None
    assert m["decisao_decide"] == "llm_decide"
    assert m["limiar_recomendado"] is None
    assert m["decisao"] == "triagem"


# --------------------------------------------------------------------------------------
# Metricas: entrega segura e limiar recomendado
# --------------------------------------------------------------------------------------

def _ref(entra: bool, tickers=()) -> dict:
    return {"papel": "protagonista" if entra else "mencao", "tipo_conteudo": "reportagem",
            "evento": "operacional", "tickers_adicionais": list(tickers), "qualidade": 0.8,
            "confianca": 0.9}


def _jev(entra: bool, p_entra: float, p_nao_entra: float, confianca: float = 0.95) -> dict:
    return {"id": "x", "ticker": "VALE3", "papel": "protagonista" if entra else "mencao",
            "tipo_conteudo": "reportagem", "evento": "operacional", "tickers_adicionais": [],
            "qualidade": 0.8, "confianca": confianca, "p_entra": p_entra,
            "p_nao_entra": p_nao_entra, "prob_fonte": "papel"}


def _par_entrega(id_: int, ref_entra: bool, jev: dict | None, tickers=(),
                 origem: str = "google_news") -> dict:
    return {"id": str(id_), "ticker": "VALE3", "origem": origem,
            "referencia": _ref(ref_entra, tickers), "jev": jev}


def test_limiar_recomendado_e_o_menor_que_cumpre_as_metricas() -> None:
    # 100 pares: Jev e referencia dizem "entra" com p=0,90. 20 pares: Jev diz "entra" com
    # p=0,82 mas a referencia diz "nao entra". Em 0,80 os 20 entram e derrubam a precisao
    # (100/120); em 0,85 ficam indecididos e o conjunto decidido concorda 100%.
    pares = [_par_entrega(i, True, _jev(True, 0.90, 0.05)) for i in range(100)]
    pares += [_par_entrega(100 + i, False, _jev(True, 0.82, 0.10)) for i in range(20)]
    m = jc.metricas(pares, confianca_minima=0.5)

    bloco_080, bloco_085 = m["entrega_segura"][0.80], m["entrega_segura"][0.85]
    assert bloco_080["n_decididos"] == 120
    assert bloco_080["precisao_entra"] == pytest.approx(100 / 120)
    assert bloco_080["falsos_entra"] == 20
    assert bloco_085["n_decididos"] == 100
    assert bloco_085["concordancia"] == 1.0
    assert m["limiar_recomendado"] == 0.85
    assert m["decisao"] == "entrega"


def test_direcao_com_menos_de_30_decididos_nao_e_avaliada() -> None:
    # 200 concordam em "entra" (concordancia 200/210 >= 0,95). 10 pares em que o Jev diz
    # "nao entra" e a referencia diz "entra": precisao de "nao entra" = 0, mas n = 10 < 30.
    pares = [_par_entrega(i, True, _jev(True, 0.90, 0.05)) for i in range(200)]
    pares += [_par_entrega(500 + i, True, _jev(False, 0.20, 0.95)) for i in range(10)]
    m = jc.metricas(pares, confianca_minima=0.5)

    bloco = m["entrega_segura"][0.80]
    assert bloco["n_nao_entra"] == 10
    assert bloco["precisao_nao_entra"] == 0.0
    assert m["limiar_recomendado"] == 0.80
    assert m["decisao"] == "entrega"


def test_nenhum_limiar_passa_e_a_decisao_e_triagem() -> None:
    # 1000 concordam em "entra" (p=0,90). Em "nao entra" (p=0,95), 26 concordam e 4 nao:
    # precisao 26/30 = 0,87 < 0,90 com n = 30. Em 0,95 sobram so 30 decididos (< 100).
    pares = [_par_entrega(i, True, _jev(True, 0.90, 0.05)) for i in range(1000)]
    pares += [_par_entrega(2000 + i, False, _jev(False, 0.20, 0.95)) for i in range(26)]
    pares += [_par_entrega(3000 + i, True, _jev(False, 0.20, 0.95)) for i in range(4)]
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["entrega_segura"][0.80]["precisao_nao_entra"] == pytest.approx(26 / 30)
    assert m["limiar_recomendado"] is None
    assert m["decisao"] == "triagem"


def test_poucos_decididos_mantem_a_triagem() -> None:
    pares = [_par_entrega(i, True, _jev(True, 0.99, 0.01)) for i in range(50)]
    m = jc.metricas(pares, confianca_minima=0.5)

    assert m["entrega_segura"][0.80]["concordancia"] == 1.0
    assert m["limiar_recomendado"] is None
    assert m["decisao"] == "triagem"


def test_perda_de_tickers_adicionais_e_contada_nos_decididos() -> None:
    pares = [_par_entrega(i, True, _jev(True, 0.90, 0.05)) for i in range(100)]
    pares += [_par_entrega(100 + i, True, _jev(True, 0.90, 0.05), tickers=("PETR4",))
              for i in range(3)]
    m = jc.metricas(pares, confianca_minima=0.5)

    bloco = m["entrega_segura"][0.85]
    assert bloco["perda_tickers_adicionais"] == 3
    assert bloco["prob_fonte"] == {"papel": 103}


def test_falha_do_jev_conta_como_nao_coberto() -> None:
    pares = [_par_entrega(1, True, _jev(True, 0.90, 0.05)), _par_entrega(2, True, None)]
    m = jc.metricas(pares, confianca_minima=0.5)

    bloco = m["entrega_segura"][0.85]
    assert bloco["n_total"] == 2
    assert bloco["n_decididos"] == 1
    assert bloco["cobertura"] == 0.5
    assert m["com_jev"] == 1


# --------------------------------------------------------------------------------------
# SQL: referencia no banco, reprodutivel por semente, compilado em MySQL
# --------------------------------------------------------------------------------------

def _sql(consulta) -> str:
    return str(consulta.compile(dialect=mysql.dialect(),
                                compile_kwargs={"literal_binds": True}))


def test_consulta_contagens_compila_em_mysql_e_exclui_cvm_ipe() -> None:
    sql = str(jc._consulta_contagens().compile(dialect=mysql.dialect()))

    assert "year(raw_articles.published_at)" in sql
    assert "GROUP BY" in sql
    assert "raw_articles.origem != " in sql


def test_consulta_contagens_junta_so_a_referencia_da_versao_e_do_modelo() -> None:
    sql = _sql(jc._consulta_contagens("modelo-x"))

    assert "JOIN `v1-staging_noticias_julgamento`" in sql
    assert "`v1-staging_noticias_julgamento`.article_id = raw_articles.id" in sql
    assert "`v1-staging_noticias_julgamento`.ticker = staging_article_tickers.ticker" in sql
    assert f"`v1-staging_noticias_julgamento`.prompt_versao = {VERSAO}" in sql
    assert "`v1-staging_noticias_julgamento`.modelo = 'modelo-x'" in sql
    assert "raw_articles.origem != 'cvm_ipe'" in sql


def test_consulta_amostra_usa_crc32_com_a_semente_e_e_reprodutivel() -> None:
    top = {"valor.com.br"}
    sql42 = _sql(jc._consulta_amostra(2026, "valor.com.br", top, 42))

    assert "crc32(concat(raw_articles.id, '|', staging_article_tickers.ticker, '|', 42))" in sql42
    assert "ORDER BY crc32(" in sql42
    assert "LIMIT 500" in sql42
    assert "raw_articles.origem != 'cvm_ipe'" in sql42
    assert sql42 == _sql(jc._consulta_amostra(2026, "valor.com.br", top, 42))
    assert sql42 != _sql(jc._consulta_amostra(2026, "valor.com.br", top, 7))


def test_consulta_amostra_traz_a_referencia_e_o_trecho_configurado() -> None:
    sql = _sql(jc._consulta_amostra(2026, "valor.com.br", {"valor.com.br"}, 42,
                                    referencia="modelo-x", trecho=300))

    assert "`v1-staging_noticias_julgamento`.modelo = 'modelo-x'" in sql
    assert "substr(raw_article_bodies.body_text, 1, 300) AS trecho" in sql
    for campo in ("ref_papel", "ref_tipo_conteudo", "ref_evento", "ref_qualidade",
                  "ref_tickers_adicionais", "ref_confianca"):
        assert f"AS {campo}" in sql


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
    jev = JevFalso(mencao=[3])

    r = jc.medir_concordancia(10, semente=42, jev=jev)

    assert r.amostra == 10  # n >= total da populacao falsa: entra tudo
    assert r.descartados_regra == 2
    chamadas = [run for run in registro if run["modelo"] != "concordancia"]
    assert chamadas, "as chamadas pagas devem gerar runs"
    assert all(run["finalidade"] == "concordancia" for run in registro)
    assert all(run["modelo"] == rotulo_modelo_jev(settings.jev_modelo, settings.jev_trecho)
               for run in chamadas)
    assert all(run["custo_usd"] is not None for run in chamadas)
    assert r.custo_usd == pytest.approx(sum(run["custo_usd"] for run in chamadas))
    # 10 pares em lotes de 2 no Jev: 5 chamadas, e nenhuma outra LLM.
    assert jev.chamadas == 5


def test_medir_resumo_leva_o_json_sem_custo(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS, mencao_ref=[2])
    jc.medir_concordancia(10, semente=7, jev=JevFalso(mencao=[3]))

    resumo = registro[-1]
    assert resumo["modelo"] == "concordancia"
    assert resumo["custo_usd"] is None
    assert resumo["finalidade"] == "concordancia"
    corpo = json.loads(resumo["resposta_bruta"])
    assert set(corpo) == {"semente", "alocacao", "modelos", "metricas", "pares"}
    assert corpo["semente"] == 7
    assert "2026|valor.com.br" in corpo["alocacao"]
    assert corpo["modelos"] == {"referencia": REF,
                                "jev": rotulo_modelo_jev(settings.jev_modelo, settings.jev_trecho)}
    assert corpo["metricas"]["decisao"] == "triagem"
    assert "0.85" in corpo["metricas"]["entrega_segura"]
    assert len(corpo["pares"]) == 10
    assert all({"id", "ticker", "origem", "referencia", "jev"} <= set(p) for p in corpo["pares"])


def test_medir_discordancia_no_papel_fica_na_triagem(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS, mencao_ref=[2])
    r = jc.medir_concordancia(10, jev=JevFalso(mencao=[3]))

    assert r.metricas["papel"]["n"] == 10
    assert r.metricas["papel"]["concordancia"] == pytest.approx(0.8)
    assert r.metricas["decisao_decide"] == "llm_decide"
    assert r.decisao == "triagem"


def test_medir_concordancia_total_decide_jev_na_regra_antiga(monkeypatch, settings,
                                                             registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, jev=JevFalso())

    assert r.metricas["decisao_decide"] == "jev_decide"
    assert r.metricas["cobertura_jev"] == 1.0
    assert r.decisao == "triagem"  # 10 decididos: abaixo do N minimo de 100


def test_jev_com_erro_de_api_nao_derruba_a_medicao(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    rotulo = rotulo_modelo_jev(settings.jev_modelo, settings.jev_trecho)
    r = jc.medir_concordancia(10, jev=JevFalso(erro=RuntimeError("x")))

    assert any(run["status"] == "erro_api" and run["modelo"] == rotulo for run in registro)
    assert r.metricas["com_jev"] == 0
    assert r.metricas["entrega_segura"][0.85]["cobertura"] == 0.0
    assert r.decisao == "triagem"


def test_sem_jev_falha_antes_de_qualquer_chamada(monkeypatch, settings, registro) -> None:
    monkeypatch.setattr(settings, "jev_enabled", False)
    with pytest.raises(RuntimeError, match="Jev"):
        jc.medir_concordancia(5)
    assert registro == []


def test_sem_referencia_no_banco_falha_antes_do_jev(monkeypatch, settings, registro) -> None:
    monkeypatch.setattr(jc, "_populacao", lambda referencia=REF: {})
    jev = JevFalso()
    with pytest.raises(RuntimeError, match="sem julgamentos de referencia"):
        jc.medir_concordancia(5, jev=jev)
    assert jev.chamadas == 0
    assert registro == []


def test_nao_exige_gemini_api_key(monkeypatch, settings, registro) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(jc, "_populacao", lambda referencia=REF: {})
    with pytest.raises(RuntimeError, match="sem julgamentos de referencia"):
        jc.medir_concordancia(5, jev=JevFalso())


def test_n_nao_positivo_e_recusado() -> None:
    with pytest.raises(ValueError):
        jc.medir_concordancia(0, jev=JevFalso())


def test_regra_barata_descarta_do_calculo(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, jev=JevFalso())

    # Cada estrato descartou 1 par pela regra (fake), fora da amostra de 10.
    assert r.descartados_regra == 2
    assert r.amostra == 10


class _ResultadoSqlFalso:
    def __init__(self, linhas: list) -> None:
        self._linhas = linhas

    def all(self) -> list:
        return self._linhas

    def mappings(self) -> _ResultadoSqlFalso:
        return self


class _SessaoFalsa:
    """Responde as consultas falsas: a populacao e as amostras por estrato."""

    def __init__(self, populacao: list, amostras: dict) -> None:
        self.populacao, self.amostras = populacao, amostras

    def execute(self, consulta):
        if consulta == "contagens":
            return _ResultadoSqlFalso(self.populacao)
        _, ano, fonte = consulta
        return _ResultadoSqlFalso(self.amostras[(ano, fonte)])


def test_medir_com_consultas_e_sessao_falsas_so_grava_concordancia(monkeypatch, settings,
                                                                   registro) -> None:
    def linha(id_: int, fonte: str) -> dict:
        return {"h": 0, "id": id_, "ticker": "VALE3", "title": "Vale anuncia novo diretor",
                "source_domain": fonte, "origem": "google_news", "lang": "pt",
                "published_at": None, "trecho": "trecho de teste",
                "ref_papel": "protagonista", "ref_tipo_conteudo": "reportagem",
                "ref_evento": "operacional", "ref_qualidade": 0.8,
                "ref_tickers_adicionais": [], "ref_confianca": 0.9}

    populacao = [SimpleNamespace(ano=2026, fonte="valor.com.br", n=6),
                 SimpleNamespace(ano=2026, fonte="g1.com", n=4)]
    amostras = {(2026, "valor.com.br"): [linha(i, "valor.com.br") for i in range(1, 7)],
                (2026, "g1.com"): [linha(i, "g1.com") for i in range(7, 11)]}
    monkeypatch.setattr(jc, "_consulta_contagens", lambda referencia=REF: "contagens")
    monkeypatch.setattr(jc, "_consulta_amostra",
                        lambda ano, fonte, top, semente, apos=None, **kw: ("amostra", ano, fonte))
    monkeypatch.setattr(jc, "regra_barata", lambda item, ticker, nomes: None)
    sessao = _SessaoFalsa(populacao, amostras)

    @contextlib.contextmanager
    def scope():
        yield sessao

    monkeypatch.setattr(jc, "session_scope", scope)
    jev = JevFalso()

    r = jc.medir_concordancia(10, jev=jev)

    rotulo = rotulo_modelo_jev(settings.jev_modelo, settings.jev_trecho)
    assert r.amostra == 10
    assert jev.chamadas == 5
    assert all(run["finalidade"] == "concordancia" for run in registro)
    assert {run["modelo"] for run in registro} == {rotulo, "concordancia"}
    assert json.loads(registro[-1]["resposta_bruta"])["modelos"]["referencia"] == REF


def test_resumo_formata_as_linhas_da_cli(monkeypatch, settings, registro) -> None:
    _amostra_fixa(monkeypatch, ESTRATOS)
    r = jc.medir_concordancia(10, jev=JevFalso())
    linhas = r.resumo()

    assert linhas["decisao"] == "triagem"
    assert linhas["decisao_decide"] == "jev_decide"
    assert linhas["papel: concordancia"] == 1.0
    assert linhas["limiar_recomendado"] == "-"
    assert str(linhas["entrega_segura@0.85"]).startswith("cobertura=")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

class _ResultadoFalso:
    def __init__(self, decisao: str = "entrega", limiar: float | None = 0.85) -> None:
        self.decisao = decisao
        self.metricas = {"limiar_recomendado": limiar}

    def resumo(self) -> dict[str, object]:
        return {"amostra": 3, "decisao": self.decisao}


def test_cli_concordancia_repassa_n_e_semente_e_imprime_a_decisao(monkeypatch) -> None:
    chamadas: list[tuple] = []

    def falso(n, *, semente=42, **_):
        chamadas.append((n, semente))
        return _ResultadoFalso()

    monkeypatch.setattr("tcc_scraper.llm.juiza_concordancia.medir_concordancia", falso)
    r = runner.invoke(app, ["judge-news", "--concordancia", "10", "--semente", "7"])

    assert r.exit_code == 0, r.output
    assert chamadas == [(10, 7)]
    assert "decisao: entrega" in r.output
    assert "JUIZA_JEV_LIMIAR_ENTREGA=0.85" in r.output
    assert "JUIZA_JEV_DECIDE" not in r.output


def test_cli_concordancia_triagem_recomenda_triagem_sem_limiar(monkeypatch) -> None:
    monkeypatch.setattr(
        "tcc_scraper.llm.juiza_concordancia.medir_concordancia",
        lambda n, **_: _ResultadoFalso(decisao="triagem", limiar=None),
    )
    r = runner.invoke(app, ["judge-news", "--concordancia", "10"])

    assert r.exit_code == 0, r.output
    assert "JUIZA_JEV_MODO=triagem recomendado" in r.output
    assert "LIMIAR_ENTREGA" not in r.output


def test_cli_concordancia_repassa_referencia(monkeypatch) -> None:
    chamadas: list[dict] = []

    def falso(n, **kwargs):
        chamadas.append({"n": n, **kwargs})
        return _ResultadoFalso()

    monkeypatch.setattr("tcc_scraper.llm.juiza_concordancia.medir_concordancia", falso)
    r = runner.invoke(app, ["judge-news", "--concordancia", "10", "--referencia", "X"])

    assert r.exit_code == 0, r.output
    assert chamadas[0]["referencia"] == "X"


def test_cli_concordancia_sem_referencia_usa_o_padrao(monkeypatch) -> None:
    chamadas: list[dict] = []

    def falso(n, **kwargs):
        chamadas.append(kwargs)
        return _ResultadoFalso()

    monkeypatch.setattr("tcc_scraper.llm.juiza_concordancia.medir_concordancia", falso)
    runner.invoke(app, ["judge-news", "--concordancia", "10"])

    assert chamadas[0]["referencia"] == jc.REFERENCIA_PADRAO


def test_cli_referencia_sem_concordancia_recusa(monkeypatch) -> None:
    monkeypatch.setattr("tcc_scraper.llm.juiza_concordancia.medir_concordancia",
                        lambda *a, **k: pytest.fail("nao deveria medir"))
    assert runner.invoke(app, ["judge-news", "--referencia", "X"]).exit_code != 0


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
