"""Adaptador do Jev (TypeSafe AI): julga pares (artigo, ticker) pela API systemone.

Uma requisicao por par (a API nao aceita lote de states); as requisicoes correm em paralelo
com ThreadPoolExecutor. _chamar_jev e o unico ponto que conhece HTTP. mapear_resposta e o
unico que conhece a forma da resposta. Os rotulos dos enums sao os de llm/juiza_schema.py.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from tcc_scraper.config import APELIDOS, NOMES_EMPRESA
from tcc_scraper.llm.juiza_schema import Evento, Papel, TipoConteudo
from tcc_scraper.llm.prompt import PromptRenderizado

logger = logging.getLogger(__name__)

# Versao das perguntas enviadas ao Jev. Mudou texto ou criterios: suba o numero.
JEV_PERGUNTAS_VERSAO = 1
NOME_PROMPT = "juiza_jev"
_CAMINHO = "/v1/systemone"
# Escala de qualidade vai de 0 (primeiro nivel) a 4 (ultimo nivel).
_NIVEL_MAXIMO = 4
_NIVEIS_QUALIDADE = ["muito baixa", "baixa", "média", "alta", "muito alta"]
_ESPERA_MAXIMA_S = 60.0
_BACKOFF = wait_exponential(multiplier=2, min=2, max=30)

_DESC_PAPEL = {
    "protagonista": "a empresa é o assunto central da notícia",
    "citada_com_fato": "a empresa é citada com um fato concreto sobre ela, mas não é o foco",
    "mencao": "a empresa só aparece de passagem, sem fato relevante sobre ela",
    "errada": "a notícia atribui à empresa um fato que não é dela, ou a confunde com outra",
    "matriz_estrangeira": "a notícia trata da matriz estrangeira, não da companhia listada",
}
_DESC_TIPO = {
    "reportagem": "texto jornalístico sobre fatos do dia",
    "opiniao": "coluna, editorial ou análise de opinião",
    "lista": "lista ou ranking de ações ou empresas",
    "agenda": "agenda da semana, calendário de eventos ou previsão",
    "resumo_mercado": "fechamento ou panorama geral do mercado",
    "podcast_video": "conteúdo em áudio ou vídeo com transcrição",
    "newsletter": "boletim ou newsletter de assinatura",
    "patrocinado": "conteúdo publicitário ou patrocinado",
    "outro": "nenhum dos anteriores",
}
_DESC_EVENTO = {
    "resultado": "divulgação de resultado trimestral ou anual",
    "dividendo_jcp": "dividendos, juros sobre capital próprio ou bonificação",
    "fato_relevante": "fato relevante ou comunicado oficial da companhia",
    "fusao_aquisicao": "fusão, aquisição, venda de ativo ou reorganização societária",
    "guidance": "projeção ou guidance de resultados",
    "recomendacao": "recomendação de compra ou venda por analistas",
    "regulatorio": "decisão de órgão regulador ou agência",
    "operacional": "operação do negócio, contrato, produção ou investimento",
    "governanca": "mudança de conselho, diretoria ou controle",
    "juridico": "processo, condenação, acordo ou investigação",
    "mudanca_ticker": "troca de código de negociação ou listagem",
    "macro": "fato de mercado ou economia que não é da empresa",
    "outro": "nenhum dos anteriores",
}

# Perguntas com {empresa} e {ticker} nas instrucoes. prompt_jev hasheia esta estrutura inteira.
_MODELO: dict[str, dict[str, Any]] = {
    "papel": {
        "type": "choice",
        "instructions": "Qual é o papel da empresa {empresa} ({ticker}) nesta notícia?",
        "criteria": {p.value: _DESC_PAPEL[p.value] for p in Papel},
    },
    "tipo_conteudo": {
        "type": "choice",
        "instructions": "Qual é o tipo de conteúdo desta notícia?",
        "criteria": {t.value: _DESC_TIPO[t.value] for t in TipoConteudo},
    },
    "evento": {
        "type": "choice",
        "instructions": "Qual é o principal evento da notícia para {empresa} ({ticker})?",
        "criteria": {e.value: _DESC_EVENTO[e.value] for e in Evento},
    },
    "qualidade": {
        "type": "score",
        "instructions": (
            "Qual a qualidade informativa desta notícia para quem acompanha {empresa} "
            "({ticker})? Considere especificidade, fato concreto e ausência de texto genérico."
        ),
        "criteria": list(_NIVEIS_QUALIDADE),
    },
}


@dataclass
class RespostaJev:
    # julgamentos no formato de JulgamentoItem (tickers_adicionais sempre vazio)
    itens: list[dict]
    tokens_entrada: int
    latencia_ms: int
    # respostas cruas que voltaram da API: {id, ticker, resposta}
    bruto: list[dict]
    # pares que falharam: {id, ticker, erro}
    erros: list[dict]


def estado_jev(item: dict) -> str:
    """Contexto do par: empresa, apelidos, fonte, titulo e trecho."""
    ticker = item["ticker"]
    apelidos = ", ".join(APELIDOS.get(ticker, [])) or "nenhum"
    return "\n".join([
        f"Empresa: {NOMES_EMPRESA.get(ticker, ticker)} ({ticker})",
        f"Apelidos: {apelidos}",
        f"Fonte: {item.get('source_domain') or ''}",
        f"Título: {item.get('title') or ''}",
        f"Trecho: {item.get('trecho') or ''}",
    ])


def perguntas_jev(item: dict) -> dict[str, dict[str, Any]]:
    """Questions do Jev para o par: Choice em papel, tipo_conteudo e evento; Score em qualidade."""
    ticker = item["ticker"]
    formato = {"empresa": NOMES_EMPRESA.get(ticker, ticker), "ticker": ticker}
    perguntas = copy.deepcopy(_MODELO)
    for pergunta in perguntas.values():
        pergunta["instructions"] = pergunta["instructions"].format(**formato)
    return perguntas


def prompt_jev() -> PromptRenderizado:
    """Versao fixa das perguntas. Mudou qualquer criterio: suba JEV_PERGUNTAS_VERSAO."""
    texto = json.dumps(
        {"versao": JEV_PERGUNTAS_VERSAO, "perguntas": _MODELO},
        ensure_ascii=False, sort_keys=True, indent=2,
    )
    return PromptRenderizado(
        nome=NOME_PROMPT,
        versao=JEV_PERGUNTAS_VERSAO,
        texto=texto,
        template=texto,
        sha256=hashlib.sha256(texto.encode("utf-8")).hexdigest(),
        schema={"perguntas": list(_MODELO)},
    )


def _chamar_jev(cliente: httpx.Client, modelo: str, item: dict) -> dict:
    """UNICO ponto que conhece HTTP: POST /v1/systemone com state e questions."""
    corpo = {"state": estado_jev(item), "model": modelo, "questions": perguntas_jev(item)}
    resposta = cliente.post(_CAMINHO, json=corpo)
    resposta.raise_for_status()
    return resposta.json()


def _transitorio(exc: BaseException) -> bool:
    """Repete em 408, 429, 5xx (529 incluso) e erro de conexao ou timeout.

    400, 401, 403, 404 e 422 nao melhoram repetindo.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        codigo = exc.response.status_code
        return codigo in (408, 429) or codigo >= 500
    return isinstance(exc, httpx.TransportError)


