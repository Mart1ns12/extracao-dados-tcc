# Executor e painel

Este documento cobre o executor, o worker que roda o daily e a carga histórica a pedido do painel: o contrato com a API do painel, os comandos e como são recusados, a confirmação, a agenda das 08:00, o heartbeat, as tabelas `v1-painel_*`, as limitações atuais, as variáveis e como rodar e implantar.

## 1. O que é o executor

O executor é um worker que fica em loop. Não é um cron. Ele roda com:

```bash
python -m tcc_scraper.cli executor
```

Esse é o `CMD` da imagem do Railway (ver seção 12). Hoje, porém, o executor roda na máquina local do dono, não no Railway. A cada ciclo, ele:

- busca os comandos pendentes na API do painel, a cada `EXECUTOR_POLL_S` segundos (padrão 5);
- aplica cada comando e confirma o resultado à API (ack);
- cria o daily automático no horário da agenda (`DAILY_HORA`, padrão 08:00 em `DAILY_TZ`);
- inicia a execução agendada, quando a carga histórica deixa.

Em paralelo, uma thread própria envia o heartbeat a cada `EXECUTOR_HEARTBEAT_S` segundos (padrão 15). O daily e a carga histórica rodam, cada um, em uma thread própria. Tudo é um processo só.

O executor exige `PAINEL_API_URL` e `EXECUTOR_TOKEN`. Sem essas duas variáveis, a CLI sai com erro.

## 2. Fluxo

```
painel -> API do painel -> v1-painel_comandos (fila)
                                | POST /api/executor/comandos (a cada EXECUTOR_POLL_S)
                           executor -> daily | etapa | coletar | carga histórica
                                | grava
                           MySQL v1-painel_* -> lido pela API -> painel
```

A API nunca chama o scraper. Ela só enfileira o comando. O executor busca, aplica e confirma. O painel lê o progresso direto das tabelas `v1-painel_*`.

## 3. Contrato com a API do painel

O contrato completo está em `interface-acompanhamento/api/docs/contrato-api.md`. Onde ele e o código divergem, vale o código da API (`api/app/rotas.py`).

Todas as rotas são `POST`, com corpo JSON, e exigem `Authorization: Bearer <EXECUTOR_TOKEN>`. O token precisa ser igual ao configurado na API. Sem o token, a API responde `401`. Se a API não tem o token configurado, responde `503`.

| Rota | Corpo | Resposta | Quem chama |
|---|---|---|---|
| `POST /api/executor/comandos` | `{}` | `200 {"comandos": [{"id", "tipo", "parametros", "criado_em"}]}`: os comandos ainda não confirmados, em ordem | Executor, a cada ciclo |
| `POST /api/executor/comandos/ack` | `{"id": "...", "ok": true, "erro": null}` | `204`. `404` (id desconhecido) e `409` (já confirmado) contam como confirmado | Executor, depois de aplicar o comando |
| `POST /api/executor/heartbeat` | `{"host": "...", "versao": "...", "ocupado_com": null}` | `204` | Executor, a cada `EXECUTOR_HEARTBEAT_S` |

Os comandos chegam ao painel pelas rotas que o próprio painel usa:

| Ação no painel | Rota da API | Comando na fila |
|---|---|---|
| Disparar o daily | `POST /api/disparos` com `{"tipo": "daily"}` | `daily` |
| Disparar uma etapa | `POST /api/disparos` com `{"tipo": "etapa", "etapa": "noticias"}` | `etapa` |
| Coletar sob demanda (verificações) | rota definida pelo painel | `coletar` (seção 4.1) |
| Disparar a carga histórica | `POST /api/historico/disparos` com `{"anos": [2025, 2026], "modo": "pendentes"}` | `historico` |
| Pausar, retomar ou cancelar a carga | `POST /api/historico/pausar` (`/retomar`, `/cancelar`) | `pausar`, `retomar`, `cancelar` |

Em `MODO=live`, essas rotas ainda respondem `501`. Ver seção 9.

## 4. Comandos

O executor aplica cada comando de acordo com o `tipo`:

