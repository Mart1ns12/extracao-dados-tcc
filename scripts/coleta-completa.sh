#!/usr/bin/env bash
# Coleta completa do Dataset 1.0 -- rodar NO SERVIDOR (mesma maquina do banco).
#
# NO RAILWAY quem roda e o executor (`python -m tcc_scraper.cli executor`, worker sempre
# ligado, ver docs/07-executor-e-painel.md). Este script e para rodar FORA dele, numa maquina com
# acesso direto ao banco. Nao rode os dois ao mesmo tempo no mesmo banco.
#
# Cada etapa e retomavel: se o script cair (ou voce interromper com Ctrl-C),
# rodar de novo continua de onde parou. Nenhuma etapa recomeca do zero.
#
# Uso:
#   chmod +x scripts/coleta-completa.sh
#   nohup ./scripts/coleta-completa.sh > logs/coleta.log 2>&1 &
#   tail -f logs/coleta.log
#
# Para acompanhar de outro terminal:
#   tail -f logs/04-noticias.log
#   python -m tcc_scraper.cli db-info

set -uo pipefail   # sem -e: uma etapa que falha nao deve impedir as seguintes

LOGS="logs"
mkdir -p "$LOGS"
PY="python -m tcc_scraper.cli"

etapa() {
    local num="$1" nome="$2" log="$LOGS/$1-$2.log"
    shift 2
    echo ""
    echo "=============================================================="
    echo ">> [$num] $nome  --  $(date '+%F %T')"
    echo "   log: $log"
    echo "=============================================================="
    if "$@" > "$log" 2>&1; then
        tail -20 "$log"
        echo "   [$num] $nome: OK"
    else
        echo "   [$num] $nome: FALHOU (codigo $?) -- ultimas linhas:"
        tail -25 "$log"
        echo "   >>> as etapas seguintes continuam; reveja este log depois <<<"
    fi
}

echo "##############################################################"
echo "# COLETA COMPLETA -- Dataset 1.0"
echo "# inicio: $(date '+%F %T')"
echo "##############################################################"

$PY db-info || { echo "ERRO: banco inacessivel. Confira IP_BANCO, USER_BANCO, SENHA_BANCO e PORTA_BANCO no .env"; exit 1; }

# --- rapidas: precos, macro e indices -----------------------------------------
etapa 01 precos    $PY collect-prices     # ~2 min   | 88 series do Yahoo
etapa 02 macro     $PY collect-macro      # ~1 min   | Selic + PTAX
etapa 03 indices   $PY collect-indices    # CDI + IPCA (SGS 12 e 433)

# --- a longa ----------------------------------------------------------------
# ~9.000 requisicoes com throttle de 2 s (janela de 1 ano). Janelas que saturam
# o teto de 100 itens sao subdivididas em dias, o que adiciona requisicoes --
# conte 6 a 9 h, nao 5.
etapa 04 noticias  $PY collect-news

# --- fatos relevantes, corpo e sentimento -----------------------------------
etapa 05 ipe       $PY collect-ipe        # fatos relevantes (IPE) do CVM
etapa 06 corpos    $PY enrich             # so veiculos da allowlist
etapa 07 finbert   $PY score              # FinBERT pt-BR + en
etapa 08 juiza     $PY judge-news         # regra, Jev (desligado) e Gemini; sem busca na web

# --- cadastro e eventos (CVM e B3) -------------------------------------------
etapa 09 cvm       $PY collect-cvm        # ITR e DFP
etapa 10 cadastro  $PY collect-cadastro   # CVM cad_cia_aberta + B3 GetDetail
etapa 11 proventos $PY collect-proventos  # dividendos, JCP etc. (B3)

# --- dataset v1 (janela fixa, reproduzivel) ---------------------------------
# Grava em features_dataset_v1 E exporta Parquet/CSV.
etapa 12 dataset   $PY build-dataset

# --- entrega (8 tabelas, fim rolante) ----------------------------------------
etapa 13 entrega   $PY build-entrega --csv

echo ""
echo "##############################################################"
echo "# fim: $(date '+%F %T')"
echo "##############################################################"
echo ""
echo "Dataset gravado em features_dataset_v1 (banco), em exports/ (Parquet/CSV)"
echo "e nas 8 tabelas da entrega (banco, e CSV em exports/):"
ls -lh exports/ 2>/dev/null || echo "  (nenhum -- veja $LOGS/12-dataset.log)"
echo ""
echo "Se alguma etapa falhou, rode o script de novo: ele retoma de onde parou."
