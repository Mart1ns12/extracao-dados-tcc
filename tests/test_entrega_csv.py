"""escrever_csv: UTF-8 sem BOM, None vira campo vazio, cabecalho igual a COLUNAS."""

from __future__ import annotations

import csv
import datetime as dt

from tcc_scraper.db.models_entrega import COLUNAS
from tcc_scraper.entrega.writers import escrever_csv


def test_csv_sem_bom_com_cabecalho_e_vazio_para_none(tmp_path) -> None:
    linhas = [{
        "data": dt.date(2026, 6, 18), "ticker": "PETR4", "abertura": 37.12,
        "maxima": None, "minima": 36.95, "fechamento": 37.45,
        "fechamento_ajustado": 35.98, "volume": 41250300,
    }]
    caminho = escrever_csv("precos", linhas, COLUNAS["precos"], tmp_path)

    bruto = caminho.read_bytes()
    assert not bruto.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in bruto
    texto = bruto.decode("utf-8")
    cabecalho, linha = texto.splitlines()
    assert cabecalho == ",".join(COLUNAS["precos"])
    assert linha == "2026-06-18,PETR4,37.12,,36.95,37.45,35.98,41250300"


def test_floats_pequenos_saem_sem_notacao_cientifica(tmp_path) -> None:
    linhas = [{"data": dt.date(2026, 1, 2), "indice": "CDI", "valor": 1e-7}]
    caminho = escrever_csv("indices", linhas, COLUNAS["indices"], tmp_path)
    texto = caminho.read_text(encoding="utf-8")
    assert "e-" not in texto
    assert texto.splitlines()[1] == "2026-01-02,CDI,0.0000001"


def test_campos_com_virgula_e_aspas_sao_escapados(tmp_path) -> None:
    linhas = [{"id": "n000001", "ticker": "PETR4", "titulo": 'Petrobras, "campo" novo'}]
    caminho = escrever_csv("noticias", linhas, ["id", "ticker", "titulo"], tmp_path)
    with caminho.open(encoding="utf-8", newline="") as arquivo:
        lidas = list(csv.reader(arquivo))
    assert lidas[1] == ["n000001", "PETR4", 'Petrobras, "campo" novo']