| `tipo` | `parametros` | Efeito | Recusado (ack com `ok: false`) quando |
|---|---|---|---|
| `daily` | `{}` | Cria e inicia um daily, com origem `manual` | Já existe execução `agendada` ou `em_andamento` |
| `etapa` | `{"etapa": "<id>"}` | Cria e inicia uma etapa, sozinha | Id fora do catálogo (`etapa invalida: ...`), ou execução ativa |
| `coletar` | `{"itens": [{"coletor", "anos" ou "tickers" ou "desde" e "ate" ou "aplicar" e "limite" ou "n" e "semente", "refazer"}], "montar_entrega": [...], "origem_verificacoes": [...]}` | Cria e inicia uma execução `coletar`: uma etapa por item, e mais `build_entrega` se houver `montar_entrega`. Sem orçamento | Parâmetro inválido (lista completa na seção 4.1), ou execução `agendada` ou `em_andamento` |
| `historico` | `{"anos": [2025, 2026], "modo": "pendentes"}` | Dispara a carga histórica | Carga ativa (`em_andamento`, `pausando`, `pausada` ou `cancelando`); `modo` inválido; nenhum dos anos cai na faixa de coleta |
| `pausar` | `{}` | Carga: `em_andamento` vai para `pausando` | A carga não está `em_andamento`, ou não há carga |
| `retomar` | `{}` | Carga: `pausada` vai para `em_andamento` | A carga não está `pausada` |
| `cancelar` | `{}` | Carga: vai para `cancelando`, ou direto para `cancelada` | A carga não está `em_andamento`, `pausando` ou `pausada` |

- **Tipo desconhecido** é recusado com `tipo desconhecido: <tipo>`.
- **Falha interna** ao aplicar um comando é recusada com `falha interna ao aplicar o comando`. A exceção vai para o log do processo.
- **Não há cancelamento do daily pelo painel.** O método `Daily.cancelar()` existe, mas nenhum comando o aciona.
- **Mensagens de recusa** vão no campo `erro` do ack. Exemplo: `pausar exige carga em_andamento (atual: pausada)`.

Regras de execução (vêm de [02](02-pipeline-diaria.md), seção 5, e [03](03-carga-historica.md)):

- Uma execução por vez. Um daily, uma etapa ou um coletar é recusado enquanto houver execução `agendada` ou `em_andamento`.
- Com carga `em_andamento` ou `pausando`, o daily e o coletar nascem `agendada` com `aguardando = historico`, e não são recusados.

### 4.1 Comando `coletar`

O comando `coletar` faz uma coleta avulsa de coletores escolhidos, para verificações do painel, sem disparar a carga histórica inteira. Ele reaproveita o `Daily`: cria uma execução `tipo = coletar` com um plano de etapas montado pelo comando. O catálogo dos coletores fica em `orquestrador/coletores.py`, e é o mesmo usado pela carga histórica.

Parâmetros:

```json
{
  "itens": [
    {"coletor": "cvm_itr_dfp", "anos": [2024, 2025], "refazer": true},
    {"coletor": "google_news", "tickers": ["PETR4"], "desde": "2026-01-01", "ate": "2026-02-01"}
  ],
  "montar_entrega": ["balancos"],
  "origem_verificacoes": ["balancos_contas_novas"]
}
```

- `itens` é obrigatório e não pode estar vazio. Cada item é um coletor do catálogo, com os parâmetros que ele aceita.
- `montar_entrega` é opcional: lista de tabelas da entrega (`COLUNAS` de `db/models_entrega.py`) que a etapa `build_entrega` monta por último.
- `origem_verificacoes` é opcional e só vai para o log, na mensagem `coletar <id>: verificações ...`.
- Chave de topo fora dessas três é recusada, para pegar erro de digitação.

Catálogo de coletores:

