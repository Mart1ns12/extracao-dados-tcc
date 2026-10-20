# Carga histórica

Este documento cobre a carga histórica: como ela divide os anos pedidos em segmentos, a ordem dos passos, os modos `pendentes` e `reprocessar_erros`, pausa, retomada e cancelamento, a retomada depois de reinício, a prioridade sobre o daily e os tempos e custos esperados.

## 1. O que é e como se dispara

A carga histórica coleta anos civis inteiros, com os mesmos coletores do daily. Ela pode ser pedida de duas formas:

- **Pelo painel.** O painel chama `POST /historico/disparos` na API, que enfileira o comando `historico`. O executor aplica o comando e roda a carga em uma thread própria, dentro do mesmo processo.
- **Pela CLI.** O comando abaixo dispara a carga e espera o fim. É bloqueante.

```bash
python -m tcc_scraper.cli historico --anos 2025 --anos 2026 --modo pendentes
python -m tcc_scraper.cli historico --anos 2025 --modo reprocessar_erros
```

Uma carga por vez. Enquanto existir uma carga com status `em_andamento`, `pausando`, `pausada` ou `cancelando`, um novo disparo é recusado.

## 2. Faixa de datas e segmentos

- **Faixa.** Vai de `inicio_noticias` até `fim_coleta + 1 dia`. `inicio_noticias` é `DATA_START` menos 31 dias. `fim_coleta` está descrito em [02](02-pipeline-diaria.md), seção 2.
- **Segmento.** Cada ano civil dentro da faixa é um segmento. O primeiro e o último podem ser parciais.
- **Anos fora da faixa** são ignorados. Se nenhum dos anos pedidos cai na faixa, o disparo é recusado com `nenhum dos anos está na janela de coleta`.
- **Ordem.** Do segmento mais recente para o mais antigo. Assim, a entrega parcial já cobre o período recente enquanto o resto carrega.

Exemplo com `--anos 2025 --anos 2026` e `DATA_START=2025-09-01` (valor de `.env.example`), a faixa começa em 2025-08-01. O segmento de 2026 vai de 1º de janeiro de 2026 até hoje (inclusive). O de 2025 vai de 1º de agosto de 2025 até 31 de dezembro de 2025.

## 3. Passos e ordem

Cada passo é uma unidade de trabalho com um coletor e um ano. Os passos globais rodam uma vez, no primeiro ano da lista (o mais recente pedido). Os passos por segmento rodam ano a ano.

| Ordem | Passo (coletor) | Ano | Argumentos | Retoma por checkpoint |
|---|---|---|---|---|
| 1 | `yahoo` | Mais recente | Preços de todos os tickers | Não (`resume=False`): refaz tudo |
| 2 | `bcb` | Mais recente | Selic e PTAX | Sim |
| 3 | `indices_bcb` | Mais recente | CDI e IPCA de todos os anos pedidos | Sim |
| 4 | `cvm_cadastro` | Mais recente | Cadastro da CVM | Sim |
| 5 | `b3_cadastro` | Mais recente | Cadastro da B3 | Sim |
| 6 | `b3_proventos` | Mais recente | Proventos da B3 | Não (`resume=False`): refaz tudo |
| 7 | `cvm_itr_dfp` | Mais recente | ITR e DFP de todos os anos pedidos | Sim |
| 8 | `google_news` | Cada segmento | Do início do segmento até o fim (exclusivo) | Sim |
| 9 | `cvm_ipe` | Cada segmento | Só o ano do segmento | Sim |
| 10 | `enriquecimento` | Cada segmento | Corpo dos artigos | Não (`resume=False`); só entram os pendentes |
| 11 | `finbert` | Cada segmento | Sentimento dos artigos pendentes | Não se aplica (função) |
| 12 | `juiza` | Cada segmento | Até `JUIZA_MAX_ITENS_DIA` pares por segmento | Não se aplica (função) |
| 13 | `build_entrega` | Cada segmento | Monta as 8 tabelas da entrega | Não se aplica (função) |

A carga não segue a ordem do daily: primeiro vêm os passos globais, depois os passos ano a ano. Os coletores são os mesmos do daily (ver [02](02-pipeline-diaria.md), seção 3).

