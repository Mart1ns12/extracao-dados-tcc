# Pipeline diária

Este documento cobre as nove etapas do `daily`, na ordem em que rodam: o que cada uma faz, de qual fonte vem o dado, a unidade de trabalho, as tabelas que grava e o que acontece quando falha. Cobre também o fim rolante da coleta, o orçamento de tempo, o prazo e os status da execução.

## 1. O que é o daily

Uma execução do `daily` roda as nove etapas em sequência, para o dia de referência (`fim_coleta`, seção 2). Ela nasce de três formas:

| Origem | Como dispara | `origem` gravada |
|---|---|---|
| Agenda | O executor cria o daily às `DAILY_HORA` (08:00, em `DAILY_TZ`), uma vez por dia | `cron` |
| Painel | Comando `daily` ou `etapa` enviado pela API do painel (ver [07](07-executor-e-painel.md)) | `manual` |
| CLI | `python -m tcc_scraper.cli daily`, bloqueante, sem painel | `manual`, ou o valor de `--origem` |

```bash
python -m tcc_scraper.cli daily                  # as 9 etapas
python -m tcc_scraper.cli daily --etapa precos   # so uma etapa; a execucao vira do tipo "etapa"
```

A CLI roda o daily no processo atual e não aplica a prioridade da carga histórica. Essa regra existe só no caminho do executor (ver [03](03-carga-historica.md), seção 7).

## 2. Dia de referência e janelas de coleta

- **Fim rolante.** O dia de referência (`fim_coleta`) é hoje, no fuso `DAILY_TZ` (padrão `America/Sao_Paulo`). A variável `DATA_FIM_COLETA` fixa outra data. Deixe-a vazia no servidor: ela serve só para reproduzir um dia. Nenhuma das duas mexe na janela do dataset (`DATA_START` e `DATA_END`).
- **Notícias.** Do dia `fim_coleta − 2` até `fim_coleta`. A busca do Google News usa `after:` com `fim_coleta − 2` e `before:` com `fim_coleta + 1`.
- **Preços.** Do `price_start` (`DATA_START` menos `PRICE_WARMUP_DAYS`, padrão 400 dias corridos) até `fim_coleta`, inclusive.
- **Juros e inflação.** CDI e IPCA do ano civil de `fim_coleta`.
- **Fatos relevantes (IPE) e demonstrações (ITR/DFP).** Só o ano civil de `fim_coleta`.

## 3. As nove etapas

Os ids, nomes e coletores são o contrato com o painel. Não renomear sem combinar com ele. O catálogo está em `src/tcc_scraper/orquestrador/etapas.py`.

| # | Id | Nome no painel | Coletor | Fonte | Unidade de trabalho | Retoma por checkpoint? |
|---|---|---|---|---|---|---|
| 1 | `precos` | Preços e índices | `yahoo` | Yahoo Finance, chart API (`adjclose`), e B3 (`GetPortfolioDay`, para IDIV, SMLL e IFIX) | Um ticker: o universo de coleta, `^BVSP`, `BZ=F`, `USDBRL=X`, `IVVB11`; depois, um índice da B3 no ano corrente (`b3\|<indice>\|<ano>`) | Não: refaz todos os tickers a cada dia |
| 2 | `macro` | Macro do BCB | `bcb` e `indices_bcb` | BCB SGS (Selic, série 432; CDI, série 12; IPCA, série 433) e PTAX (`cotacaoVenda`) | Uma série; depois, uma série por ano (`sgs\|<serie>\|<ano>`) | Não |
| 3 | `noticias` | Notícias | `google_news` | Google News RSS, sem chave | Ticker × semana × idioma, na janela da seção 2 | Não |
| 4 | `ipe` | Fatos relevantes | `cvm_ipe` | CVM, IPE (fatos relevantes e comunicados), arquivo anual | Um arquivo do IPE do ano (`ipe\|<ano>\|<Last-Modified>`) | Sim |
| 5 | `enriquecimento` | Corpo e sentimento | `finbert` | Página do veículo (só domínios da allowlist) e FinBERT local | Lote de artigos sem corpo; depois, bloco de textos sem sentimento | Não é preciso: só entram os pendentes |
| 6 | `juiza` | Juíza de qualidade | `juiza` | Regra local, sem API; depois Jev (se ligado); depois Gemini, sem busca na web | Lote de `JUIZA_LOTE` pares (artigo, ticker) pendentes, até `JUIZA_MAX_ITENS_DIA` por dia e até o teto mensal em US$ (`JUIZA_ORCAMENTO_MENSAL_USD`) | Só entram os pendentes |
| 7 | `cadastro` | Cadastro e eventos | `cvm_b3` | CVM (cadastro das companhias), B3 (`GetDetail` e proventos), CVM (ITR e DFP) | Quatro partes: companhia da CVM (`cad\|<Last-Modified>`), empresa da B3 (`detail\|<cd_cvm>`), empresa de proventos (`prov\|<issuing_company>`), arquivo ITR/DFP (`<tipo>\|<ano>\|<Last-Modified>`) | Só ITR/DFP (`resume=True`) |
| 8 | `entrega` | Montagem das 9 tabelas | `build_entrega` | Tabelas `raw_*` e `staging_*` já gravadas | As 9 tabelas da entrega | Refaz as 9 tabelas a cada execução |
| 9 | `analise` | Análise diária | `gemini` | Gemini, com busca Google e saída em JSON | Uma chamada, para o dia `fim_coleta` | Refaz o dia (apaga e regrava) |

