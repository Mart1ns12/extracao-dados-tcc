#!/usr/bin/env bash
# Coleta completa do Dataset 1.0 -- rodar NO SERVIDOR (mesma maquina do banco).
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
#   tail -f logs/03-noticias.log
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

$PY db-info || { echo "ERRO: banco inacessivel. Confira DB_TARGET e as credenciais no .env"; exit 1; }

# --- rapidas: preenchem 10 das 14 colunas -----------------------------------
etapa 01 precos  $PY collect-prices     # ~2 min   | 88 series do Yahoo
etapa 02 macro   $PY collect-macro      # ~1 min   | Selic + PTAX

# --- a longa ----------------------------------------------------------------
# ~9.000 requisicoes com throttle de 2 s (janela de 1 ano). Janelas que saturam
# o teto de 100 itens sao subdivididas em dias, o que adiciona requisicoes --
# conte 6 a 9 h, nao 5.
etapa 03 noticias $PY collect-news

# --- enriquecimento e sentimento -------------------------------------------
etapa 04 corpos   $PY enrich            # so veiculos da allowlist
etapa 05 finbert  $PY score             # FinBERT pt-BR + en

# --- dataset ----------------------------------------------------------------
# Grava em features_dataset_v1 E exporta Parquet/CSV.
etapa 06 dataset  $PY build-dataset

echo ""
echo "##############################################################"
echo "# fim: $(date '+%F %T')"
echo "##############################################################"
echo ""
echo "Dataset gravado em features_dataset_v1 (banco) e em exports/ :"
ls -lh exports/ 2>/dev/null || echo "  (nenhum -- veja $LOGS/06-dataset.log)"
echo ""
echo "Se alguma etapa falhou, rode o script de novo: ele retoma de onde parou."