A unidade de progresso é o par (coletor, ano). `v1-painel_historico_progresso` guarda uma linha por par.

## 4. Modos

| Modo | O que faz |
|---|---|
| `pendentes` (padrão) | Roda o que falta. Coletores com `resume=True` pulam as unidades que já têm checkpoint `ok` em `raw_collection_runs`. Coletores com `resume=False` refazem tudo |
| `reprocessar_erros` | Roda só as unidades com erro no checkpoint, nos coletores. `finbert`, `juiza` e `build_entrega` não têm checkpoint: rodam como em `pendentes` |

## 5. Pausar, retomar e cancelar

Os três comandos chegam pelo painel (`POST /historico/pausar`, `/retomar`, `/cancelar`). Cada um vira um comando na fila do executor.

| Comando | Precisa estar em | Vai para | Efeito |
|---|---|---|---|
| `pausar` | `em_andamento` | `pausando`, depois `pausada` | A unidade em curso termina. A carga então fica parada |
| `retomar` | `pausada` | `em_andamento` | Continua no mesmo passo e na mesma unidade |
| `cancelar` | `em_andamento`, `pausando` ou `pausada` | `cancelando`, depois `cancelada`. Vai direto a `cancelada` se a thread não estiver ativa | A unidade em curso termina e a carga para |

- **Ponto de parada.** A pausa e o cancelamento são checados entre unidades (coletores), entre blocos (`finbert`) e entre lotes (`juiza`). Nunca interrompem uma requisição no meio.
- **Transição inválida** é recusada. O executor confirma o comando com `ok: false` e uma mensagem, por exemplo `pausar exige carga em_andamento (atual: pausada)`.
- **Passo pausado no meio.** Ao pausar, o passo fica `pausado` em `status_segmento`. Ao retomar, o passo roda de novo: coletores com checkpoint continuam de onde pararam, e os com `resume=False` começam do zero.
- **Contadores.** Um mesmo objeto acompanha o passo inteiro, inclusive as retomadas. Os contadores não zeram quando a carga é pausada e retomada (ver seção 10 sobre um caso de contagem).

## 6. Retomada depois de reinício

Quando o processo do executor sobe, a carga é reconciliada com o banco:

| Status gravado | O que acontece ao subir |
|---|---|
| `em_andamento` ou `pausando` | Vira `em_andamento`. Uma thread nova começa no passo gravado (`ano_atual`, `coletor_atual`). Os passos anteriores não são refeitos. Log de aviso: `carga <id> retomada após reinício` |
| `cancelando` | Vira `cancelada` |
| `pausada` | Continua `pausada` até um comando `retomar` |

O passo gravado roda de novo a partir do checkpoint, como na seção 5.

## 7. Prioridade sobre o daily

Enquanto a carga está `em_andamento` ou `pausando`, um daily ou uma etapa que chega é criada com `status = agendada` e `aguardando = historico`. Ela começa sozinha quando a carga entra em `pausada`, termina (`concluida`), é cancelada ou falha.

- Em `pausada`, o daily roda normalmente. Só `em_andamento` e `pausando` bloqueiam.
- Uma execução `agendada` conta como execução ativa. Um segundo daily é recusado enquanto ela existir.
- Pelo painel, o disparo nesse caso responde `202`, não `409` (contrato da API, seção "Prioridade").
- A **CLI** (`daily` e `historico`) não passa por essa regra. Ver seção 10.

## 8. Acompanhamento

- **`v1-painel_historico`**: uma linha por carga. O id tem o formato `hist-<AAAAMMDD>-<HHMMSS>`, com a hora da criação em `DAILY_TZ`. Exemplo: `hist-20261019-210512`. Colunas: `id`, `status`, `modo`, `anos` (JSON), `ano_atual`, `coletor_atual`, `unidade_atual`, `iniciada_em`, `finalizada_em`, `itens_por_min`, `erro`, `atualizada_em`.
- **`v1-painel_historico_progresso`**: uma linha por (ano, coletor). Criada com `status_segmento = pendente` no disparo, para o painel mostrar o plano antes de rodar. Valores: `pendente`, `em_andamento`, `pausado`, `concluido`, `falhou`. Colunas: `ano`, `coletor`, `unidades_total`, `unidades_feitas`, `unidades_erro`, `itens`, `status_segmento`, `atualizado_em`.
- **`itens_por_min`** é uma média móvel dos últimos 10 minutos.
- **Logs.** Saem com `origem = 'historico'`. Na coluna `etapa` fica o nome do coletor.