Ordem de execução: `precos`, `macro`, `noticias`, `ipe`, `enriquecimento`, `juiza`, `cadastro`, `entrega`, `analise`. As etapas `enriquecimento` e `juiza` vêm antes de `entrega`, então a entrega do dia já sai com as notícias julgadas.

## 4. Tabelas gravadas e regra de falha

Só as 27 tabelas criadas pela pipeline nova (fontes novas, painel e entrega) têm o prefixo `v1-`. As demais mantêm o nome de camada (`raw_prices`, `raw_articles` etc.). Ver [06](06-banco-de-dados.md).

| Etapa | Tabelas que grava | Sai `falhou` quando | Sai `pulada` quando |
|---|---|---|---|
| `precos` | `raw_prices`, `raw_collection_runs` | Foi interrompida (orçamento ou cancelamento), ou todos os tickers falharam | Orçamento esgotado antes de começar, ou cancelamento |
| `macro` | `raw_macro`, `raw_collection_runs` | Alguma parte foi interrompida, ou as duas partes falharam | Orçamento esgotado antes de começar, ou cancelamento |
| `noticias` | `raw_articles`, `staging_article_tickers`, `raw_collection_runs` | Foi interrompida, ou todas as janelas falharam | Orçamento esgotado antes de começar, ou cancelamento |
| `ipe` | `v1-raw_ipe_docs`, `raw_articles`, `staging_article_tickers`, `raw_collection_runs` | Foi interrompida, ou todas as unidades falharam | Orçamento esgotado antes de começar, ou cancelamento |
| `enriquecimento` | `raw_article_bodies`, `raw_articles` (coluna `resolved_url`), `staging_article_sentiment` | Foi interrompida, ou o FinBERT não está instalado (falta o extra `[sentiment]`). O corpo já gravado fica | Orçamento esgotado antes de começar, ou cancelamento |
| `juiza` | `v1-staging_noticias_julgamento`, `v1-raw_llm_juiza_runs` | Foi interrompida (orçamento ou cancelamento) | Orçamento esgotado antes de começar, ou cancelamento. Sem `GEMINI_API_KEY`, a etapa sai `concluida` sem julgar pelo Gemini (ver nota abaixo) |
| `cadastro` | `v1-raw_cvm_cadastro`, `v1-raw_b3_cadastro`, `v1-raw_b3_proventos`, `v1-raw_b3_suplemento`, `v1-raw_cvm_balancos`, `v1-raw_cvm_acoes` | Alguma parte foi interrompida, ou as quatro partes falharam | Orçamento esgotado antes de começar, ou cancelamento |
| `entrega` | As 9 tabelas `v1-noticias` … `v1-indices` e `v1-composicao_ibov` (ver [05](05-entrega.md)) | Alguma das 9 tabelas falhou (a mensagem lista quais), ou nenhuma foi montada | Não é pulada pelo orçamento (ver seção 5) |
| `analise` | `raw_llm_prompts`, `raw_llm_runs`, `staging_llm_noticias_empresas`, `staging_llm_noticias_macro`, `staging_llm_acoes_sinais`, `staging_llm_mercados` | A chamada de análise não devolveu status `ok` | Sem `GEMINI_API_KEY` |

Regras de falha, valem para todas as etapas:

- **Unidade com erro não derruba a etapa.** Ela fica contada em `unidades_erro`, e a última falha aparece em `ultimo_erro_unidade` e `ultimo_erro_mensagem`. A etapa só sai `falhou` se nenhuma unidade deu certo, ou se foi interrompida.
- **Exceção que escapa da etapa** vira `falhou`, com a mensagem `TipoDoErro: texto`. A execução segue para a etapa seguinte.
- **Etapas com várias partes** (`macro`, `cadastro`): uma parte que levanta exceção não impede as outras.
- **Juíza sem chave.** Sem `GEMINI_API_KEY`, a etapa registra um aviso e sai `concluida`, com a regra local aplicada. Os pares que restaram ficam pendentes e entram na próxima execução. A etapa não sai `pulada` nesse caso.

## 5. Orçamento, prazo e status

**Orçamento.** `DAILY_ORCAMENTO_MIN` (padrão 90 minutos) é contado a partir de `iniciada_em` da execução, que é o instante em que ela de fato começa (não o da criação). Há dois efeitos:

