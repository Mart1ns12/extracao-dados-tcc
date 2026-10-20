"""Estado do painel: execucoes, etapas, logs, heartbeat e comandos aplicados.

Dois adaptadores com o mesmo contrato: `RepositorioBanco` (tabelas v1-painel_*) e
`RepositorioMemoria` (dicts, para testes). Datas entram e saem aware em UTC.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update

from tcc_scraper.config import get_settings
from tcc_scraper.db.models_painel import (
    PainelComandoAplicado,
    PainelEtapa,
    PainelExecucao,
    PainelHeartbeat,
    PainelHistorico,
    PainelHistoricoProgresso,
    PainelLog,
)
from tcc_scraper.db.session import session_scope
from tcc_scraper.db.upsert import bulk_upsert

ATIVAS = ("em_andamento", "agendada")
# Carga historica ainda nao encerrada, na ordem de prioridade de `historico_atual`.
ATIVAS_HISTORICO = ("em_andamento", "pausando", "pausada", "cancelando")


class Repositorio(Protocol):
    def criar_execucao(self, execucao: dict, etapas: list[dict]) -> None: ...

    def atualizar_execucao(self, execucao_id: str, **campos) -> None: ...

    def atualizar_etapa(self, execucao_id: str, etapa_id: str, **campos) -> None: ...

    def log(
        self,
        origem: str,
        nivel: str,
        etapa: str | None,
        mensagem: str,
        execucao_id: str | None = None,
    ) -> None: ...

    def obter(self, execucao_id: str) -> dict | None:
        """Execucao + chave "etapas" (lista ordenada por ordem)."""

    def atual(self) -> dict | None:
        """Em andamento, senao agendada, senao a ultima por iniciada_em."""

    def listar(self, limite: int = 10) -> list[dict]:
        """Execucoes mais recentes, sem as etapas."""

    def existe_execucao_cron(self, dia_sp: date) -> bool: ...

    def marcar_interrompidas(self, motivo: str) -> int:
        """em_andamento -> falhou (execucoes e etapas). Devolve quantas execucoes."""

    def podar_logs(self, dias: int) -> int: ...

    def comando_aplicado(self, comando_id: str) -> bool: ...

    def registrar_comando(self, comando_id: str, ok: bool, erro: str | None) -> None: ...

    def heartbeat(self, host: str, versao: str, ocupado_com: str | None) -> None: ...

    def criar_historico(self, historico: dict) -> None: ...

    def atualizar_historico(self, historico_id: str, **campos) -> None: ...

    def historico_atual(self) -> dict | None:
        """Carga historica ativa (em_andamento, pausando, pausada, cancelando), senao a ultima."""

    def criar_progresso_historico(self, linhas: list[dict]) -> None:
        """Cria (ou sobrescreve) as linhas de progresso (ano, coletor) em lote."""

    def atualizar_progresso_historico(self, ano: int, coletor: str, **campos) -> None: ...

    def progresso_historico(self, ano: int) -> list[dict]:
        """Linhas de progresso do ano, ordenadas por coletor."""


def _como_dict(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _escolher_atual(execucoes: list[dict]) -> str | None:
    """Id da execucao 'atual': em andamento, senao agendada, senao a ultima iniciada."""
    for status in ATIVAS:
        ativas = [e for e in execucoes if e["status"] == status]
        if ativas:
            return max(ativas, key=lambda e: e["iniciada_em"])["id"]
    if execucoes:
        return max(execucoes, key=lambda e: e["iniciada_em"])["id"]
    return None


def _escolher_historico(historicos: list[dict]) -> dict | None:
    """Carga historica 'atual': a ativa (a mais recente), senao a ultima iniciada."""
    for status in ATIVAS_HISTORICO:
        ativas = [h for h in historicos if h["status"] == status]
        if ativas:
            return max(ativas, key=lambda h: h["iniciada_em"])
    if historicos:
        return max(historicos, key=lambda h: h["iniciada_em"])
    return None


class RepositorioBanco(Repositorio):
    """Grava nas tabelas v1-painel_* com sessoes curtas (uma por operacao)."""

    def __init__(self, tz: str | None = None) -> None:
        self._tz = ZoneInfo(tz or get_settings().daily_tz)

    def criar_execucao(self, execucao: dict, etapas: list[dict]) -> None:
        with session_scope() as s:
            bulk_upsert(
                s, PainelExecucao.__table__, [execucao], index_elements=["id"], update_columns=None
            )
            bulk_upsert(
                s,
                PainelEtapa.__table__,
                etapas,
                index_elements=["execucao_id", "etapa_id"],
                update_columns=None,
            )

    def atualizar_execucao(self, execucao_id: str, **campos) -> None:
        with session_scope() as s:
            s.execute(
                update(PainelExecucao).where(PainelExecucao.id == execucao_id).values(**campos)
            )

    def atualizar_etapa(self, execucao_id: str, etapa_id: str, **campos) -> None:
        with session_scope() as s:
            s.execute(
                update(PainelEtapa)
                .where(PainelEtapa.execucao_id == execucao_id, PainelEtapa.etapa_id == etapa_id)
                .values(**campos)
            )

    def log(
        self,
        origem: str,
        nivel: str,
        etapa: str | None,
        mensagem: str,
        execucao_id: str | None = None,
    ) -> None:
        # v1-painel_logs nao tem coluna de execucao (decisao do painel): execucao_id nao e gravado.
        with session_scope() as s:
            s.add(
                PainelLog(
                    ts=datetime.now(UTC),
                    nivel=nivel,
                    origem=origem,
                    etapa=etapa,
                    mensagem=mensagem,
                )
            )

    def obter(self, execucao_id: str) -> dict | None:
        with session_scope() as s:
            execucao = s.get(PainelExecucao, execucao_id)
            if execucao is None:
                return None
            etapas = (
                s.execute(
                    select(PainelEtapa)
                    .where(PainelEtapa.execucao_id == execucao_id)
                    .order_by(PainelEtapa.ordem)
                )
                .scalars()
                .all()
            )
            return {**_como_dict(execucao), "etapas": [_como_dict(e) for e in etapas]}

    def atual(self) -> dict | None:
        with session_scope() as s:
            execucoes = [
                {"id": e.id, "status": e.status, "iniciada_em": e.iniciada_em}
                for e in s.execute(
                    select(PainelExecucao.id, PainelExecucao.status, PainelExecucao.iniciada_em)
                ).all()
            ]
        eid = _escolher_atual(execucoes)
        return self.obter(eid) if eid else None

    def listar(self, limite: int = 10) -> list[dict]:
        with session_scope() as s:
            rows = (
                s.execute(
                    select(PainelExecucao)
                    .order_by(PainelExecucao.iniciada_em.desc())
                    .limit(limite)
                )
                .scalars()
                .all()
            )
            return [_como_dict(r) for r in rows]

    def existe_execucao_cron(self, dia_sp: date) -> bool:
        inicio = datetime.combine(dia_sp, time.min, tzinfo=self._tz).astimezone(UTC)
        fim = datetime.combine(dia_sp + timedelta(days=1), time.min, tzinfo=self._tz).astimezone(
            UTC
        )
        with session_scope() as s:
            achou = s.execute(
                select(func.count())
                .select_from(PainelExecucao)
                .where(
                    PainelExecucao.origem == "cron",
                    PainelExecucao.iniciada_em >= inicio,
                    PainelExecucao.iniciada_em < fim,
                )
            ).scalar_one()
        return achou > 0

    def marcar_interrompidas(self, motivo: str) -> int:
        agora = datetime.now(UTC)
        with session_scope() as s:
            ids = (
                s.execute(
                    select(PainelExecucao.id).where(PainelExecucao.status == "em_andamento")
                )
                .scalars()
                .all()
            )
            if not ids:
                return 0
            s.execute(
                update(PainelEtapa)
                .where(PainelEtapa.execucao_id.in_(ids), PainelEtapa.status == "em_andamento")
                .values(
                    status="falhou",
                    finalizada_em=agora,
                    ultimo_erro_mensagem=motivo,
                    atualizada_em=agora,
                )
            )
            s.execute(
                update(PainelExecucao)
                .where(PainelExecucao.id.in_(ids))
                .values(status="falhou", erro=motivo, finalizada_em=agora, atualizada_em=agora)
            )
            return len(ids)

    def podar_logs(self, dias: int) -> int:
        corte = datetime.now(UTC) - timedelta(days=dias)
        with session_scope() as s:
            return s.execute(delete(PainelLog).where(PainelLog.ts < corte)).rowcount

    def comando_aplicado(self, comando_id: str) -> bool:
        with session_scope() as s:
            return s.get(PainelComandoAplicado, comando_id) is not None

    def registrar_comando(self, comando_id: str, ok: bool, erro: str | None) -> None:
        with session_scope() as s:
            bulk_upsert(
                s,
                PainelComandoAplicado.__table__,
                [
                    {
                        "id": comando_id,
                        "aplicado_em": datetime.now(UTC),
                        "ok": ok,
                        "erro": erro,
                    }
                ],
                index_elements=["id"],
                update_columns=["aplicado_em", "ok", "erro"],
            )

    def heartbeat(self, host: str, versao: str, ocupado_com: str | None) -> None:
        with session_scope() as s:
            bulk_upsert(
                s,
                PainelHeartbeat.__table__,
                [
                    {
                        "host": host,
                        "versao": versao,
                        "ocupado_com": ocupado_com,
                        "visto_em": datetime.now(UTC),
                    }
                ],
                index_elements=["host"],
                update_columns=["versao", "ocupado_com", "visto_em"],
            )

    def criar_historico(self, historico: dict) -> None:
        with session_scope() as s:
            bulk_upsert(
                s,
                PainelHistorico.__table__,
                [historico],
                index_elements=["id"],
                update_columns=None,
            )

    def atualizar_historico(self, historico_id: str, **campos) -> None:
        with session_scope() as s:
            s.execute(
                update(PainelHistorico).where(PainelHistorico.id == historico_id).values(**campos)
            )

    def historico_atual(self) -> dict | None:
        with session_scope() as s:
            historicos = [
                _como_dict(h) for h in s.execute(select(PainelHistorico)).scalars().all()
            ]
        return _escolher_historico(historicos)

    def criar_progresso_historico(self, linhas: list[dict]) -> None:
        if not linhas:
            return
        with session_scope() as s:
            bulk_upsert(
                s,
                PainelHistoricoProgresso.__table__,
                linhas,
                index_elements=["ano", "coletor"],
                update_columns=[c for c in linhas[0] if c not in ("ano", "coletor")],
            )

    def atualizar_progresso_historico(self, ano: int, coletor: str, **campos) -> None:
        with session_scope() as s:
            s.execute(
                update(PainelHistoricoProgresso)
                .where(
                    PainelHistoricoProgresso.ano == ano,
                    PainelHistoricoProgresso.coletor == coletor,
                )
                .values(**campos)
            )

    def progresso_historico(self, ano: int) -> list[dict]:
        with session_scope() as s:
            linhas = (
                s.execute(
                    select(PainelHistoricoProgresso)
                    .where(PainelHistoricoProgresso.ano == ano)
                    .order_by(PainelHistoricoProgresso.coletor)
                )
                .scalars()
                .all()
            )
            return [_como_dict(r) for r in linhas]


class RepositorioMemoria(Repositorio):
    """Mesmo contrato sobre dicts. Usado pelos testes e nao toca banco nenhum."""

    def __init__(self, tz: str = "America/Sao_Paulo") -> None:
        self._tz = ZoneInfo(tz)
        self._lock = threading.Lock()
        self.execucoes: dict[str, dict] = {}
        self.etapas: dict[tuple[str, str], dict] = {}
        self.logs: list[dict] = []
        self.comandos: dict[str, dict] = {}
        self.heartbeats: dict[str, dict] = {}
        self.historicos: dict[str, dict] = {}
        self.progresso_historico_linhas: dict[tuple[int, str], dict] = {}

    def criar_execucao(self, execucao: dict, etapas: list[dict]) -> None:
        with self._lock:
            self.execucoes[execucao["id"]] = dict(execucao)
            for e in etapas:
                self.etapas[(e["execucao_id"], e["etapa_id"])] = dict(e)

    def atualizar_execucao(self, execucao_id: str, **campos) -> None:
        with self._lock:
            self.execucoes[execucao_id].update(campos)

    def atualizar_etapa(self, execucao_id: str, etapa_id: str, **campos) -> None:
        with self._lock:
            self.etapas[(execucao_id, etapa_id)].update(campos)

    def log(
        self,
        origem: str,
        nivel: str,
        etapa: str | None,
        mensagem: str,
        execucao_id: str | None = None,
    ) -> None:
        with self._lock:
            self.logs.append(
                {
                    "ts": datetime.now(UTC),
                    "origem": origem,
                    "nivel": nivel,
                    "etapa": etapa,
                    "mensagem": mensagem,
                    "execucao_id": execucao_id,
                }
            )

    def obter(self, execucao_id: str) -> dict | None:
        with self._lock:
            execucao = self.execucoes.get(execucao_id)
            if execucao is None:
                return None
            etapas = sorted(
                (dict(e) for (eid, _), e in self.etapas.items() if eid == execucao_id),
                key=lambda e: e["ordem"],
            )
            return {**execucao, "etapas": etapas}

    def atual(self) -> dict | None:
        with self._lock:
            eid = _escolher_atual(list(self.execucoes.values()))
        return self.obter(eid) if eid else None

    def listar(self, limite: int = 10) -> list[dict]:
        with self._lock:
            ordenadas = sorted(
                self.execucoes.values(), key=lambda e: e["iniciada_em"], reverse=True
            )
            return [dict(e) for e in ordenadas[:limite]]

    def existe_execucao_cron(self, dia_sp: date) -> bool:
        inicio = datetime.combine(dia_sp, time.min, tzinfo=self._tz).astimezone(UTC)
        fim = datetime.combine(dia_sp + timedelta(days=1), time.min, tzinfo=self._tz).astimezone(
            UTC
        )
        with self._lock:
            return any(
                e["origem"] == "cron" and inicio <= e["iniciada_em"] < fim
                for e in self.execucoes.values()
            )

    def marcar_interrompidas(self, motivo: str) -> int:
        agora = datetime.now(UTC)
        with self._lock:
            alvo = [eid for eid, e in self.execucoes.items() if e["status"] == "em_andamento"]
            for eid in alvo:
                self.execucoes[eid].update(
                    status="falhou", erro=motivo, finalizada_em=agora, atualizada_em=agora
                )
                for (xid, _), etapa in self.etapas.items():
                    if xid == eid and etapa["status"] == "em_andamento":
                        etapa.update(
                            status="falhou",
                            finalizada_em=agora,
                            ultimo_erro_mensagem=motivo,
                            atualizada_em=agora,
                        )
            return len(alvo)

    def podar_logs(self, dias: int) -> int:
        corte = datetime.now(UTC) - timedelta(days=dias)
        with self._lock:
            antes = len(self.logs)
            self.logs = [log for log in self.logs if log["ts"] >= corte]
            return antes - len(self.logs)

    def comando_aplicado(self, comando_id: str) -> bool:
        with self._lock:
            return comando_id in self.comandos

    def registrar_comando(self, comando_id: str, ok: bool, erro: str | None) -> None:
        with self._lock:
            self.comandos[comando_id] = {
                "id": comando_id,
                "aplicado_em": datetime.now(UTC),
                "ok": ok,
                "erro": erro,
            }

    def heartbeat(self, host: str, versao: str, ocupado_com: str | None) -> None:
        with self._lock:
            self.heartbeats[host] = {
                "host": host,
                "versao": versao,
                "ocupado_com": ocupado_com,
                "visto_em": datetime.now(UTC),
            }

    def criar_historico(self, historico: dict) -> None:
        with self._lock:
            self.historicos[historico["id"]] = dict(historico)

    def atualizar_historico(self, historico_id: str, **campos) -> None:
        with self._lock:
            self.historicos[historico_id].update(campos)

    def historico_atual(self) -> dict | None:
        with self._lock:
            return _escolher_historico([dict(h) for h in self.historicos.values()])

    def criar_progresso_historico(self, linhas: list[dict]) -> None:
        with self._lock:
            for linha in linhas:
                self.progresso_historico_linhas[(linha["ano"], linha["coletor"])] = dict(linha)

    def atualizar_progresso_historico(self, ano: int, coletor: str, **campos) -> None:
        # Como o UPDATE do banco: linha inexistente nao gera erro, so nao muda nada.
        with self._lock:
            linha = self.progresso_historico_linhas.get((ano, coletor))
            if linha is not None:
                linha.update(campos)

    def progresso_historico(self, ano: int) -> list[dict]:
        with self._lock:
            linhas = [
                dict(v) for (a, _), v in self.progresso_historico_linhas.items() if a == ano
            ]
        return sorted(linhas, key=lambda r: r["coletor"])