| `coletor` (id da etapa) | Parâmetros aceitos | Janela de anos (`anos`) | Sem `refazer` |
|---|---|---|---|
| `yahoo` | `tickers` (só os de `tickers_padrao()`) | - | Refaz tudo (sem checkpoint) |
| `b3_indices` | `anos` | do ano de `price_start` ao ano de `fim_coleta` | Checkpoint |
| `b3_carteira_ibov` | - | - | Refaz tudo (sem checkpoint) |
| `bcb` | - | - | Checkpoint |
| `indices_bcb` | `anos` | do ano de `price_start` ao ano de `fim_coleta` | Checkpoint |
| `cvm_cadastro` | - | - | Checkpoint |
| `b3_cadastro` | - | - | Checkpoint |
| `b3_proventos` | - | - | Refaz tudo (sem checkpoint) |
| `cvm_itr_dfp` | `anos` | do ano de `data_start` menos 1 ao ano de `fim_coleta` | Checkpoint |
| `google_news` | `tickers` (só os de `IBOV_TICKERS`), `desde` e `ate` | `desde` e `ate` em ISO, `ate` exclusivo, dentro de `inicio_noticias` até `fim_coleta` + 1 dia | Checkpoint |
| `wordpress` | `desde` e `ate` | `desde` e `ate` em ISO, `ate` exclusivo. Só InfoMoney, Money Times e Exame (API WordPress) | Checkpoint, por site e mês |
| `processar_wp` | `desde` e `ate` | `desde` e `ate` em ISO, `ate` exclusivo. Sem rede: lê o que o `wordpress` já gravou | Pula o que já foi processado. `refazer` reprocessa |
| `cvm_ipe` | `anos` | do ano de `inicio_noticias` ao ano de `fim_coleta` | Checkpoint |
| `enriquecimento` | - | - | Refaz tudo (sem checkpoint) |
| `finbert` | - | - | Só pendentes. `refazer` não se aplica |
| `juiza` | `anos` | do ano de `inicio_noticias` ao ano de `fim_coleta` | Só pendentes. Limitada por `JUIZA_MAX_ITENS_DIA` e pelo teto mensal em US$. `refazer` não se aplica |
| `build_entrega` | não é item: use `montar_entrega` | - | - |
| `corrigir_horas` | `aplicar` (bool, obrigatório; `false` simula, sem gravar) e `limite` (int ≥ 1, opcional; `null` = sem limite) | - | Sem checkpoint. `refazer` não se aplica |
| `juiza_concordancia` | `n` (int de 10 a 1000, obrigatório) e `semente` (int, opcional, padrão 42; `null` também vale 42) | - | Sem checkpoint. `refazer` não se aplica |

Os dois últimos são ações avulsas do painel (`corrigir_horas`, a mesma função do CLI `fix-news-times`; `juiza_concordancia`, a mesma do CLI `judge-news --concordancia`). Exemplos de item:

```json
{"coletor": "corrigir_horas", "aplicar": true, "limite": 500}
{"coletor": "juiza_concordancia", "n": 200, "semente": 42}
```

Regras:

- **`refazer`.** `true` liga `resume = False` (equivale a `--no-resume`). Sem `refazer`, vale o padrão do catálogo, o mesmo da carga histórica. Em coletor sem checkpoint (`finbert`, `juiza`, `corrigir_horas`, `juiza_concordancia`), `refazer` não se aplica e é recusado.
- **Ordem.** Os itens rodam na ordem enviada. A montagem (`build_entrega`) vem sempre por último.
- **Coletor repetido** nos itens é recusado: a chave da etapa é o id do coletor. Junte os parâmetros num item só.
- **Orçamento.** A execução não tem prazo: `DAILY_ORCAMENTO_MIN` não se aplica, porque itens históricos podem levar horas.
- **Falha de item** não para os demais. O final sai `parcial`, como no daily.
- **Logs** saem com `origem = daily`, para o painel não precisar de mudança.
- **Nomes.** `etapa_id` é o id do coletor (`cvm_itr_dfp`, `google_news`, …), e `build_entrega` para a montagem. Não são as nove etapas do daily.
- **Ações avulsas (`corrigir_horas` e `juiza_concordancia`) ficam fora do daily e da carga histórica.** Não estão em `ETAPAS` nem em `historico._passos`, e não têm checkpoint.
- **`juiza_concordancia` gasta API real** (Jev e Gemini) e grava a run-resumo (`modelo = concordancia`). Não grava julgamento e não tem teto. Sem `JEV_ENABLED` e `JEV_API_KEY`, ou sem `GEMINI_API_KEY`, a etapa falha antes de qualquer chamada, com `concordancia exige o Jev: ...` ou `concordancia exige o Gemini: ...` como erro do item.
- **`corrigir_horas` com `aplicar = false`** roda a fase (b) só para medir (baixa as páginas, com throttle), e não grava nada.
- **Log das ações avulsas.** Cada contagem sai numa linha, `<coletor>: <chave> = <valor>`, com origem `daily`. `corrigir_horas` loga `aplicado` ou `simulação, nada gravado`. `juiza_concordancia` loga as linhas do resumo da medição, incluindo `decisao`. A decisão não altera a config: ligar `JUIZA_JEV_DECIDE` é decisão do dono.
- **`montar_entrega` vale para qualquer plano**, inclusive com ações avulsas, e é anexado no fim como antes. Não há recusa específica para ele.

