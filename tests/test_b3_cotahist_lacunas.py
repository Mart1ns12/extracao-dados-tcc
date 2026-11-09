"""Lacunas de precos: dias do calendario sem Yahoo, preenchidos pelo COTAHIST ou pelo IBOV."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tcc_scraper.collectors import b3_cotahist
from tcc_scraper.collectors.b3_cotahist import (
    _calendario_ibov,
    candidatos_codigo,
    carregar_lacunas,
    encontrar_lacunas,
    gerar_lacunas,
)
from tcc_scraper.collectors.b3_indices import B3IndicesCollector
from tcc_scraper.collectors.prices_yahoo import LacunaPreco
from tcc_scraper.config import BENCHMARK_TICKER
from tests.test_b3_cotahist import _linha, _zip_com

DIA = dt.date


def _yahoo(valor: float, fonte: str = "yahoo") -> tuple:
    """Linha do banco (open, high, low, close, source) com O/H/L/C iguais."""
    return (valor, valor, valor, valor, fonte)


def _dia(codneg: str, data: str, centavos: int, quatot: int = 1000) -> str:
    """Registro COTAHIST com O/H/L/C iguais, por lote de 1 (FATCOT 1)."""
    return _linha(
        data=data, codneg=codneg, preabe=centavos, premax=centavos, premin=centavos,
        preult=centavos, quatot=quatot, fatcot=1,
    )


def _gerar(monkeypatch, tmp_path, *, tickers, calendario, precos, linhas=()):
    """Roda gerar_lacunas com banco, calendario, correcoes e COTAHIST falsos."""
    baixados: list[int] = []

    class ColetorFalso:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def baixar_ano(self, ano: int) -> bytes:
            baixados.append(ano)
            return _zip_com(*linhas)

    monkeypatch.setattr(b3_cotahist, "tickers_padrao", lambda: list(tickers))
    monkeypatch.setattr(b3_cotahist, "CotahistCollector", ColetorFalso)
    monkeypatch.setattr(b3_cotahist, "_calendario_ibov", lambda anos: dict(calendario))
    monkeypatch.setattr(b3_cotahist, "_precos_do_banco", lambda _tickers: precos)
    monkeypatch.setattr(b3_cotahist, "carregar_correcoes", lambda *_: {})
    monkeypatch.setattr(
        b3_cotahist,
        "get_settings",
        lambda: SimpleNamespace(price_start=DIA(2026, 1, 1), fim_coleta=DIA(2026, 12, 31)),
    )
    stats = gerar_lacunas(tmp_path)
    return stats, baixados


def test_candidato_antigo_vence_quando_o_atual_erra_a_escala(monkeypatch, tmp_path) -> None:
    # AXIA3 (codigo atual) fica 2% fora da escala do Yahoo; ELET6 0,5%; ELET3 0%.
    precos = {
        "AXIA3": {DIA(2026, 1, 10): _yahoo(10.0), DIA(2026, 1, 12): _yahoo(20.0)},
    }
    linhas = [
        _dia("AXIA3", "20260110", 5000),
        _dia("AXIA3", "20260111", 10000),
        _dia("AXIA3", "20260112", 10200),
        _dia("ELET3", "20260110", 5000),
        _dia("ELET3", "20260111", 5100, quatot=777),
        _dia("ELET3", "20260112", 10000),
        _dia("ELET6", "20260110", 5000),
        _dia("ELET6", "20260111", 5000),
        _dia("ELET6", "20260112", 9950),
    ]

    stats, _ = _gerar(
        monkeypatch, tmp_path, tickers=["AXIA3"],
        calendario={DIA(2026, 1, 10): 1.0, DIA(2026, 1, 11): 1.0, DIA(2026, 1, 12): 1.0},
        precos=precos, linhas=linhas,
    )

    assert stats["gravadas"] == 1
    assert stats["rejeitadas"] == 0
    lacuna = carregar_lacunas(tmp_path)["AXIA3"][0]
    assert lacuna.codneg == "ELET3"
    assert lacuna.date == DIA(2026, 1, 11)
    assert lacuna.close == pytest.approx(51.0)
    assert lacuna.volume == 777
    assert lacuna.fonte == "b3_cotahist"


def test_razao_instavel_sem_candidato_estavel_e_rejeitada(monkeypatch, tmp_path) -> None:
    # So o codigo atual tem dados, e 2% fora da escala do Yahoo: passa de 0,3%.
    precos = {
        "AXIA3": {DIA(2026, 1, 10): _yahoo(10.0), DIA(2026, 1, 12): _yahoo(20.0)},
    }
    linhas = [
        _dia("AXIA3", "20260110", 5000),
        _dia("AXIA3", "20260111", 10000),
        _dia("AXIA3", "20260112", 10200),
    ]

    stats, _ = _gerar(
        monkeypatch, tmp_path, tickers=["AXIA3"],
        calendario={DIA(2026, 1, 10): 1.0, DIA(2026, 1, 11): 1.0, DIA(2026, 1, 12): 1.0},
        precos=precos, linhas=linhas,
    )

    assert stats["gravadas"] == 0
    assert stats["rejeitadas"] == 1
    assert carregar_lacunas(tmp_path) == {}


def test_cada_ano_e_baixado_uma_vez(monkeypatch, tmp_path) -> None:
    # Duas lacunas (11/01 e 13/01) no mesmo ano: o COTAHIST de 2026 entra uma so vez.
    precos = {
        "AXIA3": {
            DIA(2026, 1, 10): _yahoo(10.0),
            DIA(2026, 1, 12): _yahoo(20.0),
            DIA(2026, 1, 14): _yahoo(30.0),
        },
    }
    linhas = [
        _dia("AXIA3", "20260110", 5000),
        _dia("AXIA3", "20260111", 5500),
        _dia("AXIA3", "20260112", 10000),
        _dia("AXIA3", "20260113", 12000),
        _dia("AXIA3", "20260114", 15000),
    ]

    stats, baixados = _gerar(
        monkeypatch, tmp_path, tickers=["AXIA3"],
        calendario={DIA(2026, 1, d): 1.0 for d in range(10, 15)},
        precos=precos, linhas=linhas,
    )

    assert baixados == [2026]
    assert stats["anos baixados"] == 1
    assert stats["gravadas"] == 2


def test_dia_dentro_de_janela_de_correcao_e_ignorado() -> None:
    calendario = {DIA(2026, 1, 10): 1.0, DIA(2026, 1, 11): 1.0, DIA(2026, 1, 12): 1.0}
    precos = {DIA(2026, 1, 10): _yahoo(10.0), DIA(2026, 1, 12): _yahoo(20.0)}

    assert encontrar_lacunas(calendario, precos, [(DIA(2026, 1, 11), DIA(2026, 1, 11))]) == []
    assert encontrar_lacunas(calendario, precos, []) == [DIA(2026, 1, 11)]


def test_dia_fora_do_intervalo_do_ticker_nao_e_lacuna() -> None:
    calendario = {DIA(2026, 1, d): 1.0 for d in (9, 10, 11, 12, 13)}
    precos = {DIA(2026, 1, 10): _yahoo(10.0), DIA(2026, 1, 12): _yahoo(20.0)}

    assert encontrar_lacunas(calendario, precos, []) == [DIA(2026, 1, 11)]


def test_indice_sai_com_fonte_b3(monkeypatch, tmp_path) -> None:
    precos = {
        BENCHMARK_TICKER: {DIA(2026, 1, 10): _yahoo(100.0), DIA(2026, 1, 12): _yahoo(102.0)},
    }

    stats, baixados = _gerar(
        monkeypatch, tmp_path, tickers=[BENCHMARK_TICKER],
        calendario={DIA(2026, 1, 10): 100.0, DIA(2026, 1, 11): 101.5, DIA(2026, 1, 12): 102.0},
        precos=precos,
    )

    assert baixados == []
    assert stats["rejeitadas"] == 0
    assert carregar_lacunas(tmp_path) == {
        BENCHMARK_TICKER: [
            LacunaPreco(
                ticker=BENCHMARK_TICKER, date=DIA(2026, 1, 11), codneg="IBOV",
                open=None, high=None, low=None, close=101.5, volume=None, fonte="b3",
            )
        ]
    }


def test_encontrar_lacunas_idempotente_com_b3_cotahist_ja_gravado() -> None:
    # 11/01 ja foi preenchido antes como b3_cotahist: continua lacuna, para regerar nao mudar.
    calendario = {DIA(2026, 1, 10): 1.0, DIA(2026, 1, 11): 1.0, DIA(2026, 1, 12): 1.0}
    precos = {
        DIA(2026, 1, 10): _yahoo(10.0),
        DIA(2026, 1, 11): _yahoo(5.0, fonte="b3_cotahist"),
        DIA(2026, 1, 12): _yahoo(20.0),
    }

    assert encontrar_lacunas(calendario, precos, []) == [DIA(2026, 1, 11)]


def test_candidatos_codigo_na_ordem_e_sem_repetir() -> None:
    assert candidatos_codigo("AXIA3") == ["AXIA3", "ELET3", "ELET6"]
    assert candidatos_codigo("ALOS3") == ["ALOS3", "ALSO3"]
    assert candidatos_codigo("PETR4") == ["PETR4"]


def test_calendario_ibov_le_o_fechamento_do_pregao(monkeypatch) -> None:
    chamadas: list[tuple[str, dict]] = []

    def get_json(self, metodo, params, base=None):
        chamadas.append((metodo, params))
        return {"results": [{"day": 10, "rateValue1": "1.000,50"}]}

    monkeypatch.setattr(B3IndicesCollector, "get_json", get_json)
    monkeypatch.setattr(
        b3_cotahist,
        "get_settings",
        lambda: SimpleNamespace(price_start=DIA(2026, 1, 1), fim_coleta=DIA(2026, 12, 31)),
    )

    calendario = _calendario_ibov([2026])

    assert calendario == {DIA(2026, 1, 10): 1000.5}
    assert chamadas[0][1]["index"] == "IBOV"
    assert chamadas[0][1]["year"] == "2026"


def test_calendario_ibov_sem_json_levanta_erro(monkeypatch) -> None:
    monkeypatch.setattr(B3IndicesCollector, "get_json", lambda self, *a, **k: None)
    monkeypatch.setattr(
        b3_cotahist,
        "get_settings",
        lambda: SimpleNamespace(price_start=DIA(2026, 1, 1), fim_coleta=DIA(2026, 12, 31)),
    )

    with pytest.raises(RuntimeError, match="IBOV de 2026"):
        _calendario_ibov([2026])