- Antes de cada etapa, se o orçamento estourou, a etapa sai `pulada` com o motivo `orçamento esgotado`. A exceção são `entrega` e `analise`, que rodam mesmo assim.
- Durante uma etapa, cada coletor confere o prazo antes de cada unidade. Ao estourar, para e a etapa sai `falhou` com o motivo `interrompida: orçamento ou cancelamento`. As unidades já gravadas ficam no banco.

**Prazo.** `DAILY_PRAZO_HORA` (padrão 20, no fuso `DAILY_TZ`) define `prazo_em` da execução: a hora-alvo para o dia estar publicado. O painel mostra esse campo. O código não cancela nada quando o prazo passa.

**Status da etapa** (`v1-painel_etapas.status`): `pendente`, `em_andamento`, `concluida`, `falhou`, `pulada`. A etapa fica `pendente` até começar. Se a execução for interrompida por reinício do processo, as etapas que ainda não começaram continuam `pendente`.

**Status da execução** (`v1-painel_execucoes.status`):

| Status | Quando |
|---|---|
| `agendada` | Criada, mas ainda não começou. Também usado quando a carga histórica tem prioridade (ver [03](03-carga-historica.md), seção 7) |
| `em_andamento` | Rodando |
| `concluida` | Ao menos uma etapa `concluida` e nenhuma `falhou`. Etapas `pulada` não contam como falha |
| `parcial` | Ao menos uma etapa `concluida` e ao menos uma `falhou` |
| `falhou` | Nenhuma etapa `concluida` (por exemplo, todas `pulada`), ou a execução foi cancelada, ou foi interrompida por reinício do processo |

**Uma execução por vez.** Criar um daily ou uma etapa é recusado enquanto houver outra execução `em_andamento` ou `agendada`.

**Id da execução.** `<data>-<tipo>-<HHMMSS>`, com data e hora da criação no fuso `DAILY_TZ`. Exemplo: `2026-10-19-daily-183000`.

## 6. Logs

- Cada etapa grava uma linha no início e outra no fim em `v1-painel_logs`, com `origem = 'daily'` e a coluna `etapa`.
- Durante a execução, todo aviso ou erro de `tcc_scraper.*` (nível WARNING ou acima) também vai para `v1-painel_logs`.
- `v1-painel_logs` não tem coluna de id da execução. Para ligar uma linha à execução, use o horário e a etapa.
- Os logs são apagados após `PAINEL_LOGS_DIAS` dias (padrão 7).

## 7. Quando uma etapa falha

1. Veja o estado das etapas da execução:

```sql
SELECT etapa_id, status, unidades_feitas, unidades_erro, ultimo_erro_unidade, ultimo_erro_mensagem
FROM `v1-painel_etapas`
WHERE execucao_id = '<id-da-execucao>'
ORDER BY ordem;
```

2. Veja os avisos e erros da etapa:

```sql
SELECT ts, nivel, etapa, mensagem
FROM `v1-painel_logs`
WHERE origem = 'daily' AND etapa = '<id-da-etapa>'
ORDER BY ts DESC
LIMIT 50;
```

3. Rode só a etapa de novo: `python -m tcc_scraper.cli daily --etapa <id>`. Os coletores com checkpoint pulam o que já deu certo. Os demais refazem a coleta inteira (coluna "Retoma por checkpoint" da seção 3). As etapas `entrega` e `analise` regravam o dia.

Nomes com hífen, como `v1-painel_etapas`, precisam de crases em SQL escrito à mão.

## 8. Limitações e pontos em aberto

- **Prazo não é imposto.** `prazo_em` é uma hora-alvo. Uma execução que passa das 20 h continua rodando.
- **Orçamento não corta no meio de uma requisição.** O corte acontece entre unidades. Por isso a execução pode passar um pouco dos 90 minutos.
- **Juíza sem chave Gemini** conclui sem julgar. Quem olha só o status `concluida` não vê que há pares pendentes. O log da etapa mostra o aviso.
- **Análise sem chave** sai `pulada`. A execução pode ficar `concluida` mesmo assim, se as outras etapas deram certo.
- **Uma execução só por processo.** O estado do daily fica em memória. Ver [07](07-executor-e-painel.md), seção 9.
- **A CLI `daily` não respeita a prioridade da carga histórica.** Ver [03](03-carga-historica.md), seção 7.
- **Jev desligado por padrão.** Com `JEV_ENABLED=true` e `JEV_API_KEY`, a própria juíza cria o cliente Jev. Com `JUIZA_JEV_DECIDE=false` (padrão), ele só aceita descartes. Ver [04](04-juiza-e-llm.md).

Ver também: [01 Visão geral](01-visao-geral.md), [03 Carga histórica](03-carga-historica.md), [07 Executor e painel](07-executor-e-painel.md), [ADR](ADR.md).