Recusas (ack com `ok: false` e a mensagem no campo `erro`):

| Caso | Mensagem (início) |
|---|---|
| Chave de topo desconhecida | `parâmetro desconhecido em coletar: ...` |
| Sem `itens`, ou `itens` vazio | `itens vazios: informe ao menos um coletor` |
| Item que não é objeto | `item N: esperado um objeto com 'coletor'` |
| Coletor fora do catálogo | `coletor desconhecido: ...` |
| `build_entrega` como item | `build_entrega não é item de coleta: use montar_entrega` |
| Coletor repetido | `coletor repetido nos itens: ...` |
| Parâmetro que o coletor não aceita | `<coletor> não aceita o parâmetro ...` |
| `refazer` que não é booleano | `<coletor>: refazer deve ser true ou false` |
| `refazer` em coletor sem checkpoint | `<coletor> não tem checkpoint: refazer não se aplica` |
| `anos` que não é lista de inteiros | `<coletor>: anos deve ser uma lista de anos inteiros` |
| Ano fora da janela do coletor | `<coletor>: ano fora da janela A-B: [...]` |
| `tickers` que não é lista de códigos | `<coletor>: tickers deve ser uma lista de códigos` |
| Ticker fora do universo do coletor | `<coletor>: ticker fora do universo: ...` |
| `desde` sem `ate`, ou o contrário | `<coletor>: desde e ate devem vir juntos` |
| Data fora do formato `AAAA-MM-DD` | `<coletor>: datas no formato AAAA-MM-DD` |
| `ate` não posterior a `desde` | `<coletor>: ate deve ser posterior a desde` |
| Período fora da janela de notícias | `<coletor>: período fora da janela A..B (ate exclusivo)` |
| Tabela de entrega inválida | `tabela de entrega inválida: ...` |
| `montar_entrega` que não é lista de tabelas | `montar_entrega deve ser uma lista de tabelas` |
| `corrigir_horas` sem `aplicar` | `corrigir_horas: aplicar é obrigatório (false simula, sem gravar)` |
| `aplicar` que não é booleano | `corrigir_horas: aplicar deve ser true ou false` |
| `limite` que não é inteiro ≥ 1 (nem `null`) | `corrigir_horas: limite deve ser um inteiro maior que zero, ou null` |
| `juiza_concordancia` sem `n` | `juiza_concordancia: n é obrigatório (inteiro de 10 a 1000)` |
| `n` fora de 10 a 1000, ou não inteiro | `juiza_concordancia: n deve ser um inteiro de 10 a 1000` |
| `semente` que não é inteiro | `juiza_concordancia: semente deve ser um inteiro` |
| Execução `agendada` ou `em_andamento` | `ja existe uma execucao em andamento ou agendada` |

Uma recusa não fica na fila: o painel precisa reenviar o comando.

## 5. Confirmação e reentrega

Para cada comando, o executor aplica e depois confirma com `POST /api/executor/comandos/ack`, enviando `ok` e `erro`.

- **Entrega pelo menos uma vez.** O painel pode reenviar um comando que já foi aplicado. O executor guarda o id em `v1-painel_comandos_aplicados`. Se o id já está lá, ele não aplica de novo e confirma com `ok: true`.
- **Recusa reenviada sai como sucesso.** Um comando recusado grava `ok = 0` e a mensagem em `v1-painel_comandos_aplicados`. Se a primeira confirmação se perdeu e o comando volta, a reconfirmação sai com `ok: true`. A recusa original continua registrada no banco, mas o painel recebe o sucesso. Esse é um ponto em aberto (seção 9).
- **Falha de rede no ack.** O executor não tenta de novo no mesmo ciclo. O comando continua pendente no painel e volta na próxima busca, quando é reconfirmado sem ser reaplicado.