Status da carga: `em_andamento`, `pausando`, `pausada`, `cancelando`, `cancelada`, `concluida`, `falhou`. Uma exceção fora de unidade (por exemplo, o banco fora do ar) leva a carga a `falhou`, com a mensagem em `erro`.

## 9. Tempos e custos esperados

| Item | Estimativa | Origem |
|---|---|---|
| Google News | 6 a 9 horas por ano de notícias | Spec de entrega, seção 9.2. A ADR registra 6 a 9 horas para as 9.010 janelas da v1 |
| CVM, B3, BCB e Yahoo | Minutos para cinco anos | Spec de entrega, seção 9.2 |
| Juíza e análise (Gemini) | Sujeitas ao teto de US$ 5/mês | [04](04-juiza-e-llm.md). A juíza e a análise compartilham o mesmo teto |
| Carga completa | Não medida neste código | Não há medição de uma carga multi-ano |

**Atenção ao custo da juíza.** A juíza roda uma vez por segmento, com até `JUIZA_MAX_ITENS_DIA` itens (padrão 2.000) em cada um. Uma carga de cinco segmentos pode mandar até 10.000 pares ao Gemini numa só execução. O código não confere o teto mensal. Antes de rodar uma carga grande, confira a estimativa de custo da juíza em [04](04-juiza-e-llm.md).

A carga disputa o mesmo banco, o mesmo throttle e as mesmas fontes que o daily (ADR, decisão sobre a carga histórica no executor). Por isso ela não deve rodar ao mesmo tempo que o daily. A regra da seção 7 garante isso no caminho do painel.

## 10. Limitações e pontos em aberto

- **`DATA_START` define a faixa da carga e também outras coisas.** O `.env.example` traz `DATA_START=2025-09-01`, ou seja, cerca de um ano. O contrato da API do painel cita carga de cinco anos, com anos de 2021 a 2026. Para cobrir cinco anos, `DATA_START` teria de recuar. Só que `DATA_START` também é usado pelo BCB, pelo CVM e pela janela do dataset v1 (`build-dataset`). O código não separa essas duas coisas. É uma decisão pendente do autor.
- **Anos aceitos pela API e pelo executor são diferentes.** A API aceita 2021 a 2026. O executor ignora os anos fora da faixa calculada por `DATA_START`, e recusa o comando se nenhum cair nela.
- **Contagem de passos com `resume=False`.** Pelo código de `ProgressoHistorico.plano`, quando um passo com `resume=False` é retomado depois de uma pausa, o total passa a somar as unidades já feitas com todas as unidades de novo. Isso pode contar unidades duas vezes em `unidades_total` e `unidades_feitas`. Não foi testado com uma carga real.
- **`reprocessar_erros` não se aplica** a `finbert`, `juiza` e `build_entrega`. Eles rodam igual a `pendentes`.
- **Uma carga por vez e um processo só.** Não há carga paralela, e o estado da thread fica em memória (ver [07](07-executor-e-painel.md), seção 9).
- **A CLI não respeita a prioridade da carga.** A CLI `daily` roda mesmo com carga `em_andamento` no banco, e os dois disputam o banco e as fontes. A CLI `historico` recusa o disparo se já houver carga ativa no banco, mas não espera um daily em andamento.
- **Exemplos antigos de id.** Alguns documentos mostram `hist-20261019-2105`, sem segundos. O código gera o id com segundos.

Ver também: [02 Pipeline diária](02-pipeline-diaria.md), [04 Juíza e LLM](04-juiza-e-llm.md), [07 Executor e painel](07-executor-e-painel.md), [ADR](ADR.md).
