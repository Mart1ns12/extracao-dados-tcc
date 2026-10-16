"""Orquestra a analise diaria: prompt -> Gemini -> raw_llm_runs -> staging_llm_*.

Pensado para o cron do Railway: faz tudo, grava, fecha o engine e devolve. O processo
precisa SAIR -- por isso get_engine().dispose() no finally; sem isso o pool do MySQL
segura a conexao e o container fica pendurado.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select

from tcc_scraper.config import BENCHMARK_TICKER, get_settings
from tcc_scraper.db import session as db_session
from tcc_scraper.db.models import (
    LlmAcaoSinal,
    LlmMercado,
    LlmNoticiaEmpresa,
    LlmNoticiaMacro,
    LlmPrompt,
    LlmRun,
    Price,
)
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.llm.parse import (
    filtrar_universo,
    mapear_linhas,
    parse_resposta,
    resolver_redirects,
)
from tcc_scraper.llm.prompt import PromptRenderizado, renderizar_prompt
from tcc_scraper.llm.redirects import resolver_redirect_http

logger = logging.getLogger(__name__)

_STAGING = (
    (LlmNoticiaEmpresa, ["data_referencia", "ticker", "item_hash"], "noticias_empresas"),
    (LlmNoticiaMacro, ["data_referencia", "item_hash"], "noticias_macro"),
    (LlmAcaoSinal, ["data_referencia", "ticker"], "acoes_sinais"),
    (LlmMercado, ["data_referencia", "nome"], "mercados"),
)


@dataclass
class RunResult:
    status: str   # ok | erro_api | erro_json | erro_validacao | erro_gravacao | dry_run
    data_referencia: dt.date
    pregao_referencia: dt.date | None = None
    run_id: int | None = None
    linhas: dict[str, int] = field(default_factory=dict)
    erro: str | None = None
    prompt: str | None = None   # so no dry-run
    tokens: dict[str, int | None] = field(default_factory=dict)


def hoje_local(tz: str) -> dt.date:
    return dt.datetime.now(ZoneInfo(tz)).date()


def pregao_referencia(session, data: dt.date) -> dt.date | None:
    """Ultimo pregao <= data pelo calendario do ^BVSP em raw_prices; None sem preco."""
    return session.execute(
        select(func.max(Price.date)).where(
            Price.ticker == BENCHMARK_TICKER, Price.date <= data
        )
    ).scalar()


def garantir_prompt(session, prompt: PromptRenderizado) -> int:
    """Upsert do template por (nome, versao); recusa texto diferente na mesma versao."""
    existente = session.execute(
        select(LlmPrompt.id, LlmPrompt.sha256)
        .where(LlmPrompt.nome == prompt.nome, LlmPrompt.versao == prompt.versao)
    ).first()
    if existente:
        if existente.sha256 != prompt.sha256:
            raise RuntimeError(
                f"template {prompt.nome} v{prompt.versao} mudou (sha256 difere do banco); "
                "suba VERSAO em llm/prompt.py e crie o arquivo _v{N}.txt"
            )
        return existente.id
    bulk_upsert(session, LlmPrompt.__table__, [{
        "nome": prompt.nome, "versao": prompt.versao, "texto_template": prompt.template,
        "schema_json": prompt.schema, "sha256": prompt.sha256,
        "criado_em": dt.datetime.now(dt.UTC),
    }], index_elements=["nome", "versao"])
    session.flush()
    return session.execute(select(LlmPrompt.id).where(
        LlmPrompt.nome == prompt.nome, LlmPrompt.versao == prompt.versao)).scalar_one()


def _gravar_staging(session, data: dt.date, linhas) -> None:
    """Substitui o dia inteiro: DELETE por data_referencia + upsert, na mesma transacao."""
    for modelo, chave, attr in _STAGING:
        session.execute(modelo.__table__.delete().where(modelo.data_referencia == data))
        rows = getattr(linhas, attr)
        if rows:
            atualizaveis = [c.name for c in modelo.__table__.columns if c.name not in chave]
            bulk_upsert(session, modelo.__table__, rows, index_elements=chave,
                        update_columns=atualizaveis)


def run_daily_analysis(data_referencia: dt.date | None = None, *, dry_run: bool = False,
                       modelo: str | None = None, cliente=None, resolver=None) -> RunResult:
    """`cliente` injetavel: qualquer objeto com .analisar(prompt, schema) -> RespostaGemini.

    `resolver` injetavel: callable(url) -> URL real ou None. Padrao: resolver_redirect_http.
    """
    settings = get_settings()
    data = data_referencia or hoje_local(settings.llm_tz)
    prompt = renderizar_prompt(data)
    if dry_run:
        return RunResult(status="dry_run", data_referencia=data, prompt=prompt.texto)

    modelo = modelo or settings.gemini_model
    try:
        if cliente is None:
            # lazy: extra `llm`. Dentro do try para o finally fechar o engine mesmo se a
            # chave estiver ausente (ValueError).
            from tcc_scraper.llm.gemini_client import GeminiClient
            cliente = GeminiClient(
                settings.gemini_api_key, modelo, settings.llm_timeout_s,
                settings.llm_max_attempts,
            )

        with db_session.session_scope() as s:
            prompt_id = garantir_prompt(s, prompt)
            pregao = pregao_referencia(s, data)

        iniciado = dt.datetime.now(dt.UTC)
        resposta, status, erro, analise = None, "ok", None, None
        try:
            resposta = cliente.analisar(prompt.texto, prompt.schema)
        except Exception as exc:  # noqa: BLE001 -- qualquer falha da API vira linha em raw_llm_runs
            status, erro = "erro_api", f"{type(exc).__name__}: {exc}"[:4000]
        finalizado = dt.datetime.now(dt.UTC)

        if resposta is not None:
            try:
                analise = filtrar_universo(parse_resposta(resposta.texto))
                analise = resolver_redirects(analise, resolver or resolver_redirect_http)
            except json.JSONDecodeError as exc:
                status, erro = "erro_json", str(exc)[:4000]
            except ValidationError as exc:
                status, erro = "erro_validacao", str(exc)[:4000]
            if analise is not None and analise.data_referencia != data:
                logger.warning("llm: modelo respondeu data %s, esperado %s",
                               analise.data_referencia, data)

        run = LlmRun(
            prompt_id=prompt_id, data_referencia=data, pregao_referencia=pregao, modelo=modelo,
            prompt_renderizado=prompt.texto,
            resposta_bruta=resposta.texto if resposta else None,
            resposta_json=analise.model_dump(mode="json") if analise else None,
            resumo_geral=analise.resumo_geral if analise else None,
            buscas_json=resposta.buscas if resposta else None,
            citacoes_json=resposta.citacoes if resposta else None,
            tokens_entrada=resposta.tokens_entrada if resposta else None,
            tokens_saida=resposta.tokens_saida if resposta else None,
            tokens_pensamento=resposta.tokens_pensamento if resposta else None,
            latencia_ms=resposta.latencia_ms if resposta else None,
            status=status, erro=erro, iniciado_em=iniciado, finalizado_em=finalizado,
        )
        with db_session.session_scope() as s:
            s.add(run)
            s.flush()
            run_id = run.id

        resultado = RunResult(
            status=status, data_referencia=data, pregao_referencia=pregao,
            run_id=run_id, erro=erro,
            tokens={"entrada": run.tokens_entrada, "saida": run.tokens_saida,
                    "pensamento": run.tokens_pensamento},
        )
        if status != "ok":
            return resultado

        linhas = mapear_linhas(analise, run_id=run_id, data_referencia=data,
                               pregao_referencia=pregao)
        try:
            with db_session.session_scope() as s:
                _gravar_staging(s, data, linhas)
        except Exception as exc:  # noqa: BLE001
            with db_session.session_scope() as s:
                s.execute(LlmRun.__table__.update().where(LlmRun.id == run_id)
                          .values(status="erro_gravacao",
                                  erro=f"{type(exc).__name__}: {exc}"[:4000]))
            resultado.status, resultado.erro = "erro_gravacao", str(exc)
            return resultado
        resultado.linhas = linhas.contagem()
        return resultado
    finally:
        db_session.get_engine().dispose()