## 6. Agenda do daily automático

A cada ciclo, o executor confere a agenda no fuso `DAILY_TZ`:

1. Se a hora atual é igual ou posterior a `DAILY_HORA` (padrão 08:00), e
2. Se não existe execução de origem `cron` iniciada hoje,

então cria e inicia um daily com origem `cron`. O id da execução segue o formato `<data>-daily-<HHMMSS>`.

- **Recusa.** Se a criação é recusada (por exemplo, há um daily manual em andamento), o executor tenta de novo a cada ciclo. Um aviso sai uma vez por dia no log.
- **Subir depois do horário.** Se o executor sobe depois de 08:00 e ainda não houve execução `cron` no dia, o daily dispara logo na subida.
- **Daily manual às 08:00.** Um daily manual rodando nesse horário adia o cron. O cron roda assim que a execução manual termina, no mesmo dia.
- **Carga histórica ativa.** O daily é criado com `status = agendada` e `aguardando = historico`. Ele começa quando a carga sai de `em_andamento` e `pausando` (ver [03](03-carga-historica.md), seção 7).

## 7. Heartbeat

A cada `EXECUTOR_HEARTBEAT_S` segundos, o executor manda o heartbeat para a API e grava o mesmo registro em `v1-painel_heartbeat`. Uma falha em um dos dois não impede o outro.

| Campo | Valor |
|---|---|
| `host` | `EXECUTOR_HOST`, ou o nome da máquina se a variável estiver vazia |
| `versao` | Versão do pacote instalado (`dev` se o pacote não estiver instalado) |
| `ocupado_com` | `historico` enquanto a carga está `em_andamento`, `pausando` ou `cancelando`. `daily` se a thread do daily está ativa, e `coletar` se a thread é a de um comando `coletar`. `null` caso contrário. Uma carga `pausada` não conta como ocupada |

Pelo contrato da API, o componente `executor` aparece como `ok` com sinal de até 45 segundos, `lento` até 120 segundos, e `falha` depois disso.

## 8. Tabelas `v1-painel_*`

As tabelas são lidas pela API direto no MySQL. Só `v1-painel_comandos` é escrita pela API. As demais são escritas pelo executor. Os nomes têm hífen, então em SQL escrito à mão use crases: ``SELECT * FROM `v1-painel_logs` ``.

| Tabela | Escrita por | Chave | Colunas |
|---|---|---|---|
| `v1-painel_comandos` | API (enfileira e confirma) | `id` (CHAR 36) | `tipo`, `parametros` (JSON), `criado_em`, `ack_em`, `ok`, `erro`. Índice em `ack_em` |
| `v1-painel_comandos_aplicados` | Executor | `id` (CHAR 36) | `aplicado_em`, `ok`, `erro`. Garante a idempotência (seção 5) |
| `v1-painel_heartbeat` | Executor | `host` (String 64) | `versao`, `ocupado_com`, `visto_em` |
| `v1-painel_execucoes` | Executor | `id` (String 48) | `tipo` (`daily`, `etapa` ou `coletar`), `etapa_alvo`, `origem` (`cron` ou `manual`), `status`, `aguardando` (`null` ou `historico`), `iniciada_em`, `finalizada_em`, `prazo_em`, `erro`, `atualizada_em`. Índice em `iniciada_em` |
| `v1-painel_etapas` | Executor | (`execucao_id`, `etapa_id`). FK para `v1-painel_execucoes.id` com `ON DELETE CASCADE` | `ordem`, `status`, `unidades_total`, `unidades_feitas`, `unidades_erro`, `unidades_saturadas`, `itens`, `iniciada_em`, `finalizada_em`, `ultimo_erro_unidade`, `ultimo_erro_mensagem`, `atualizada_em` |
| `v1-painel_historico` | Executor | `id` (String 32, ex.: `hist-20261019-210512`) | `status`, `modo`, `anos` (JSON), `ano_atual`, `coletor_atual`, `unidade_atual`, `iniciada_em`, `finalizada_em`, `itens_por_min`, `erro`, `atualizada_em` |
| `v1-painel_historico_progresso` | Executor | (`ano`, `coletor`) | `unidades_total`, `unidades_feitas`, `unidades_erro`, `itens`, `status_segmento`, `unidade_atual`, `ultimo_erro_unidade`, `ultimo_erro_mensagem`, `atualizado_em` |
| `v1-painel_logs` | Executor | `id` (autoincremento) | `ts`, `nivel` (`info`, `aviso` ou `erro`), `origem` (`daily` ou `historico`), `etapa`, `mensagem`. Índice em `ts`. Sem chave estrangeira, para o log sobreviver à limpeza das execuções |