def _retry_after(cabecalhos: httpx.Headers) -> float | None:
    """Segundos pedidos pela API (retry-after-ms tem precedencia). Data HTTP cai no backoff."""
    with contextlib.suppress(ValueError):
        if "retry-after-ms" in cabecalhos:
            return max(0.0, min(float(cabecalhos["retry-after-ms"]) / 1000, _ESPERA_MAXIMA_S))
        if "retry-after" in cabecalhos:
            return max(0.0, min(float(cabecalhos["retry-after"]), _ESPERA_MAXIMA_S))
    return None


def _espera(estado: RetryCallState) -> float:
    exc = estado.outcome.exception() if estado.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        espera = _retry_after(exc.response.headers)
        if espera is not None:
            return espera
    return _BACKOFF(estado)


def _resposta(respostas: dict, nome: str, tipo: str) -> dict:
    resposta = respostas.get(nome)
    if not isinstance(resposta, dict) or resposta.get("type") != tipo:
        raise ValueError(f"{nome}: resposta do tipo {tipo!r} ausente")
    return resposta


def _numero(valor: Any, campo: str) -> float:
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        raise ValueError(f"{campo}: número esperado, veio {valor!r}")
    return float(valor)


def _escolha(respostas: dict, nome: str, enum_: type) -> str:
    """Rotulo escolhido, que tem de ser um valor do enum. Desconhecido e erro do item."""
    rotulo = _resposta(respostas, nome, "choice").get("choice")
    if rotulo not in [e.value for e in enum_]:
        raise ValueError(f"{nome}: rótulo desconhecido {rotulo!r}")
    return rotulo


def _confianca(respostas: dict, nome: str) -> float:
    return _numero(_resposta(respostas, nome, "choice").get("confidence"), f"{nome}.confidence")


