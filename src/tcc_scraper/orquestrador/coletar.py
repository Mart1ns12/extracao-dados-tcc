"""Comando `coletar` do painel: valida os parametros e monta as etapas da execucao avulsa.

Cada item vira uma etapa (etapa_id = id do coletor no catalogo); `montar_entrega`, se houver,
vira a ultima etapa (`build_entrega`). Toda recusa e ValueError: a mensagem vai no ack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from tcc_scraper.config import IBOV_TICKERS, Settings
from tcc_scraper.db.models_entrega import COLUNAS
from tcc_scraper.orquestrador.coletores import CATALOGO, ColetorDef, args_fixos
from tcc_scraper.orquestrador.etapas import (
    EtapaDef,
    ResultadoEtapa,
    de_stats,
    montar_entrega,
    resultado_concordancia,
    resultado_corrigir_horas,
    resultado_finbert,
    resultado_juiza,
)

CHAVES = frozenset({"itens", "montar_entrega", "origem_verificacoes"})
CHAVES_ITEM = frozenset({"coletor", "refazer"})
ETAPA_ENTREGA = "build_entrega"
# build_entrega nao entra como item: a montagem vem de `montar_entrega`.
COLETAVEIS = tuple(c for c in CATALOGO if c != ETAPA_ENTREGA)


@dataclass(frozen=True)
class PlanoColeta:
    etapas: tuple[EtapaDef, ...]
    verificacoes: tuple[str, ...] = ()


def montar_plano(parametros: Any, settings: Settings) -> PlanoColeta:
    if not isinstance(parametros, dict):
        raise ValueError("parâmetros do coletar devem ser um objeto")
    desconhecidas = sorted(set(parametros) - CHAVES)
    if desconhecidas:
        raise ValueError(f"parâmetro desconhecido em coletar: {', '.join(desconhecidas)}")
    itens = parametros.get("itens")
    if not isinstance(itens, list) or not itens:
        raise ValueError("itens vazios: informe ao menos um coletor")
    etapas: list[EtapaDef] = []
    for ordem, item in enumerate(itens, start=1):
        etapa = _etapa_do_item(ordem, item, settings)
        if any(e.id == etapa.id for e in etapas):
            raise ValueError(
                f"coletor repetido nos itens: {etapa.id} (junte os parâmetros num item só)"
            )
        etapas.append(etapa)
    tabelas = _tabelas(parametros.get("montar_entrega"))
    if tabelas:
        etapas.append(_etapa_entrega(len(etapas) + 1, tabelas))
    verificacoes = parametros.get("origem_verificacoes") or []
    if not isinstance(verificacoes, list):
        verificacoes = [verificacoes]
    return PlanoColeta(tuple(etapas), tuple(str(v) for v in verificacoes))


def _etapa_do_item(ordem: int, item: Any, settings: Settings) -> EtapaDef:
    if not isinstance(item, dict):
        raise ValueError(f"item {ordem}: esperado um objeto com 'coletor'")
    cid = item.get("coletor")
    if cid == ETAPA_ENTREGA:
        raise ValueError("build_entrega não é item de coleta: use montar_entrega")
    if cid not in COLETAVEIS:
        raise ValueError(
            f"coletor desconhecido: {cid!r} (válidos: {', '.join(COLETAVEIS)})"
        )
    defn = CATALOGO[cid]
    extras = sorted(set(item) - CHAVES_ITEM - defn.aceita)
    if extras:
        raise ValueError(f"{cid} não aceita o parâmetro {', '.join(extras)}")
    refazer = item.get("refazer", False)
    if not isinstance(refazer, bool):
        raise ValueError(f"{cid}: refazer deve ser true ou false")
    if refazer and not defn.classe:
        raise ValueError(f"{cid} não tem checkpoint: refazer não se aplica")
    args: dict[str, Any] = {}
    if "anos" in item:
        args["anos"] = _anos(defn, item["anos"], settings)
    if "tickers" in item:
        args["tickers"] = _tickers(cid, item["tickers"])
    if "desde" in item or "ate" in item:
        args.update(_periodo(cid, item.get("desde"), item.get("ate"), settings))
    if cid == "corrigir_horas":
        args.update(_args_corrigir_horas(item))
    elif cid == "juiza_concordancia":
        args.update(_args_concordancia(item))
    args.update(args_fixos(cid, settings))
    resume = False if refazer else defn.resume
    return _etapa_coletor(ordem, defn, args, resume)


def _anos(defn: ColetorDef, anos: Any, settings: Settings) -> list[int]:
    if (
        not isinstance(anos, list)
        or not anos
        or any(isinstance(a, bool) or not isinstance(a, int) for a in anos)
    ):
        raise ValueError(f"{defn.id}: anos deve ser uma lista de anos inteiros")
    ini, fim = defn.janela_anos(settings)
    fora = sorted({a for a in anos if not ini <= a <= fim})
    if fora:
        raise ValueError(f"{defn.id}: ano fora da janela {ini}-{fim}: {fora}")
    return sorted(set(anos))


def _tickers(cid: str, tickers: Any) -> dict[str, str] | list[str]:
    if (
        not isinstance(tickers, list)
        or not tickers
        or not all(isinstance(t, str) for t in tickers)
    ):
        raise ValueError(f"{cid}: tickers deve ser uma lista de códigos")
    if cid == "google_news":
        universo = set(IBOV_TICKERS)
    else:
        # Import tardio: o modulo de precos nao deve ser carregado so para validar o painel.
        from tcc_scraper.collectors.prices_yahoo import tickers_padrao

        universo = set(tickers_padrao())
    fora = [t for t in tickers if t not in universo]
    if fora:
        raise ValueError(f"{cid}: ticker fora do universo: {', '.join(fora)}")
    unicos = list(dict.fromkeys(tickers))
    if cid == "google_news":
        # O coletor de noticias espera dict ticker -> empresa (google_news.py:156).
        return {t: IBOV_TICKERS[t] for t in unicos}
    return unicos


def _periodo(cid: str, desde: Any, ate: Any, settings: Settings) -> dict:
    if desde is None or ate is None:
        raise ValueError(f"{cid}: desde e ate devem vir juntos")
    try:
        ini_periodo = date.fromisoformat(desde)
        fim_periodo = date.fromisoformat(ate)
    except (TypeError, ValueError):
        raise ValueError(f"{cid}: datas no formato AAAA-MM-DD") from None
    if fim_periodo <= ini_periodo:
        raise ValueError(f"{cid}: ate deve ser posterior a desde")
    limite_ini = settings.inicio_noticias
    limite_fim = settings.fim_coleta + timedelta(days=1)
    if ini_periodo < limite_ini or fim_periodo > limite_fim:
        raise ValueError(
            f"{cid}: período fora da janela {limite_ini}..{limite_fim} (ate exclusivo)"
        )
    return {"desde": ini_periodo, "ate": fim_periodo}


def _args_corrigir_horas(item: dict) -> dict:
    # aplicar=false e a simulacao: corrige_horas_publicacao roda com dry_run=True e nao grava.
    if "aplicar" not in item:
        raise ValueError("corrigir_horas: aplicar é obrigatório (false simula, sem gravar)")
    aplicar = item["aplicar"]
    if not isinstance(aplicar, bool):
        raise ValueError("corrigir_horas: aplicar deve ser true ou false")
    limite = item.get("limite")
    if limite is not None and (
        isinstance(limite, bool) or not isinstance(limite, int) or limite < 1
    ):
        raise ValueError("corrigir_horas: limite deve ser um inteiro maior que zero, ou null")
    args: dict[str, Any] = {"dry_run": not aplicar}
    if limite is not None:
        args["limite"] = limite
    return args


def _args_concordancia(item: dict) -> dict:
    if item.get("n") is None:
        raise ValueError("juiza_concordancia: n é obrigatório (inteiro de 10 a 1000)")
    n = item["n"]
    if isinstance(n, bool) or not isinstance(n, int) or not 10 <= n <= 1000:
        raise ValueError("juiza_concordancia: n deve ser um inteiro de 10 a 1000")
    # semente ausente ou null vale o padrao da medicao (42).
    semente = item.get("semente")
    if semente is None:
        semente = 42
    if isinstance(semente, bool) or not isinstance(semente, int):
        raise ValueError("juiza_concordancia: semente deve ser um inteiro")
    return {"n": n, "semente": semente}


def _logar_contagens(p: Any, cid: str, valores: dict) -> None:
    # Uma linha por contagem: e o que o painel mostra no log da execucao.
    for chave, valor in valores.items():
        p.log("info", f"{cid}: {chave} = {valor}")


def _tabelas(valor: Any) -> list[str]:
    if valor is None:
        return []
    if not isinstance(valor, list) or not all(isinstance(t, str) for t in valor):
        raise ValueError("montar_entrega deve ser uma lista de tabelas")
    invalidas = [t for t in valor if t not in COLUNAS]
    if invalidas:
        raise ValueError(
            f"tabela de entrega inválida: {', '.join(invalidas)}"
            f" (válidas: {', '.join(COLUNAS)})"
        )
    return list(dict.fromkeys(valor))


def _etapa_coletor(ordem: int, defn: ColetorDef, args: dict, resume: bool) -> EtapaDef:
    def run(ctx: Any, p: Any) -> ResultadoEtapa:
        bruto = defn.executar(p, args=args, resume=resume)
        if defn.id == "juiza":
            return resultado_juiza(bruto)
        if defn.id == "finbert":
            return resultado_finbert(bruto)
        if defn.id == "corrigir_horas":
            modo = "simulação, nada gravado" if args["dry_run"] else "aplicado"
            p.log("info", f"corrigir_horas: {modo}")
            _logar_contagens(p, defn.id, bruto)
            return resultado_corrigir_horas(bruto)
        if defn.id == "juiza_concordancia":
            _logar_contagens(p, defn.id, bruto.resumo())
            return resultado_concordancia(bruto)
        return de_stats(bruto)

    return EtapaDef(defn.id, ordem, defn.nome, defn.id, run)


def _etapa_entrega(ordem: int, tabelas: list[str]) -> EtapaDef:
    def run(ctx: Any, p: Any) -> ResultadoEtapa:
        return montar_entrega(p, tabelas)

    return EtapaDef(ETAPA_ENTREGA, ordem, "Montagem da entrega", ETAPA_ENTREGA, run)