Valores de status, em uso:

- `v1-painel_execucoes.status`: `agendada`, `em_andamento`, `concluida`, `parcial`, `falhou`.
- `v1-painel_etapas.status`: `pendente`, `em_andamento`, `concluida`, `falhou`, `pulada`.
- `v1-painel_historico.status`: `em_andamento`, `pausando`, `pausada`, `cancelando`, `cancelada`, `concluida`, `falhou`.
- `v1-painel_historico_progresso.status_segmento`: `pendente`, `em_andamento`, `pausado`, `concluido`, `falhou`.

Em execuções `coletar`, `etapa_id` é o id do coletor do catálogo (`cvm_itr_dfp`, `google_news`, …) ou `build_entrega`, e não uma das nove etapas do daily (seção 4.1).

Os status são strings, sem `ENUM` no banco. Os valores de ponto flutuante são `Double`. As datas são `UTCDateTime`, gravadas em UTC.

## 9. Limitações e pontos em aberto

- **A API em `MODO=live` não implementa as rotas do executor.** As rotas `/api/executor/*` respondem `501`, porque a fila de comandos e o heartbeat ainda são TODO na fonte do modo live (`interface-acompanhamento/api/app/fila.py` e `api/app/fontes/mysql.py`). Enquanto isso, o executor não recebe comandos da API live. O loop segue rodando, sem aplicar nada.
- **`MODO=mock` simula o executor.** Pelo contrato da API, nesse modo a própria API consome a fila e manda o heartbeat. Não aponte o executor real para uma API em `MODO=mock`: os dois disputariam a mesma fila.
- **Um processo só.** O estado do daily e o da carga histórica ficam em memória. Não rode mais de um executor contra o mesmo banco.
- **Reinício no meio de um daily.** A execução `em_andamento` vira `falhou`, com o motivo `interrompida por reinício do executor`. As etapas que ainda não começaram ficam `pendente`. O daily não é retomado: dispare de novo pelo painel. A carga histórica, ao contrário, é retomada sozinha (ver [03](03-carga-historica.md), seção 6).
- **Sem cancelamento do daily** pelo painel.
- **Sem cancelamento do coletar** pelo painel. `cancelar` é da carga histórica. Um `google_news` sem recorte (~7,5 h por ano de notícias, só pt-BR, mais eventuais pausas por bloqueio) só para com o reinício do executor.
- **Plano do coletar só em memória.** Não há coluna de parâmetros em `v1-painel_execucoes`. No reinício, um coletar `em_andamento` vira `falhou`. Um coletar `agendada` também vira `falhou`, com `plano do coletar perdido no reinício do executor; reenvie o comando`.
- **Carga histórica com daily ou coletar rodando.** `Historico.disparar` não consulta o daily, então hoje é possível iniciar uma carga enquanto um daily ou um coletar roda. A lacuna é anterior ao comando `coletar` e não foi corrigida aqui.
- **Recusa reenviada sai como sucesso.** Ver seção 5.
- **Logs sem id de execução.** `v1-painel_logs` não tem essa coluna. A ligação com a execução é pelo horário e pela etapa.
- **Limpeza de logs só na subida.** `PAINEL_LOGS_DIAS` é aplicado quando o executor sobe. Num processo que roda por semanas, os logs antigos só saem no próximo reinício.
- **CLI sem a prioridade da carga.** Ver [03](03-carga-historica.md), seção 10.

## 10. Variáveis do executor

