"""Orquestracao da juiza: regra -> Jev (opcional) -> Gemini, com persistencia e retomada.

Os testes puros trocam pendentes, sessao e bulk_upsert por versoes falsas: nada toca banco
nem rede. O teste com banco (MySQL do .env, opt-in via TCC_TESTES_COM_BANCO=1) usa a
fixture `engine` de conftest.py e filtra os pendentes para os artigos que ele cria.
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tcc_scraper.config import get_settings
from tcc_scraper.db.models import Article, ArticleBody, ArticleTicker, url_hash
from tcc_scraper.db.models_fontes import RawLlmJuizaRun, StagingNoticiaJulgamento
from tcc_scraper.llm import juiza

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


class ClienteFalso:
    """Imita GeminiClient.classificar. Responde com `respostas` (lista de julgamentos)."""

    def __init__(self, respostas: list[dict] | None = None, *, erro: Exception | None = None,
                 texto: str | None = None) -> None:
        self.respostas = respostas or []
        self.erro = erro
        self.texto = texto
        self.chamadas = 0

    def classificar(self, prompt: str, schema: dict):
        self.chamadas += 1
        if self.erro is not None:
            raise self.erro
        texto = self.texto if self.texto is not None else json.dumps(
            {"itens": self.respostas}, ensure_ascii=False)
        return SimpleNamespace(texto=texto, modelo="modelo-falso", latencia_ms=5,
                               tokens_entrada=100, tokens_saida=20)


class JevFalso:
    def __init__(self, respostas: list[dict]) -> None:
        self.respostas = respostas
        self.chamadas = 0

    def classificar_lote(self, itens: list[dict]) -> list[dict]:
        self.chamadas += 1
        return self.respostas


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
    """Substitui sessao, bulk_upsert e garantir_prompt. Devolve o que seria gravado."""
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
    return registro


@pytest.fixture
def pendentes_falsos(monkeypatch):
    def definir(itens: list[dict]) -> None:
        monkeypatch.setattr(juiza, "pendentes", lambda versao, limite=None: list(itens))

    return definir


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "gemini_api_key", "chave-falsa")
    monkeypatch.setattr(s, "jev_enabled", False)
    monkeypatch.setattr(s, "juiza_lote", 50)
    monkeypatch.setattr(s, "juiza_confianca_minima", 0.5)
    return s


def _staging(gravacoes) -> list[dict]:
    return [r for nome, rows, _ in gravacoes.upserts
            if nome == "v1-staging_noticias_julgamento" for r in rows]


def _runs(gravacoes) -> list[RawLlmJuizaRun]:
    return [o for s in gravacoes.sessoes for o in s.objetos
            if isinstance(o, RawLlmJuizaRun)]


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
    assert res.gemini == 1
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
    assert linhas[0]["modelo"] == settings.juiza_modelo
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
    assert res.erros == 1 and res.gemini == 0


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


def test_jev_ligado_aceita_so_com_confianca_suficiente(
    gravacoes, pendentes_falsos, settings, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "jev_enabled", True)
    pendentes_falsos([_item(4), _item(5)])
    jev = JevFalso([
        _julgamento(4, "VALE3", confianca=0.9),
        _julgamento(5, "VALE3", confianca=0.2),  # abaixo do minimo: volta para o Gemini
    ])
    cliente = ClienteFalso([_julgamento(5, "VALE3")])

    res = juiza.julgar_pendentes(cliente=cliente, jev=jev)

    assert res.jev == 1
    assert cliente.chamadas == 1
    linhas = _staging(gravacoes)
    jev_linha = [r for r in linhas if r["article_id"] == 4]
    assert jev_linha and jev_linha[0]["modelo"] == settings.jev_modelo
    assert jev_linha[0]["run_id"] is None


def test_sem_chave_gemini_so_a_regra_roda(gravacoes, pendentes_falsos, settings, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    pendentes_falsos([_item(1, origem="cvm_ipe", dominio="cvm.gov.br"), _item(4)])

    res = juiza.julgar_pendentes()

    assert res.regra == 1
    assert res.gemini == 0
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
    assert res.gemini == 5
    assert len(_runs(gravacoes)) == 3


def test_avaliar_conjunto_ouro_calcula_precisao_e_revocacao(
    tmp_path, monkeypatch, settings
) -> None:
    caminho = tmp_path / "gold.csv"
    with caminho.open("w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(
            f, fieldnames=["article_id", "ticker", "titulo", "papel", "tipo_conteudo"])
        escritor.writeheader()
        escritor.writerow({"article_id": "1", "ticker": "VALE3",
                           "titulo": "Ibovespa sobe com Petrobras, Itaú e Bradesco",
                           "papel": "mencao", "tipo_conteudo": "resumo_mercado"})
        escritor.writerow({"article_id": "2", "ticker": "VALE3",
                           "titulo": "Vale anuncia novo diretor",
                           "papel": "protagonista", "tipo_conteudo": "reportagem"})
        escritor.writerow({"article_id": "3", "ticker": "VALE3",
                           "titulo": "Vale anuncia novo diretor",
                           "papel": "errada", "tipo_conteudo": "reportagem"})
    cliente = ClienteFalso([_julgamento("2", "VALE3"), _julgamento("3", "VALE3")])
    from tcc_scraper.llm import gemini_client
    monkeypatch.setattr(gemini_client, "GeminiClient", lambda *a, **k: cliente)

    resultado = juiza.avaliar_conjunto_ouro(caminho)

    assert resultado["n"] == 3
    assert resultado["n_regra"] == 1
    assert resultado["acuracia"] == pytest.approx(2 / 3)
    classes = resultado["por_classe"]
    assert classes["protagonista"]["precisao"] == pytest.approx(0.5)
    assert classes["protagonista"]["revocacao"] == pytest.approx(1.0)
    assert classes["mencao"]["precisao"] == pytest.approx(1.0)
    assert classes["errada"]["precisao"] is None
    assert classes["errada"]["revocacao"] == pytest.approx(0.0)


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
                        lambda versao, limite=None: [p for p in original(versao, None)
                                                     if p["id"] in ids])
    try:
        cliente = ClienteFalso([_julgamento(id_real[4], "VALE3"), _julgamento(id_real[5], "PETR4")])
        res1 = juiza.julgar_pendentes(cliente=cliente, lote=50)

        assert res1.regra == 3
        assert res1.gemini == 2
        assert cliente.chamadas == 1

        with Session(engine) as s:
            linhas = s.execute(select(StagingNoticiaJulgamento).where(
                StagingNoticiaJulgamento.article_id.in_(ids))).scalars().all()
            modelos = sorted(r.modelo for r in linhas)
        assert modelos == sorted(["regra"] * 3 + [get_settings().juiza_modelo] * 2)

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