def _qualidade(respostas: dict) -> float:
    score = _numero(_resposta(respostas, "qualidade", "score").get("score"), "qualidade.score")
    return min(max(round(score / _NIVEL_MAXIMO, 4), 0.0), 1.0)


def mapear_resposta(item: dict, bruto: dict) -> dict:
    """Resposta crua do Jev -> julgamento no formato de JulgamentoItem.

    Levanta ValueError se faltar resposta ou se um rotulo nao existir no enum.
    """
    respostas = bruto.get("answers") if isinstance(bruto, dict) else None
    if not isinstance(respostas, dict):
        raise ValueError("resposta sem 'answers'")
    return {
        "id": str(item["id"]),
        "ticker": item["ticker"],
        "papel": _escolha(respostas, "papel", Papel),
        "tipo_conteudo": _escolha(respostas, "tipo_conteudo", TipoConteudo),
        "evento": _escolha(respostas, "evento", Evento),
        "tickers_adicionais": [],
        "qualidade": _qualidade(respostas),
        # confianca do julgamento e a menor entre papel e tipo de conteudo
        "confianca": min(_confianca(respostas, "papel"), _confianca(respostas, "tipo_conteudo")),
    }


def _tokens_entrada(resposta: dict) -> int:
    valor = (resposta.get("usage") or {}).get("input_tokens")
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else 0


class JevClient:
    """Adaptador do Jev (TypeSafe AI). Uma requisicao por par, em paralelo."""

    def __init__(
        self,
        api_key: str,
        modelo: str = "jev-1.13.0",
        timeout_s: float = 30.0,
        max_attempts: int = 3,
        concorrencia: int = 8,
        base_url: str = "https://api.typesafe.ai",
    ) -> None:
        if not api_key:
            raise ValueError("JEV_API_KEY ausente")
        self.modelo, self.max_attempts, self.concorrencia = modelo, max_attempts, concorrencia
        self._cliente = httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout_s,
        )

    def classificar_lote(self, itens: list[dict]) -> RespostaJev:
        """Julga cada par. Falha de um item vai para `erros`; de todos, RuntimeError."""
        inicio = time.monotonic()
        if not itens:
            return RespostaJev(itens=[], tokens_entrada=0, latencia_ms=0, bruto=[], erros=[])
        with ThreadPoolExecutor(max_workers=self.concorrencia) as pool:
            resultados = list(pool.map(self._julgar_um, itens))
        julgamentos: list[dict] = []
        bruto: list[dict] = []
        erros: list[dict] = []
        tokens = 0
        for item, (resposta, erro) in zip(itens, resultados, strict=True):
            identificacao = {"id": str(item["id"]), "ticker": item["ticker"]}
            if resposta is None:
                erros.append({**identificacao, "erro": erro})
                continue
            bruto.append({**identificacao, "resposta": resposta})
            tokens += _tokens_entrada(resposta)
            try:
                julgamentos.append(mapear_resposta(item, resposta))
            except ValueError as exc:
                erros.append({**identificacao, "erro": f"resposta inválida: {exc}"})
        if erros and len(erros) == len(itens):
            raise RuntimeError(
                f"Jev: todos os {len(itens)} itens falharam (primeiro: {erros[0]['erro']})"
            )
        return RespostaJev(
            itens=julgamentos,
            tokens_entrada=tokens,
            latencia_ms=int((time.monotonic() - inicio) * 1000),
            bruto=bruto,
            erros=erros,
        )

    def close(self) -> None:
        """Fecha o cliente HTTP. A juiza chama ao fim da execucao quando foi ela que criou."""
        self._cliente.close()

    def _julgar_um(self, item: dict) -> tuple[dict | None, str | None]:
        """Devolve (resposta crua, None) ou (None, descricao do erro)."""
        chamada = retry(
            stop=stop_after_attempt(self.max_attempts),
            wait=_espera,
            retry=retry_if_exception(_transitorio),
            reraise=True,
            before_sleep=lambda s: logger.warning(
                "jev: tentativa %d falhou (%s)", s.attempt_number, s.outcome.exception(),
            ),
        )(_chamar_jev)
        try:
            bruta = chamada(self._cliente, self.modelo, item)
        except httpx.HTTPStatusError as exc:
            return None, f"HTTP {exc.response.status_code}"
        except Exception as exc:  # falha de um item nao derruba o lote
            return None, f"{type(exc).__name__}: {exc}"
        if not isinstance(bruta, dict):
            return None, "resposta não é um objeto JSON"
        return bruta, None