As demais variáveis (banco, Gemini, juíza, Jev, `EXPORT_DIR`) estão em [08](08-operacao-e-deploy.md) e [04](04-juiza-e-llm.md).

| Variável | Padrão | Para quê |
|---|---|---|
| `PAINEL_API_URL` | vazio (obrigatória para o executor) | Base da API do painel, sem barra no final |
| `EXECUTOR_TOKEN` | vazio (obrigatória para o executor) | Bearer enviado à API. Igual ao configurado na API |
| `EXECUTOR_POLL_S` | 5 | Intervalo entre buscas de comandos, em segundos |
| `EXECUTOR_HEARTBEAT_S` | 15 | Intervalo do heartbeat, em segundos |
| `EXECUTOR_HOST` | vazio (nome da máquina) | Nome deste executor no painel |
| `DAILY_HORA` | `08:00` | Horário-alvo do daily automático, em `HH:MM` no fuso de `DAILY_TZ` |
| `DAILY_TZ` | `America/Sao_Paulo` | Fuso do horário da agenda, do dia de referência e do prazo |
| `DAILY_ORCAMENTO_MIN` | 90 | Orçamento do daily, em minutos (ver [02](02-pipeline-diaria.md), seção 5) |
| `DAILY_PRAZO_HORA` | 20 | Hora-alvo de publicação do dia (`prazo_em`) |
| `DATA_FIM_COLETA` | vazio | Fixa o dia de referência. Deixe vazio no servidor |
| `PAINEL_LOGS_DIAS` | 7 | Dias de retenção de `v1-painel_logs`, aplicados na subida |

## 11. Como rodar localmente

```bash
cp .env.example .env                  # PAINEL_API_URL e EXECUTOR_TOKEN; o banco é o MySQL do .env
pip install -e ".[dev,llm]"           # o extra [sentiment] é opcional localmente
python -m tcc_scraper.cli db-info     # confere a conexão com o banco (só leitura)
python -m tcc_scraper.cli executor    # worker; em outro terminal, a API do painel
python -m tcc_scraper.cli daily --etapa precos   # roda uma etapa sem painel
```

- **Não há banco de desenvolvimento.** O executor local grava no MySQL do `.env`, que é o banco do TCC. Ver [06](06-banco-de-dados.md).
- **Sem o extra `[sentiment]`**, a etapa `enriquecimento` falha e o daily sai `parcial`.
- **Teste ponta a ponta com a API real** não é possível enquanto as rotas do executor estiverem em `501` (seção 9).
- **Alterações de schema** são aplicadas à mão, da máquina local, a partir de `sql/alteracoes/`. O usuário do executor não tem permissão de DDL (ver seção 12).

## 12. Implantação no Railway

Esta seção descreve o deploy no Railway. Hoje o executor roda na máquina local do dono, não no Railway.

- **Serviço.** Um worker criado a partir deste repositório. O `railway.json` manda construir pelo `Dockerfile`. Não há healthcheck, porque o worker não serve HTTP. A política de reinício é `ON_FAILURE`, com até 10 tentativas.
- **Imagem.** `python:3.12-slim`, com torch da roda CPU do PyTorch. Os modelos do FinBERT (pt e en) são baixados no build, em `/app/.hf_cache`, e por isso a subida do serviço não baixa modelo.
- **Variáveis.** Ficam no painel do Railway, nunca no repositório. `EXECUTOR_TOKEN` tem que ser igual ao da API do painel.
- **Alterações de schema.** Não rodam no serviço. Aplique o ALTER de `sql/alteracoes/` à mão, da máquina local, com um usuário que tem DDL e aprovação do dono. Ver [06](06-banco-de-dados.md) e [08](08-operacao-e-deploy.md).
- **Disco efêmero.** Parquet e CSV gerados no contêiner somem no próximo deploy. Para gerar CSV, rode `build-entrega --csv` localmente.
- **Checklist de entrada em produção.** Está em [08](08-operacao-e-deploy.md), na ordem em que deve ser feito.

Ver também: [02 Pipeline diária](02-pipeline-diaria.md), [03 Carga histórica](03-carga-historica.md), [06 Banco de dados](06-banco-de-dados.md), [08 Operação e deploy](08-operacao-e-deploy.md), [ADR](ADR.md).
