"""Composicao do IBOV por data: quais acoes estavam no indice em cada dia (v1-composicao_ibov).

Linha do tempo em tres fontes, em ordem cronologica:
1. Carteiras oficiais quadrimestrais, jan/2021 a set/2024: CSVs em data/composicao_ibov/
   (origem e atribuicao em LEIAME.md, na mesma pasta).
2. Rebalanceamentos (REBALANCEAMENTOS_IBOV), eventos extraordinarios (EVENTOS_IBOV_EXTRAORDINARIOS)
   e trocas de codigo (MUDANCAS_TICKER), depois de set/2024, em config.py.
3. Coletor diario (collectors/b3_carteira_ibov.py): mudancas vistas na API da B3. Ele grava
   direto na tabela (fonte b3_api), e o builder respeita essas vigencias ao reconstruir.

Tudo aqui e funcao pura: le os CSVs, recebe listas e devolve listas. O banco fica em builder.py.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tcc_scraper.config import (
    EVENTOS_IBOV_EXTRAORDINARIOS,
    MUDANCAS_TICKER,
    PROJECT_ROOT,
    REBALANCEAMENTOS_IBOV,
)

logger = logging.getLogger(__name__)

PASTA_CARTEIRAS = PROJECT_ROOT / "data" / "composicao_ibov"

# Inicio de vigencia das carteiras oficiais que nao caem no primeiro dia util (LEIAME.md).
# Chave: mes do arquivo, no formato AAAA-MM.
INICIO_CONHECIDO: dict[str, dt.date] = {
    "2023-05": dt.date(2023, 5, 2),
    "2023-09": dt.date(2023, 9, 4),
    "2024-01": dt.date(2024, 1, 2),
    "2024-05": dt.date(2024, 5, 6),
    "2024-09": dt.date(2024, 9, 2),
}

FONTE_CSV = "paccafelipe_csv"
FONTE_REBALANCEAMENTO = "rebalanceamento"
FONTE_API = "b3_api"

# Papel -> participacao (%). None quando a fonte nao traz o peso (rebalanceamentos).
Carteira = dict[str, float | None]


@dataclass(frozen=True)
class EventoIbov:
    """Mudanca na carteira a partir de `data`: papeis que entram e papeis que saem.

    `so_se_presente`: o evento so vale se o papel de `saem` estava na carteira. E o caso das
    trocas de codigo: trocar um codigo que nunca esteve no IBOV nao pode por o novo dentro.
    """

    data: dt.date
    entram: tuple[str, ...] = ()
    saem: tuple[str, ...] = ()
    so_se_presente: bool = False


def primeiro_dia_util(ano: int, mes: int) -> dt.date:
    """Primeiro dia de segunda a sexta do mes. Feriados nao entram na conta (regra da spec)."""
    dia = dt.date(ano, mes, 1)
    while dia.weekday() >= 5:
        dia += dt.timedelta(days=1)
    return dia


def inicio_da_carteira(nome_arquivo: str) -> dt.date:
    """Inicio de vigencia de IBOV_<ini>_<fim>.csv: a data conhecida do mes ou o 1o dia util."""
    ini = dt.date.fromisoformat(nome_arquivo.split("_")[1])
    chave = f"{ini.year:04d}-{ini.month:02d}"
    return INICIO_CONHECIDO.get(chave) or primeiro_dia_util(ini.year, ini.month)


def ler_carteira_csv(caminho: Path) -> Carteira:
    """Codigo -> participacao (%) de um CSV de carteira. Codigo repetido e erro."""
    carteira: Carteira = {}
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            codigo = (linha.get("Codigo") or "").strip()
            if not codigo:
                continue
            if codigo in carteira:
                raise ValueError(f"{caminho.name}: codigo {codigo} repetido")
            peso = (linha.get("Part %") or "").strip()
            carteira[codigo] = float(peso) if peso else None
    return carteira


def carregar_carteiras_csv(pasta: Path = PASTA_CARTEIRAS) -> list[tuple[dt.date, Carteira]]:
    """Uma carteira oficial por IBOV_*.csv da pasta, em ordem de inicio de vigencia."""
    carteiras = [
        (inicio_da_carteira(caminho.name), ler_carteira_csv(caminho))
        for caminho in sorted(pasta.glob("IBOV_*.csv"))
    ]
    carteiras.sort(key=lambda item: item[0])
    datas = [data for data, _ in carteiras]
    if len(set(datas)) != len(datas):
        raise ValueError(f"carteiras com o mesmo inicio de vigencia em {pasta}")
    return carteiras


def eventos_rebalanceamento() -> list[EventoIbov]:
    return [
        EventoIbov(data, tuple(entram), tuple(saem))
        for data, entram, saem in REBALANCEAMENTOS_IBOV
    ]


def eventos_extraordinarios() -> list[EventoIbov]:
    return [
        EventoIbov(data, tuple(entram), tuple(saem))
        for data, entram, saem in EVENTOS_IBOV_EXTRAORDINARIOS
    ]


def eventos_trocas(explicitos: Iterable[EventoIbov] = ()) -> list[EventoIbov]:
    """Trocas de MUDANCAS_TICKER como eventos. Troca sem data nao tem onde entrar: fica de fora.

    Troca que um evento explicito ja trata (o codigo antigo sai ou o novo entra nele) tambem
    fica de fora: aplicada de novo, poria o codigo novo na carteira por um segundo caminho.
    """
    explicitos = list(explicitos)
    saidas = {papel for evento in explicitos for papel in evento.saem}
    entradas = {papel for evento in explicitos for papel in evento.entram}
    eventos: list[EventoIbov] = []
    for antigo, novo, data in MUDANCAS_TICKER:
        if data is None:
            logger.warning("composicao_ibov: troca %s -> %s sem data; ignorada", antigo, novo)
            continue
        if antigo in saidas or novo in entradas:
            continue
        eventos.append(EventoIbov(data, (novo,), (antigo,), so_se_presente=True))
    return eventos


def aplicar_eventos(
    carteira: Carteira, eventos: Iterable[EventoIbov]
) -> list[tuple[dt.date, Carteira]]:
    """Aplica os eventos sobre `carteira`, em ordem de data. Devolve (data, carteira do dia).

    Eventos do mesmo dia valem juntos e geram um unico item. Papel que entra e ja estava
    mantem o peso antigo. Papel que sai e nao estava gera aviso e nao muda nada. Troca
    (so_se_presente) sem o codigo antigo na carteira nao faz nada.
    """
    atual: Carteira = dict(carteira)
    por_dia: dict[dt.date, list[EventoIbov]] = {}
    for evento in eventos:
        por_dia.setdefault(evento.data, []).append(evento)

    linha_do_tempo: list[tuple[dt.date, Carteira]] = []
    for dia in sorted(por_dia):
        for evento in por_dia[dia]:
            if evento.so_se_presente and not all(papel in atual for papel in evento.saem):
                continue
            for papel in evento.saem:
                if papel in atual:
                    del atual[papel]
                else:
                    logger.warning("composicao_ibov: %s sai em %s e nao estava", papel, dia)
            for papel in evento.entram:
                atual.setdefault(papel, None)
        linha_do_tempo.append((dia, dict(atual)))
    return linha_do_tempo


def linha_do_tempo_ibov(
    carteiras: list[tuple[dt.date, Carteira]],
) -> list[tuple[dt.date, Carteira, str]]:
    """Carteiras oficiais mais rebalanceamentos, eventos extraordinarios e trocas posteriores.

    Cada item traz a fonte da carteira que comeca naquele dia: paccafelipe_csv para as
    oficiais e rebalanceamento para o que vem depois da ultima.
    """
    if not carteiras:
        raise ValueError("sem carteira oficial para partir")
    base_data, base = carteiras[-1]
    explicitos = eventos_rebalanceamento() + eventos_extraordinarios()
    eventos: list[EventoIbov] = []
    for evento in explicitos + eventos_trocas(explicitos):
        if evento.data > base_data:
            eventos.append(evento)
        else:
            logger.warning("composicao_ibov: evento de %s antes da base; ignorado", evento.data)
    linha = [(data, carteira, FONTE_CSV) for data, carteira in carteiras]
    linha += [
        (data, carteira, FONTE_REBALANCEAMENTO)
        for data, carteira in aplicar_eventos(base, eventos)
    ]
    return linha


def montar_vigencias(linha_do_tempo: list[tuple[dt.date, Carteira, str]]) -> list[dict]:
    """Uma linha por periodo continuo de cada papel.

    `vigencia_fim` e a vespera da primeira carteira, depois do periodo, em que o papel nao
    aparece; NULL quando ainda esta na ultima. `participacao` e a do dia em que o periodo
    comeca, e `fonte` e a da carteira desse dia.
    """
    datas = [data for data, _, _ in linha_do_tempo]
    if len(set(datas)) != len(datas):
        raise ValueError("linha do tempo com duas carteiras no mesmo dia")

    abertas: dict[str, dict] = {}
    linhas: list[dict] = []
    for dia, carteira, fonte in sorted(linha_do_tempo, key=lambda item: item[0]):
        for papel in [p for p in abertas if p not in carteira]:
            aberta = abertas.pop(papel)
            aberta["vigencia_fim"] = dia - dt.timedelta(days=1)
            linhas.append(aberta)
        for papel, participacao in carteira.items():
            if papel not in abertas:
                abertas[papel] = {
                    "ticker": papel,
                    "vigencia_inicio": dia,
                    "vigencia_fim": None,
                    "participacao": participacao,
                    "fonte": fonte,
                }
    linhas.extend(abertas.values())
    return sorted(linhas, key=lambda r: (r["ticker"], r["vigencia_inicio"]))
