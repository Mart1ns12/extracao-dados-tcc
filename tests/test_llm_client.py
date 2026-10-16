"""Testes do wrapper google-genai sem rede: so a logica de montagem e de retry.

O SDK nao e importado aqui (import lazy); GeminiClient e testado so pela validacao.
"""
import pytest

from tcc_scraper.llm.gemini_client import GeminiClient, _transitorio, montar_resposta


def test_montar_resposta_forma_nova():
    d = {
        "output_text": "{}",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "steps": [
            {"type": "google_search_call", "queries": ["a", "b"]},
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "annotations": [
                            {"type": "url_citation", "url": "https://x", "title": "X"},
                            {"type": "url_citation", "url": "https://x", "title": "dup"},
                        ],
                    }
                ],
            },
        ],
    }
    r = montar_resposta(d, "m", 7)
    assert r.tokens_entrada == 10
    assert r.tokens_saida == 5
    assert r.tokens_pensamento is None
    assert r.buscas == ["a", "b"]
    assert len(r.citacoes) == 1
    assert r.citacoes[0]["url"] == "https://x"
    assert r.texto == "{}"
    assert r.latencia_ms == 7


def test_montar_resposta_vazia():
    r = montar_resposta({}, "m", 1)
    assert r.texto == ""
    assert r.buscas == []
    assert r.citacoes == []
    assert r.tokens_entrada is None
    assert r.tokens_saida is None
    assert r.tokens_pensamento is None


def test_montar_resposta_forma_antiga():
    d = {
        "text": "{}",
        "usage_metadata": {"prompt_token_count": 3, "candidates_token_count": 4},
        "candidates": [
            {
                "grounding_metadata": {
                    "web_search_queries": ["q"],
                    "grounding_chunks": [{"web": {"uri": "https://y", "title": "Y"}}],
                }
            }
        ],
    }
    r = montar_resposta(d, "m", 1)
    assert r.tokens_entrada == 3
    assert r.tokens_saida == 4
    assert r.buscas == ["q"]
    assert len(r.citacoes) == 1
    assert r.citacoes[0]["url"] == "https://y"


def test_transitorio_por_codigo():
    assert _transitorio(type("E", (Exception,), {"code": 429})()) is True
    assert _transitorio(type("E", (Exception,), {"code": 400})()) is False


def test_transitorio_timeout():
    assert _transitorio(TimeoutError()) is True


def test_client_sem_chave_levanta_antes_do_import():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiClient("", "m")
