"""Orquestracao da juiza: regra -> Jev (opcional) -> LLM, com persistencia e retomada.

Os testes puros trocam pendentes, sessao e bulk_upsert por versoes falsas: nada toca banco
nem rede. O teste com banco (MySQL do .env, opt-in via TCC_TESTES_COM_BANCO=1) usa a
fixture `engine` de conftest.py e filtra os pendentes para os artigos que ele cria.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tcc_scraper.config import get_settings
from tcc_scraper.db.models import Article, ArticleBody, ArticleTicker, url_hash
from tcc_scraper.db.models_fontes import RawLlmJuizaRun, StagingNoticiaJulgamento
from tcc_scraper.llm import juiza, juiza_custo
from tcc_scraper.llm.jev_client import JevFalhaLote, RespostaJev, rotulo_modelo_jev
from tcc_scraper.llm.openai_client import SemCreditoLLM

# --------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------

def _item(id_: int, ticker: str = "VALE3", *, title: str = "Vale anuncia novo diretor",
          origem: str = "google_news", dominio: str = "valor.com.br") -> dict:
    return {"id": id_, "ticker": ticker, "title": title, "source_domain": dominio,
            "origem": origem, "lang": "pt", "trecho": "trecho de teste"}


def _julgamento(id_, ticker: str, **extra) -> dict:
    base = {"id": str(id_), "ticker": ticker, "papel": "protagonista",
            "tipo_conteudo": "reportagem", "evento": "operacional",
            "tickers_adicionais": [], "qualidade": 0.8, "confianca": 0.9}
    return {**base, **extra}


def _jev_par(id_, ticker: str = "VALE3", *, p_entra: float, p_nao_entra: float,
             **extra) -> dict:
    """Julgamento do Jev com as probabilidades que o modo entrega usa."""
    return {**_julgamento(id_, ticker, **extra), "p_entra": p_entra,
            "p_nao_entra": p_nao_entra, "prob_fonte": "probabilidades"}


class ClienteFalso:
    """Imita o cliente da LLM (classificar). Responde com `respostas` (lista de julgamentos)."""

    def __init__(self, respostas: list[dict] | None = None, *, erro: Exception | None = None,
                 texto: str | None = None, tokens_cache: int | None = None) -> None:
        self.respostas = respostas or []
        self.erro = erro
        self.texto = texto
        self.tokens_cache = tokens_cache
        self.chamadas = 0
        self.prompts: list[str] = []

    def classificar(self, prompt: str, schema: dict):
        self.chamadas += 1
        self.prompts.append(prompt)
        if self.erro is not None:
            raise self.erro
        texto = self.texto if self.texto is not None else json.dumps(
            {"itens": self.respostas}, ensure_ascii=False)
        return SimpleNamespace(texto=texto, modelo="modelo-falso", latencia_ms=5,
                               tokens_entrada=100, tokens_saida=20,
                               tokens_cache=self.tokens_cache)


class JevFalso:
    """Imita JevClient.classificar_lote: devolve RespostaJev, ou levanta `erro`."""

    def __init__(self, itens: list[dict] | None = None, *, erros: list[dict] | None = None,
                 erro: Exception | None = None, tokens_entrada: int = 1000) -> None:
        self.itens = itens or []
        self.erros = erros or []
        self.erro = erro
        self.tokens_entrada = tokens_entrada
        self.chamadas = 0
        self.recebidos: list[list[dict]] = []
        self.fechado = False

    def classificar_lote(self, itens: list[dict]) -> RespostaJev:
        self.chamadas += 1
        self.recebidos.append(list(itens))
        if self.erro is not None:
            raise self.erro
        return RespostaJev(itens=list(self.itens), tokens_entrada=self.tokens_entrada,
                           latencia_ms=7, bruto=[], erros=list(self.erros))

    def close(self) -> None:
        self.fechado = True


class ObservadorFalso:
    def __init__(self, parar: bool = False) -> None:
        self.parar = parar
        self.planos: list[int] = []
        self.unidades: list[tuple] = []

    def plano(self, n: int) -> None:
        self.planos.append(n)

    def unidade(self, nome: str, **kw) -> None:
        self.unidades.append((nome, kw))

    def deve_parar(self) -> bool:
        return self.parar


class _SessaoFalsa:
    def __init__(self) -> None:
        self.objetos: list = []
        self._proximo = 0

    def add(self, obj) -> None:
        self.objetos.append(obj)

    def flush(self) -> None:
        for obj in self.objetos:
            if getattr(obj, "id", None) is None:
                self._proximo += 1
                obj.id = self._proximo

    def execute(self, *a, **k):  # pragma: no cover -- nao deve ser chamado nos testes puros
        raise AssertionError("consulta direta ao banco num teste puro")


@pytest.fixture
def gravacoes(monkeypatch):
    """Substitui sessao, bulk_upsert, garantir_prompt e o gasto do banco (zero).

    Devolve o que seria gravado.
    """
    registro = SimpleNamespace(upserts=[], sessoes=[])

    @contextlib.contextmanager
    def scope():
        sessao = _SessaoFalsa()
        registro.sessoes.append(sessao)
        yield sessao

    def upsert(session, table, rows, *, index_elements, update_columns=None, **_):
        registro.upserts.append((table.name, [dict(r) for r in rows], update_columns))
        return len(rows)

    monkeypatch.setattr(juiza, "session_scope", scope)
    monkeypatch.setattr(juiza, "bulk_upsert", upsert)
    monkeypatch.setattr(juiza, "garantir_prompt", lambda session, prompt: 7)
    monkeypatch.setattr(juiza_custo, "gasto_usd", lambda finalidade, desde=None: 0.0)
    return registro


@pytest.fixture
def pendentes_falsos(monkeypatch):
    """Devolve os itens so na 1a chamada. Devolve a lista de kwargs recebidos por pendentes()."""
    def definir(itens: list[dict]) -> list[dict]:
        chamadas: list[dict] = []

        def falso(versao, limite=None, **kw):
            chamadas.append({"limite": limite, **kw})
            return list(itens) if len(chamadas) == 1 else []

        monkeypatch.setattr(juiza, "pendentes", falso)
        return chamadas

    return definir


@pytest.fixture
def settings(monkeypatch):
    # Configuracao fixa: nao depende do .env de quem roda os testes.
    s = get_settings()
    monkeypatch.setattr(s, "juiza_provedor", "gemini")
    monkeypatch.setattr(s, "juiza_modelo", "")
    monkeypatch.setattr(s, "gemini_api_key", "chave-falsa")
    monkeypatch.setattr(s, "openai_api_key", "")
    monkeypatch.setattr(s, "jev_enabled", False)
    monkeypatch.setattr(s, "jev_api_key", "chave-falsa")
    monkeypatch.setattr(s, "jev_modelo", "jev-1.13.0")
    monkeypatch.setattr(s, "jev_trecho", 1500)
    monkeypatch.setattr(s, "jev_lote", 50)
    monkeypatch.setattr(s, "juiza_jev_modo", "entrega")
    monkeypatch.setattr(s, "juiza_jev_limiar_entrega", 0.9)
    monkeypatch.setattr(s, "juiza_lote", 50)
    monkeypatch.setattr(s, "juiza_confianca_minima", 0.5)
    monkeypatch.setattr(s, "juiza_confianca_triagem", 0.9)
    return s


def _staging(gravacoes) -> list[dict]:
    return [r for nome, rows, _ in gravacoes.upserts
            if nome == "v1-staging_noticias_julgamento" for r in rows]


def _runs(gravacoes) -> list[RawLlmJuizaRun]:
    return [o for s in gravacoes.sessoes for o in s.objetos
            if isinstance(o, RawLlmJuizaRun)]


def _tentativas(gravacoes) -> list[dict]:
    return [r for nome, rows, _ in gravacoes.upserts
            if nome == "v1-staging_juiza_tentativas" for r in rows]


# --------------------------------------------------------------------------------------
# Testes puros
# --------------------------------------------------------------------------------------

def test_regra_resolve_ipe_youtube_e_panorama_sem_gastar_llm(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([
        _item(1, origem="cvm_ipe", dominio="cvm.gov.br", title="Vale divulga fato relevante"),
        _item(2, dominio="youtube.com", title="Vale: o que pensar agora"),
        _item(3, title="Ibovespa sobe com Petrobras, Itaú e Bradesco"),
        _item(4),
    ])
    cliente = ClienteFalso([_julgamento(4, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente)

    assert res.pendentes == 4
    assert res.regra == 3
    assert cliente.chamadas == 1, "so o artigo normal deve chegar ao Gemini"
    assert res.llm == 1
    regra = [r for r in _staging(gravacoes) if r["modelo"] == "regra"]
    assert sorted(r["article_id"] for r in regra) == [1, 2, 3]
    assert all(r["run_id"] is None for r in regra)
    assert all(r["prompt_versao"] == juiza.VERSAO for r in regra)


def test_tickers_adicionais_fora_do_universo_sao_descartados(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([_item(4)])
    cliente = ClienteFalso([
        _julgamento(4, "VALE3", tickers_adicionais=["PETR4", "XXXX1", "PETR4"]),
        _julgamento(99, "ZZZZ9"),  # par que nao foi pedido: descartado
    ])

    juiza.julgar_pendentes(cliente=cliente)

    linhas = _staging(gravacoes)
    assert [(r["article_id"], r["ticker"]) for r in linhas] == [(4, "VALE3")]
    assert linhas[0]["tickers_adicionais"] == ["PETR4"]
    assert linhas[0]["modelo"] == settings.juiza_modelo_efetivo
    assert linhas[0]["run_id"] is not None


def test_erro_da_api_grava_run_e_deixa_o_item_pendente(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([_item(4)])
    cliente = ClienteFalso(erro=RuntimeError("503 indisponivel"))

    res = juiza.julgar_pendentes(cliente=cliente)

    runs = _runs(gravacoes)
    assert [r.status for r in runs] == ["erro_api"]
    assert "503" in runs[0].erro
    assert _staging(gravacoes) == []
    assert res.erros == 1 and res.llm == 0


def test_json_invalido_vira_erro_json(gravacoes, pendentes_falsos, settings) -> None:
    pendentes_falsos([_item(4)])

    juiza.julgar_pendentes(cliente=ClienteFalso(texto="isto nao e json"))

    assert [r.status for r in _runs(gravacoes)] == ["erro_json"]
    assert _staging(gravacoes) == []


def test_enum_invalido_vira_erro_validacao(gravacoes, pendentes_falsos, settings) -> None:
    pendentes_falsos([_item(4)])
    cliente = ClienteFalso([_julgamento(4, "VALE3", papel="inventado")])

    juiza.julgar_pendentes(cliente=cliente)

    assert [r.status for r in _runs(gravacoes)] == ["erro_validacao"]
    assert _staging(gravacoes) == []


def test_deve_parar_interrompe_antes_do_primeiro_lote(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([_item(4)])
    cliente = ClienteFalso([_julgamento(4, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, observador=ObservadorFalso(parar=True))

    assert res.interrompido == 1
    assert cliente.chamadas == 0


def test_jev_desligado_nunca_e_chamado(gravacoes, pendentes_falsos, settings) -> None:
    pendentes_falsos([_item(4)])
    jev = JevFalso([_julgamento(4, "VALE3")])

    res = juiza.julgar_pendentes(cliente=ClienteFalso([_julgamento(4, "VALE3")]), jev=jev)

    assert jev.chamadas == 0
    assert res.jev == 0


def test_jev_triagem_aceita_so_descarte_com_confianca_alta(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "juiza_jev_modo", "triagem")
    pendentes_falsos([_item(4), _item(5), _item(6)])
    jev = JevFalso([
        _julgamento(4, "VALE3", papel="mencao", confianca=0.95),  # descarte com confianca: aceito
        _julgamento(5, "VALE3", confianca=0.99),                  # protagonista: vai ao Gemini
        _julgamento(6, "VALE3", papel="mencao", confianca=0.8),   # descarte, confianca baixa
    ])
    cliente = ClienteFalso([_julgamento(5, "VALE3"), _julgamento(6, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert res.jev == 1 and res.llm == 2
    assert cliente.chamadas == 1
    linhas = {r["article_id"]: r for r in _staging(gravacoes)}
    assert set(linhas) == {4, 5, 6}
    assert linhas[4]["modelo"] == rotulo_modelo_jev(settings.jev_modelo, settings.jev_trecho)
    assert linhas[4]["run_id"] is not None
    assert linhas[5]["modelo"] == settings.juiza_modelo_efetivo


def test_jev_decide_aceita_protagonista_com_confianca_minima(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "juiza_jev_modo", "decide")
    pendentes_falsos([_item(4), _item(5)])
    jev = JevFalso([
        _julgamento(4, "VALE3", confianca=0.6),  # protagonista com 0.6: decide, sem Gemini
        _julgamento(5, "VALE3", confianca=0.4),  # abaixo do minimo: volta para o Gemini
    ])
    cliente = ClienteFalso([_julgamento(5, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert res.jev == 1 and res.llm == 1
    assert cliente.chamadas == 1
    assert {r["article_id"]: r["modelo"] for r in _staging(gravacoes)}[4] == (
        rotulo_modelo_jev(settings.jev_modelo, settings.jev_trecho))


def test_lote_do_jev_com_excecao_fica_pendente_e_nao_chama_a_llm(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    pendentes_falsos([_item(4), _item(5)])
    jev = JevFalso(erro=RuntimeError("Jev: falha de rede"))
    cliente = ClienteFalso([_julgamento(4, "VALE3"), _julgamento(5, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert res.jev == 0 and res.llm == 0 and res.erros == 1
    assert res.parada is None
    assert cliente.chamadas == 0
    assert _staging(gravacoes) == []
    assert _tentativas(gravacoes) == []
    jev_runs = [r for r in _runs(gravacoes) if r.modelo.startswith(settings.jev_modelo)]
    assert [(r.status, r.custo_usd) for r in jev_runs] == [("erro_api", 0.0)]
    assert "falha de rede" in jev_runs[0].erro


def test_dois_lotes_do_jev_com_erro_seguido_param_em_erro_jev(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_lote", 1)
    pendentes_falsos([_item(4), _item(5), _item(6)])
    jev = JevFalso(erro=RuntimeError("503"))
    cliente = ClienteFalso([_julgamento(i, "VALE3") for i in (4, 5, 6)])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert jev.chamadas == 2
    assert res.parada == "erro_jev"
    assert res.erros == 2
    assert cliente.chamadas == 0


def test_erro_permanente_do_jev_para_no_primeiro_lote(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_lote", 1)
    pendentes_falsos([_item(4), _item(5)])
    jev = JevFalso(erro=JevFalhaLote("Jev: todos os 1 itens falharam (HTTP 401)",
                                     permanente=True, codigos={401}))
    cliente = ClienteFalso([_julgamento(4, "VALE3"), _julgamento(5, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert jev.chamadas == 1
    assert res.parada == "erro_jev"
    assert cliente.chamadas == 0


def test_item_em_erros_do_jev_segue_a_llm_sem_tentativa(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "juiza_jev_modo", "triagem")
    pendentes_falsos([_item(4), _item(5)])
    jev = JevFalso([_julgamento(4, "VALE3", papel="mencao", confianca=0.95)],
                   erros=[{"id": "5", "ticker": "VALE3", "erro": "HTTP 500"}])
    cliente = ClienteFalso([_julgamento(5, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert res.jev == 1 and res.llm == 1
    assert cliente.chamadas == 1
    assert _tentativas(gravacoes) == []
    jev_runs = [r for r in _runs(gravacoes) if r.modelo.startswith(settings.jev_modelo)]
    assert [r.status for r in jev_runs] == ["parcial"]
    assert "HTTP 500" in jev_runs[0].resposta_bruta


def test_custo_do_jev_soma_ao_da_llm_e_fica_na_run(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "juiza_jev_modo", "triagem")
    pendentes_falsos([_item(4), _item(5)])
    jev = JevFalso([_julgamento(4, "VALE3", papel="mencao", confianca=0.95)],
                   tokens_entrada=2_000_000)
    cliente = ClienteFalso([_julgamento(5, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    custo_jev = juiza_custo.custo_jev(2_000_000, settings)
    custo_gem = juiza_custo.custo_llm(100, 20, s=settings)
    assert res.custo_usd == pytest.approx(custo_jev + custo_gem)
    jev_run = [r for r in _runs(gravacoes) if r.modelo.startswith(settings.jev_modelo)][0]
    assert jev_run.custo_usd == pytest.approx(custo_jev)
    assert jev_run.finalidade == "producao"


def test_teto_de_itens_conta_o_par_que_passa_pelo_jev_e_pelo_gemini_uma_vez(
    gravacoes, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "juiza_pagina", 2)
    pub = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    paginas = [
        [{**_item(4), "published_at": pub}, {**_item(5), "published_at": pub}],
        [{**_item(6), "published_at": pub}, {**_item(7), "published_at": pub}],
    ]
    chamadas: list[dict] = []

    def falso(versao, limite=None, **kw):
        chamadas.append(kw)
        return paginas[len(chamadas) - 1] if len(chamadas) <= len(paginas) else []

    monkeypatch.setattr(juiza, "pendentes", falso)
    jev = JevFalso([])  # nada decidido: todo par segue ao Gemini
    cliente = ClienteFalso([_julgamento(i, "VALE3") for i in (4, 5, 6, 7)])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev, limite_itens=3, lote=10)

    # 3 pares no total: 2 na 1a pagina e 1 na 2a. O par do Jev que vai ao Gemini nao conta 2x.
    assert jev.chamadas == 2
    assert res.llm == 3
    assert res.parada == "teto_itens"
    assert len(chamadas) == 2


def test_julgar_pendentes_fecha_o_jev_que_criou(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    from tcc_scraper.llm import jev_client

    criados: list[JevFalso] = []

    class JevCriado(JevFalso):
        def __init__(self, api_key, modelo, timeout_s, concorrencia, trecho_max):
            super().__init__([])
            criados.append(self)

    monkeypatch.setattr(jev_client, "JevClient", JevCriado)
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_api_key", "chave-falsa")
    pendentes_falsos([_item(4)])

    juiza.julgar_pendentes(cliente=ClienteFalso([_julgamento(4, "VALE3")]))

    assert len(criados) == 1 and criados[0].fechado


def test_contar_pendentes_devolve_pendentes_esgotados_e_julgados(monkeypatch) -> None:
    class _Sessao:
        def execute(self, consulta):
            linha = SimpleNamespace(pendentes=3, esgotados=1, julgados=7)
            return SimpleNamespace(one=lambda: linha)

    @contextlib.contextmanager
    def scope():
        yield _Sessao()

    monkeypatch.setattr(juiza, "session_scope", scope)

    assert juiza.contar_pendentes(3) == {"pendentes": 3, "esgotados": 1, "julgados": 7}


def test_consulta_contagem_compila_em_mysql_com_os_joins_de_pendentes() -> None:
    from sqlalchemy.dialects import mysql

    sql = str(juiza._consulta_contagem(3).compile(dialect=mysql.dialect()))

    assert sql.count("LEFT OUTER JOIN") == 3
    assert "`v1-staging_juiza_tentativas`" in sql
    assert "`v1-staging_noticias_julgamento`" in sql
    assert "count(CASE WHEN" in sql


def test_sem_chave_gemini_so_a_regra_roda(gravacoes, pendentes_falsos, settings, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    pendentes_falsos([_item(1, origem="cvm_ipe", dominio="cvm.gov.br"), _item(4)])

    res = juiza.julgar_pendentes()

    assert res.regra == 1
    assert res.llm == 0
    assert [r["article_id"] for r in _staging(gravacoes)] == [1]


def test_lotes_respeitam_o_tamanho_e_avisam_o_observador(
    gravacoes, pendentes_falsos, settings
) -> None:
    itens = [_item(i) for i in range(1, 6)]
    pendentes_falsos(itens)
    cliente = ClienteFalso([_julgamento(i, "VALE3") for i in range(1, 6)])
    observador = ObservadorFalso()

    res = juiza.julgar_pendentes(cliente=cliente, observador=observador, lote=2)

    assert cliente.chamadas == 3
    assert observador.planos == [3]
    assert [nome for nome, _ in observador.unidades] == ["lote|1", "lote|2", "lote|3"]
    assert res.llm == 5
    assert len(_runs(gravacoes)) == 3


def test_parcial_aceita_os_validos_e_tenta_o_invalido(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([_item(4), _item(5), _item(6)])
    cliente = ClienteFalso([
        _julgamento(4, "VALE3"), _julgamento(5, "VALE3"),
        _julgamento(6, "VALE3", papel="inventado"),
    ])

    res = juiza.julgar_pendentes(cliente=cliente)

    assert [r.status for r in _runs(gravacoes)] == ["parcial"]
    assert sorted(r["article_id"] for r in _staging(gravacoes)) == [4, 5]
    assert [(t["article_id"], t["tentativas"], t["ultimo_status"])
            for t in _tentativas(gravacoes)] == [(6, 1, "parcial")]
    assert res.llm == 2 and res.invalidos == 1 and res.erros == 1


def test_erro_da_api_nao_gera_tentativa(gravacoes, pendentes_falsos, settings) -> None:
    pendentes_falsos([_item(4)])

    juiza.julgar_pendentes(cliente=ClienteFalso(erro=RuntimeError("503 indisponivel")))

    assert _tentativas(gravacoes) == []


def test_erro_json_registra_tentativa_para_todos_os_pedidos(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([_item(4), _item(5)])

    juiza.julgar_pendentes(cliente=ClienteFalso(texto="isto nao e json"))

    assert [(t["article_id"], t["tentativas"], t["ultimo_status"])
            for t in _tentativas(gravacoes)] == [(4, 1, "erro_json"), (5, 1, "erro_json")]


def test_tentativa_soma_a_partir_do_valor_que_veio_do_pendentes(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([{**_item(4), "tentativas": 2}])

    juiza.julgar_pendentes(cliente=ClienteFalso(texto="isto nao e json"))

    assert _tentativas(gravacoes)[0]["tentativas"] == 3


def test_teto_de_itens_nao_conta_a_regra(gravacoes, pendentes_falsos, settings) -> None:
    pendentes_falsos([_item(1, origem="cvm_ipe", dominio="cvm.gov.br"), _item(4), _item(5),
                      _item(6)])
    cliente = ClienteFalso([_julgamento(4, "VALE3"), _julgamento(5, "VALE3"),
                            _julgamento(6, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, limite_itens=2)

    assert res.regra == 1
    assert res.llm == 2
    assert res.parada == "teto_itens"
    assert sorted(r["article_id"] for r in _staging(gravacoes)
                  if r["modelo"] != "regra") == [4, 5]


def test_teto_em_usd_para_antes_do_lote_que_estouraria(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    custo = juiza_custo.custo_llm(100, 20, s=settings)
    monkeypatch.setattr(settings, "juiza_orcamento_mensal_usd", custo * 1.5)
    pendentes_falsos([_item(4), _item(5), _item(6)])
    cliente = ClienteFalso([_julgamento(i, "VALE3") for i in (4, 5, 6)])

    res = juiza.julgar_pendentes(cliente=cliente, lote=1)

    assert cliente.chamadas == 1
    assert res.parada == "orcamento_usd"
    assert res.custo_usd == pytest.approx(custo)
    runs = _runs(gravacoes)
    assert len(runs) == 1 and runs[0].custo_usd == pytest.approx(custo)


def test_duas_falhas_de_api_seguidas_param_a_execucao(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([_item(4), _item(5), _item(6)])
    cliente = ClienteFalso(erro=RuntimeError("503"))

    res = juiza.julgar_pendentes(cliente=cliente, lote=1)

    assert cliente.chamadas == 2
    assert res.parada == "erro_api"


def test_backlog_pagina_em_ordem_crescente_e_grava_finalidade_backlog(
    gravacoes, pendentes_falsos, settings
) -> None:
    chamadas = pendentes_falsos([_item(4)])
    cliente = ClienteFalso([_julgamento(4, "VALE3")])

    juiza.julgar_pendentes(cliente=cliente, modo="backlog", anos=[2025])

    assert chamadas[0]["ordem"] == "asc"
    assert chamadas[0]["anos"] == [2025]
    assert [r.finalidade for r in _runs(gravacoes)] == ["backlog"]


def test_paginas_seguem_o_cursor_keyset(gravacoes, settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "juiza_pagina", 2)
    pub = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    paginas = [
        [{**_item(4), "published_at": pub}, {**_item(5), "published_at": pub}],
        [{**_item(6), "published_at": pub}],
    ]
    chamadas: list[dict] = []

    def falso(versao, limite=None, **kw):
        chamadas.append(kw)
        return paginas[len(chamadas) - 1] if len(chamadas) <= len(paginas) else []

    monkeypatch.setattr(juiza, "pendentes", falso)
    cliente = ClienteFalso([_julgamento(i, "VALE3") for i in (4, 5, 6)])

    res = juiza.julgar_pendentes(cliente=cliente, lote=10)

    assert len(chamadas) == 2
    assert chamadas[0]["apos"] is None
    assert chamadas[1]["apos"] == (pub, 5, "VALE3")
    assert res.llm == 3 and cliente.chamadas == 2


def test_modo_entrega_grava_par_que_entra_sem_chamar_a_llm(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    pendentes_falsos([_item(4)])
    jev = JevFalso([_jev_par(4, p_entra=0.95, p_nao_entra=0.03)])
    cliente = ClienteFalso()

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert cliente.chamadas == 0
    assert res.jev == 1 and res.llm == 0
    linha = _staging(gravacoes)[0]
    assert linha["modelo"] == rotulo_modelo_jev(settings.jev_modelo, settings.jev_trecho)
    assert linha["confianca"] == 0.95


def test_modo_entrega_grava_par_que_nao_entra(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    pendentes_falsos([_item(4)])
    jev = JevFalso([_jev_par(4, papel="mencao", p_entra=0.03, p_nao_entra=0.95)])
    cliente = ClienteFalso()

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert cliente.chamadas == 0
    assert res.jev == 1 and res.llm == 0
    assert _staging(gravacoes)[0]["confianca"] == 0.95


def test_modo_entrega_par_indeciso_vai_a_llm(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    pendentes_falsos([_item(4)])
    jev = JevFalso([_jev_par(4, p_entra=0.6, p_nao_entra=0.4)])
    cliente = ClienteFalso([_julgamento(4, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert cliente.chamadas == 1
    assert res.jev == 0 and res.llm == 1
    assert _staging(gravacoes)[0]["modelo"] == settings.juiza_modelo_efetivo


def test_modo_entrega_mencao_com_p_entra_alta_vai_a_llm(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    pendentes_falsos([_item(4)])
    jev = JevFalso([_jev_par(4, papel="mencao", p_entra=0.95, p_nao_entra=0.04)])
    cliente = ClienteFalso([_julgamento(4, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert cliente.chamadas == 1
    assert res.jev == 0 and res.llm == 1


def test_jev_ligado_sem_chave_bloqueia_a_llm_e_a_regra_roda(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "jev_api_key", "")
    pendentes_falsos([_item(1, origem="cvm_ipe", dominio="cvm.gov.br"), _item(4)])
    cliente = ClienteFalso([_julgamento(4, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente)

    assert cliente.chamadas == 0
    assert res.parada == "jev_sem_chave"
    assert "jev_sem_chave" in res.avisos
    assert [r["article_id"] for r in _staging(gravacoes)] == [1]


def test_sem_credito_para_a_execucao_sem_tentativa(
    gravacoes, pendentes_falsos, settings
) -> None:
    pendentes_falsos([_item(4), _item(5)])
    cliente = ClienteFalso(erro=SemCreditoLLM("OpenAI sem credito"))

    res = juiza.julgar_pendentes(cliente=cliente, lote=1)

    assert cliente.chamadas == 1
    assert res.parada == "sem_credito"
    assert _tentativas(gravacoes) == []
    assert [r.status for r in _runs(gravacoes)] == ["erro_api"]


class _ErroComCodigo(RuntimeError):
    def __init__(self) -> None:
        super().__init__("402 Payment Required")
        self.code = 402


def test_codigo_402_tambem_para_a_execucao(gravacoes, pendentes_falsos, settings) -> None:
    pendentes_falsos([_item(4), _item(5)])
    cliente = ClienteFalso(erro=_ErroComCodigo())

    res = juiza.julgar_pendentes(cliente=cliente, lote=1)

    assert cliente.chamadas == 1
    assert res.parada == "sem_credito"
    assert _tentativas(gravacoes) == []


def test_provedor_openai_com_modelo_gemini_recusa_a_execucao(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "juiza_provedor", "openai")
    monkeypatch.setattr(settings, "juiza_modelo", "gemini-x")
    cliente = ClienteFalso()

    with pytest.raises(ValueError, match="JUIZA_MODELO"):
        juiza.julgar_pendentes(cliente=cliente)
    assert cliente.chamadas == 0


def test_jev_recebe_o_trecho_inteiro_e_a_llm_so_300_caracteres(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    trecho = "A" * 300 + "ZQX" + "B" * 700
    chamadas = pendentes_falsos([{**_item(4), "trecho": trecho}])
    jev = JevFalso([])  # nada decidido: o par segue para a LLM
    cliente = ClienteFalso([_julgamento(4, "VALE3")])

    juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert chamadas[0]["trecho"] == 1500
    assert jev.recebidos[0][0]["trecho"] == trecho
    assert "A" * 300 in cliente.prompts[0]
    assert "ZQX" not in cliente.prompts[0]


class _ResultadoFalso:
    def __init__(self, linhas) -> None:
        self._linhas = list(linhas)

    def mappings(self):
        return self

    def all(self):
        return list(self._linhas)


class _SessaoConsultaFalsa:
    """A 1a execute devolve as linhas de pendentes; as seguintes, os corpos (tuplas)."""

    def __init__(self, linhas, corpos) -> None:
        self.linhas = linhas
        self.corpos = corpos
        self.execucoes = 0

    def execute(self, *a, **k):
        self.execucoes += 1
        return _ResultadoFalso(self.linhas if self.execucoes == 1 else self.corpos)


@pytest.fixture
def consulta_pendentes(monkeypatch):
    """Troca session_scope por uma sessao que responde as consultas de pendentes()."""
    def montar(linhas: list[dict], corpos: list[tuple] = ()) -> _SessaoConsultaFalsa:
        sessao = _SessaoConsultaFalsa(linhas, list(corpos))

        @contextlib.contextmanager
        def scope():
            yield sessao

        monkeypatch.setattr(juiza, "session_scope", scope)
        return sessao

    return montar


# Mencao do ticker so no fim: os 300 primeiros caracteres (o substr do SQL) nao a contem.
_CORPO_FIM = ("Mercado sem novidades relevantes no setor de energia e bancos. " * 30
              + "A VALE3 subiu 3% no pregao de hoje.")


def _linha_pendente(id_: int, origem: str, trecho_sql: str, ticker: str = "VALE3") -> dict:
    return {"id": id_, "ticker": ticker, "title": "Mercado fecha em alta",
            "source_domain": "x.com.br", "origem": origem, "lang": "pt",
            "published_at": dt.datetime(2026, 10, 1, tzinfo=dt.UTC), "tentativas": 0,
            "trecho": trecho_sql}


def test_pendentes_wordpress_traz_a_mencao_que_esta_no_fim_do_corpo(consulta_pendentes) -> None:
    truncado = _CORPO_FIM[:300]
    assert "VALE3" not in truncado
    sessao = consulta_pendentes([_linha_pendente(4, "wordpress", truncado)], [(4, _CORPO_FIM)])

    itens = juiza.pendentes(7, trecho=1500)

    assert "VALE3" in itens[0]["trecho"]
    assert len(itens[0]["trecho"]) <= 1500
    assert sessao.execucoes == 2


def test_pendentes_google_news_nao_muda_o_trecho(consulta_pendentes) -> None:
    truncado = _CORPO_FIM[:300]
    sessao = consulta_pendentes([_linha_pendente(4, "google_news", truncado)], [(4, _CORPO_FIM)])

    itens = juiza.pendentes(7, trecho=1500)

    assert itens[0]["trecho"] == truncado
    assert sessao.execucoes == 1  # nenhuma query extra para o corpo


def test_pendentes_wordpress_sem_mencao_valida_mantem_o_trecho_do_sql(consulta_pendentes) -> None:
    corpo = "Texto de economia geral, sem empresa da bolsa citada. " * 20
    truncado = corpo[:300]
    consulta_pendentes([_linha_pendente(4, "wordpress", truncado)], [(4, corpo)])

    itens = juiza.pendentes(7, trecho=1500)

    assert itens[0]["trecho"] == truncado


def test_pendentes_wordpress_busca_os_corpos_em_uma_query_so(consulta_pendentes) -> None:
    linhas = [_linha_pendente(4, "wordpress", ""), _linha_pendente(5, "wordpress", ""),
              _linha_pendente(6, "google_news", "")]
    sessao = consulta_pendentes(linhas, [(4, _CORPO_FIM), (5, _CORPO_FIM)])

    juiza.pendentes(7, trecho=1500)

    assert sessao.execucoes == 2  # consulta principal + um lote para os dois wordpress


def test_regras_do_casador_sao_calculadas_uma_vez() -> None:
    assert juiza._regras() is juiza._regras()


def test_custo_usa_o_cache_da_llm_e_res_acumula_tokens_cache(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "juiza_provedor", "openai")
    for campo in ("juiza_preco_entrada_usd_mtok", "juiza_preco_saida_usd_mtok",
                  "juiza_preco_cache_usd_mtok"):
        monkeypatch.setattr(settings, campo, None)
    pendentes_falsos([_item(4)])
    cliente = ClienteFalso([_julgamento(4, "VALE3")], tokens_cache=60)

    res = juiza.julgar_pendentes(cliente=cliente)

    esperado = juiza_custo.custo_llm(100, 20, tokens_cache=60, s=settings)
    assert res.custo_usd == pytest.approx(esperado)
    assert _runs(gravacoes)[0].custo_usd == pytest.approx(esperado)
    assert res.tokens_cache == 60
    assert esperado < juiza_custo.custo_llm(100, 20, s=settings)


def test_avisos_config_combinacoes() -> None:
    def cfg(**campos):
        base = {"jev_enabled": False, "jev_api_key": "", "juiza_provedor": "openai",
                "juiza_chave_llm": "chave", "juiza_modelo_efetivo": "gpt-5.6-luna"}
        return SimpleNamespace(**{**base, **campos})

    assert juiza.avisos_config(cfg()) == ["jev_desligado"]
    assert juiza.avisos_config(cfg(jev_enabled=True)) == ["jev_sem_chave"]
    assert juiza.avisos_config(cfg(jev_enabled=True, jev_api_key="j")) == []
    assert juiza.avisos_config(cfg(juiza_chave_llm="")) == [
        "jev_desligado", "openai_sem_chave"]
    assert juiza.avisos_config(cfg(juiza_modelo_efetivo="gemini-3.1-flash-lite")) == [
        "jev_desligado", "modelo_incompativel"]
    assert juiza.avisos_config(cfg(juiza_provedor="gemini",
                                   juiza_modelo_efetivo="gpt-5.6-luna")) == [
        "jev_desligado", "modelo_incompativel"]


# --------------------------------------------------------------------------------------
# Com banco (MySQL do .env; opt-in via TCC_TESTES_COM_BANCO=1, ver conftest.py)
# --------------------------------------------------------------------------------------


def _artigo(n: int, title: str, dominio: str, ticker: str, origem: str = "google_news",
            corpo: str | None = None) -> dict:
    agora = dt.datetime.now(dt.UTC)
    u = f"https://news.google.com/rss/articles/JUIZ{n}"
    return {"url_hash": url_hash(u), "url": u, "google_id": f"JUIZ{n}", "title": title,
            "source_name": "Teste", "source_domain": dominio, "published_at": agora,
            "lang": "pt", "collected_at": agora, "origem": origem, "_ticker": ticker,
            "_corpo": corpo}


def test_banco_julga_so_o_que_falta_e_a_segunda_execucao_nao_reenvia(
    engine, monkeypatch
) -> None:
    artigos = [
        _artigo(1, "Vale divulga fato relevante sobre dividendos", "cvm.gov.br", "VALE3",
                origem="cvm_ipe"),                                       # regra: IPE
        _artigo(2, "Vale: o que pensar agora", "youtube.com", "VALE3"),  # regra: midia sem texto
        _artigo(3, "Ibovespa sobe com Petrobras, Itaú e Bradesco", "valor.com.br",
                "VALE3"),                                                # regra: panorama
        _artigo(4, "Vale anuncia novo diretor financeiro", "valor.com.br", "VALE3",
                corpo="A Vale anunciou nesta semana a contratacao de um novo diretor."),
        _artigo(5, "Petrobras anuncia reajuste de diesel", "valor.com.br", "PETR4"),
    ]
    run_max_antes = _max_run_id(engine)
    ids: set[int] = set()
    id_real: dict[int, int] = {}  # numero do artigo no teste -> id no banco
    with Session(engine) as s:
        for n, a in enumerate(artigos, start=1):
            ticker = a.pop("_ticker")
            corpo = a.pop("_corpo")
            art = Article(**a)
            s.add(art)
            s.flush()
            ids.add(art.id)
            id_real[n] = art.id
            s.add(ArticleTicker(article_id=art.id, ticker=ticker))
            if corpo:
                s.add(ArticleBody(article_id=art.id, body_text=corpo,
                                  extraction_method="rss_content_encoded",
                                  fetched_at=dt.datetime.now(dt.UTC)))
        s.commit()

    @contextlib.contextmanager
    def scope():
        sessao = Session(engine)
        try:
            yield sessao
            sessao.commit()
        except Exception:
            sessao.rollback()
            raise
        finally:
            sessao.close()

    original = juiza.pendentes
    monkeypatch.setattr(juiza, "session_scope", scope)
    # Restringe aos artigos deste teste: o banco de desenvolvimento pode ter pendentes reais.
    monkeypatch.setattr(juiza, "pendentes",
                        lambda versao, limite=None, **kw: [p for p in original(versao, None, **kw)
                                                           if p["id"] in ids])
    try:
        cliente = ClienteFalso([_julgamento(id_real[4], "VALE3"), _julgamento(id_real[5], "PETR4")])
        res1 = juiza.julgar_pendentes(cliente=cliente, lote=50)

        assert res1.regra == 3
        assert res1.llm == 2
        assert cliente.chamadas == 1

        with Session(engine) as s:
            linhas = s.execute(select(StagingNoticiaJulgamento).where(
                StagingNoticiaJulgamento.article_id.in_(ids))).scalars().all()
            modelos = sorted(r.modelo for r in linhas)
        assert modelos == sorted(["regra"] * 3 + [get_settings().juiza_modelo_efetivo] * 2)

        cliente2 = ClienteFalso([_julgamento(id_real[4], "VALE3"),
                                _julgamento(id_real[5], "PETR4")])
        res2 = juiza.julgar_pendentes(cliente=cliente2, lote=50)
        assert res2.pendentes == 0
        assert cliente2.chamadas == 0
    finally:
        with Session(engine) as s:
            s.execute(delete(StagingNoticiaJulgamento).where(
                StagingNoticiaJulgamento.article_id.in_(ids)))
            s.execute(delete(RawLlmJuizaRun).where(RawLlmJuizaRun.id > run_max_antes))
            s.execute(delete(ArticleTicker).where(ArticleTicker.article_id.in_(ids)))
            s.execute(delete(ArticleBody).where(ArticleBody.article_id.in_(ids)))
            s.execute(delete(Article).where(Article.id.in_(ids)))
            s.commit()



def _max_run_id(engine) -> int:
    with Session(engine) as s:
        return s.execute(select(RawLlmJuizaRun.id).order_by(RawLlmJuizaRun.id.desc())
                         .limit(1)).scalar() or 0
