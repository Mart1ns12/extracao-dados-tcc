"""Testes das features de preco, agregacao de noticias e target.

Todos os numeros esperados foram calculados a mao a partir de series pequenas e
verificaveis. Se um teste falhar, suspeite do codigo antes de suspeitar do numero.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tcc_scraper.features.dataset import assemble_dataset
from tcc_scraper.features.news_agg import aggregate_news
from tcc_scraper.features.returns import compute_price_features, compute_target


def _serie(precos: list[float], volumes: list[int] | None = None, ticker: str = "TEST3"):
    """Serie diaria sintetica comecando em 2024-01-01, dias uteis consecutivos."""
    n = len(precos)
    datas = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": datas.date,
            "adj_close": precos,
            "volume": volumes if volumes is not None else [1000] * n,
        }
    )


# --------------------------------------------------------------------------------------
# Retornos
# --------------------------------------------------------------------------------------
def test_retorno_1d_e_variacao_contra_o_pregao_anterior() -> None:
    out = compute_price_features(_serie([100.0, 110.0, 99.0]))

    assert pd.isna(out.loc[0, "retorno_1d"]), "primeiro pregao nao tem anterior"
    assert out.loc[1, "retorno_1d"] == pytest.approx(0.10)
    assert out.loc[2, "retorno_1d"] == pytest.approx(-0.10)


def test_retorno_5d_e_acumulado_de_cinco_pregoes() -> None:
    precos = [100.0, 101.0, 102.0, 103.0, 104.0, 200.0]
    out = compute_price_features(_serie(precos))

    # indice 5 contra indice 0: 200/100 - 1 = 1.0
    assert out.loc[5, "retorno_5d"] == pytest.approx(1.0)
    assert pd.isna(out.loc[4, "retorno_5d"]), "sem 5 pregoes anteriores completos"


def test_volume_relativo_compara_com_media_de_20_pregoes() -> None:
    volumes = [100] * 20 + [300]
    out = compute_price_features(_serie([10.0] * 21, volumes=volumes))

    # media dos 20 anteriores = 100; volume do dia = 300 -> 3.0
    assert out.loc[20, "volume_relativo"] == pytest.approx(3.0)


def test_volatilidade_20d_e_zero_em_serie_de_retorno_constante() -> None:
    precos = [100.0 * (1.01**i) for i in range(25)]
    out = compute_price_features(_serie(precos))

    assert out.loc[24, "volatilidade_20d"] == pytest.approx(0.0, abs=1e-9)


def test_volatilidade_20d_positiva_em_serie_oscilante() -> None:
    precos = [100.0 if i % 2 == 0 else 110.0 for i in range(25)]
    out = compute_price_features(_serie(precos))

    assert out.loc[24, "volatilidade_20d"] > 0


def test_features_calculadas_por_ticker_sem_vazar_entre_ativos() -> None:
    """Concatenar dois ativos nao pode fazer o primeiro pregao de um usar o outro."""
    a = _serie([100.0, 110.0], ticker="AAAA3")
    b = _serie([50.0, 25.0], ticker="BBBB3")
    out = compute_price_features(pd.concat([a, b], ignore_index=True))

    primeiro_b = out[(out.ticker == "BBBB3")].iloc[0]
    assert pd.isna(primeiro_b["retorno_1d"]), "primeiro pregao de BBBB3 nao tem anterior"
    segundo_b = out[(out.ticker == "BBBB3")].iloc[1]
    assert segundo_b["retorno_1d"] == pytest.approx(-0.50)


# --------------------------------------------------------------------------------------
# Target
# --------------------------------------------------------------------------------------
def test_target_1_quando_acao_supera_ibov_no_dia_seguinte() -> None:
    acao = _serie([100.0, 100.0, 102.0])       # retorno em t+1 = +2%
    ibov = _serie([100.0, 100.0, 101.0], ticker="^BVSP")  # +1%

    out = compute_target(compute_price_features(acao), compute_price_features(ibov))

    assert out.loc[1, "target"] == 1


def test_target_0_quando_acao_perde_do_ibov() -> None:
    acao = _serie([100.0, 100.0, 100.5])       # +0.5%
    ibov = _serie([100.0, 100.0, 102.0], ticker="^BVSP")  # +2%

    out = compute_target(compute_price_features(acao), compute_price_features(ibov))

    assert out.loc[1, "target"] == 0


def test_ultima_data_fica_sem_target() -> None:
    """Nao existe t+1 para o ultimo pregao: inventar um valor seria fabricar dado."""
    acao = _serie([100.0, 101.0, 102.0])
    ibov = _serie([100.0, 101.0, 102.0], ticker="^BVSP")

    out = compute_target(compute_price_features(acao), compute_price_features(ibov))

    assert pd.isna(out.iloc[-1]["target"])


# --------------------------------------------------------------------------------------
# Vazamento temporal -- requisito de corretude, nota 6 de docs/fontes_dataset.xlsx
# --------------------------------------------------------------------------------------
LEAK_COLS = ["retorno_1d", "retorno_5d", "retorno_20d", "volatilidade_20d", "volume_relativo"]


def test_features_de_preco_nao_usam_o_futuro() -> None:
    """Alterar precos FUTUROS nao pode mudar nenhuma feature de datas anteriores.

    Este e o teste central contra vazamento: se qualquer janela estiver centrada
    ou deslocada para frente, o valor em t muda quando t+k muda.

    A serie precisa ser longa o bastante para as janelas de 20 pregoes produzirem
    valores reais -- com uma serie curta todas as colunas ficam NaN e a comparacao
    passa sem testar nada.
    """
    n = 60
    precos = [100.0 + (i % 7) * 3 - (i % 4) * 2 for i in range(n)]
    volumes = [1000 + (i % 11) * 130 for i in range(n)]

    base = _serie(precos, volumes=volumes)
    alterado = base.copy()
    # Corrompe TODO o futuro a partir do pregao 45, nao so o ultimo: uma janela
    # centrada de 20 dias em t=44 alcancaria ate t=54.
    alterado.loc[45:, "adj_close"] = 9999.0
    alterado.loc[45:, "volume"] = 99999

    out_base = compute_price_features(base)
    out_alt = compute_price_features(alterado)

    corte = 44
    # Guarda: se as colunas forem NaN, o teste nao esta exercitando nada.
    for col in LEAK_COLS:
        assert out_base.loc[:corte, col].notna().any(), f"{col} sem valores: teste vazio"

    pd.testing.assert_frame_equal(
        out_base.loc[:corte, LEAK_COLS],
        out_alt.loc[:corte, LEAK_COLS],
        check_exact=False,
    )


def test_agregacao_de_noticias_nao_usa_o_futuro() -> None:
    """Noticia publicada depois da data da observacao nao pode entrar na linha."""
    artigos = pd.DataFrame(
        {
            "article_id": [1, 2],
            "ticker": ["TEST3", "TEST3"],
            "published_at": pd.to_datetime(
                ["2024-01-02 10:00:00+00:00", "2024-01-03 10:00:00+00:00"]
            ),
            "prob_pos": [0.9, 0.1],
            "prob_neu": [0.05, 0.1],
            "prob_neg": [0.05, 0.8],
        }
    )
    pregoes = pd.DataFrame({"ticker": ["TEST3"], "date": [dt.date(2024, 1, 2)]})

    out = aggregate_news(artigos, pregoes)

    linha = out.iloc[0]
    assert linha["quant_noticias"] == 1, "a noticia do dia 03 nao pode contar no dia 02"
    assert linha["positivo"] == pytest.approx(0.9)


# --------------------------------------------------------------------------------------
# Agregacao de noticias
# --------------------------------------------------------------------------------------
def test_score_finbert_e_positivo_menos_negativo() -> None:
    artigos = pd.DataFrame(
        {
            "article_id": [1, 2],
            "ticker": ["TEST3", "TEST3"],
            "published_at": pd.to_datetime(
                ["2024-01-02 08:00:00+00:00", "2024-01-02 12:00:00+00:00"]
            ),
            "prob_pos": [0.8, 0.6],
            "prob_neu": [0.1, 0.2],
            "prob_neg": [0.1, 0.2],
        }
    )
    pregoes = pd.DataFrame({"ticker": ["TEST3"], "date": [dt.date(2024, 1, 2)]})

    linha = aggregate_news(artigos, pregoes).iloc[0]

    assert linha["quant_noticias"] == 2
    assert linha["positivo"] == pytest.approx(0.7)
    assert linha["negativo"] == pytest.approx(0.15)
    assert linha["score_finbert"] == pytest.approx(0.55)


def test_dia_sem_noticia_fica_com_contagem_zero_e_sentimento_nulo() -> None:
    """Zero noticias nao e sentimento neutro -- preencher com 0 seria inventar dado."""
    artigos = pd.DataFrame(
        columns=["article_id", "ticker", "published_at", "prob_pos", "prob_neu", "prob_neg"]
    )
    pregoes = pd.DataFrame({"ticker": ["TEST3"], "date": [dt.date(2024, 1, 2)]})

    linha = aggregate_news(artigos, pregoes).iloc[0]

    assert linha["quant_noticias"] == 0
    assert pd.isna(linha["positivo"])
    assert pd.isna(linha["score_finbert"])


# --------------------------------------------------------------------------------------
# Montagem final
# --------------------------------------------------------------------------------------
def test_dataset_final_tem_exatamente_as_14_colunas() -> None:
    esperado = [
        "data", "ticker", "quant_noticias", "positivo", "neutro", "negativo",
        "score_finbert", "retorno_1d", "retorno_5d", "retorno_20d",
        "volatilidade_20d", "volume_relativo", "retorno_ibovespa", "target",
    ]
    precos = pd.concat(
        [_serie([100.0, 101.0, 102.0]), _serie([100.0, 100.5, 101.0], ticker="^BVSP")],
        ignore_index=True,
    )
    artigos = pd.DataFrame(
        columns=["article_id", "ticker", "published_at", "prob_pos", "prob_neu", "prob_neg"]
    )

    out = assemble_dataset(precos, artigos, benchmark="^BVSP")

    assert list(out.columns) == esperado
    assert "^BVSP" not in set(out["ticker"]), "o benchmark nao e uma linha do dataset"


def test_series_macro_nao_viram_linhas_do_dataset() -> None:
    """Brent e dolar estao em raw.prices, mas nao sao ativos observados.

    Bug encontrado no piloto: assemble_dataset inferia os ativos por exclusao
    ("tudo menos o benchmark"), entao BZ=F e USDBRL=X viraram linhas do dataset --
    e o USDBRL=X, sem volume por ser forex, inflava os nulos de volume_relativo.
    """
    precos = pd.concat(
        [
            _serie([100.0, 101.0, 102.0], ticker="PETR4"),
            _serie([100.0, 100.5, 101.0], ticker="^BVSP"),
            _serie([80.0, 81.0, 82.0], ticker="BZ=F"),
            _serie([5.0, 5.1, 5.2], ticker="USDBRL=X"),
        ],
        ignore_index=True,
    )
    artigos = pd.DataFrame(
        columns=["article_id", "ticker", "published_at", "prob_pos", "prob_neu", "prob_neg"]
    )

    out = assemble_dataset(precos, artigos, benchmark="^BVSP")

    assert set(out["ticker"]) == {"PETR4"}, "so acoes podem virar linha do dataset"


def test_universo_explicito_restringe_os_ativos() -> None:
    """Passar o universo explicitamente e mais seguro que inferir por exclusao."""
    precos = pd.concat(
        [
            _serie([100.0, 101.0, 102.0], ticker="PETR4"),
            _serie([50.0, 51.0, 52.0], ticker="VALE3"),
            _serie([100.0, 100.5, 101.0], ticker="^BVSP"),
        ],
        ignore_index=True,
    )
    artigos = pd.DataFrame(
        columns=["article_id", "ticker", "published_at", "prob_pos", "prob_neu", "prob_neg"]
    )

    out = assemble_dataset(precos, artigos, benchmark="^BVSP", universe=["PETR4"])

    assert set(out["ticker"]) == {"PETR4"}
