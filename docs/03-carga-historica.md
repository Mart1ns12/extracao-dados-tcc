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

- **Faixa.** Vai de `inicio_noticias` até `fim_coleta + 1 dia`. No WordPress, a coleta de cada segmento começa 31 dias antes do início dele, para o boilerplate causal (30 dias) já ter aquecimento. O processamento (`processar_wp`) começa no início do segmento. `inicio_noticias` é `DATA_START` menos 31 dias. `fim_coleta` está descrito em [02](02-pipeline-diaria.md), seção 2.
- **Segmento.** Cada ano civil dentro da faixa é um segmento. O primeiro e o último podem ser parciais.
- **Anos fora da faixa** são ignorados. Se nenhum dos anos pedidos cai na faixa, o disparo é recusado com `nenhum dos anos está na janela de coleta`.
- **Ordem.** Do segmento mais recente para o mais antigo. Assim, a entrega parcial já cobre o período recente enquanto o resto carrega.

Exemplo com `--anos 2025 --anos 2026` e `DATA_START=2025-09-01` (valor de `.env.example`), a faixa começa em 2025-08-01. O segmento de 2026 vai de 1º de janeiro de 2026 até hoje (inclusive). O de 2025 vai de 1º de agosto de 2025 até 31 de dezembro de 2025.

## 3. Passos, trilhas e ordem

Cada passo é uma unidade de trabalho com um coletor e um ano. Os passos globais rodam uma vez, no primeiro ano da lista (o mais recente pedido). Os passos por segmento rodam ano a ano, em **duas trilhas que correm ao mesmo tempo**:

- **Trilha Google:** `google_news` e `enriquecimento` de cada segmento.
- **Trilha principal:** os passos globais e, por segmento, `wordpress`, `processar_wp`, `cvm_ipe`, a juíza de WordPress e IPE, `finbert`, a juíza do ano e `build_entrega`.

A trilha Google começa junto com os passos globais. Os passos marcados como "espera o Google" só começam quando a trilha Google termina o último passo do ano (`enriquecimento`). Esse é o **portão do ano**.

| Trilha | Passo (coletor) | Ano | Argumentos | Retoma por checkpoint |
|---|---|---|---|---|
| Principal | `yahoo` | Mais recente | Preços de todos os tickers | Não (`resume=False`): refaz tudo |
| Principal | `b3_indices` | Mais recente | Índices IDIV, SMLL e IFIX de todos os anos pedidos | Sim |
| Principal | `bcb` | Mais recente | Selic e PTAX | Sim |
| Principal | `indices_bcb` | Mais recente | CDI e IPCA de todos os anos pedidos | Sim |
| Principal | `cvm_cadastro` | Mais recente | Cadastro da CVM | Sim |
| Principal | `b3_cadastro` | Mais recente | Cadastro da B3 | Sim |
| Principal | `b3_proventos` | Mais recente | Proventos da B3 | Não (`resume=False`): refaz tudo |
| Principal | `cvm_itr_dfp` | Mais recente | ITR e DFP de todos os anos pedidos | Sim |
| Google | `google_news` | Cada segmento | Do início do segmento até o fim (exclusivo). Com `WP_ENABLED=true`, sem os 3 domínios WordPress. Só os idiomas de `GOOGLE_NEWS_IDIOMAS` (padrão `pt`) | Sim |
| Google | `enriquecimento` | Cada segmento | Corpo dos artigos | Não (`resume=False`); só entram os pendentes |
| Principal | `wordpress` | Cada segmento | Só com `WP_ENABLED=true`. Posts de InfoMoney, Money Times e Exame: `desde` é o início do segmento menos 31 dias (aquecimento do boilerplate) e `ate` é o fim do segmento | Sim (por site e mês) |
| Principal | `processar_wp` | Cada segmento | Só com `WP_ENABLED=true`. Processa do início ao fim do segmento, sem rede | Não se aplica (função) |
| Principal | `cvm_ipe` | Cada segmento | Só o ano do segmento | Sim |
| Principal | `juiza` (id no painel `juiza_wp`) | Cada segmento | Julga só os pares de WordPress e IPE do ano (`origens`), em modo `backlog`. Não espera o Google | Não se aplica (função) |
| Principal | `finbert` | Cada segmento | Sentimento dos artigos do ano que ainda não têm score (`anos`). Espera o portão do ano | Não se aplica (função) |
| Principal | `juiza` | Cada segmento | Julga os pares do ano que faltam, em modo `backlog`: sem teto de itens, limitada pela verba `JUIZA_BACKLOG_VERBA_USD` (US$ 15), cumulativa. Espera o portão do ano | Não se aplica (função) |
| Principal | `build_entrega` | Cada segmento | Monta as 11 tabelas da entrega. Espera o portão do ano | Não se aplica (função) |

A carga não segue a ordem do daily. Os coletores são os mesmos do daily (ver [02](02-pipeline-diaria.md), seção 3), exceto a carteira do IBOV (`b3_carteira_ibov`), que a carga não roda: ela existe só no daily e no `coletar`. A análise diária (`analise`) também não entra na carga.

- **Por que duas trilhas.** O Google News é o passo mais lento (cerca de 7,5 horas por ano de notícias, só pt-BR; ver seção 9). O WordPress e a IPE não dependem do corpo dos artigos do Google. Em sequência, a fila inteira esperava o Google.
- **Por que o portão.** FinBERT e juíza pontuam e julgam pelo texto que já existe no banco. Se rodassem antes do `enriquecimento` do ano, pontuariam pela manchete os artigos cujo corpo ainda não foi processado. Por isso o FinBERT também recebe `anos`: pontua só o ano que chegou, e não o backlog inteiro.
- **Por que a juíza de WordPress e IPE não espera.** Essas origens não dependem do corpo do Google, então podem ser julgadas enquanto o Google coleta o ano. Com `WP_ENABLED=false`, ela fica só com a IPE.
- **Falha da principal.** Se um passo da trilha principal falha, a trilha Google para na unidade em curso e a carga vai a `falhou` (seção 8).
- **Falha da Google.** Se a trilha Google falha (inclusive quando as pausas do bloqueio acabam, seção 3.1), só ela para. A principal segue até o fim. Os passos com portão dos anos que o Google não concluiu não rodam e ficam como estão no painel; o log mostra um `aviso` para cada um. A carga vai a `falhou`, com erro `trilha google: ...` (seção 8).

Uma nova carga refaz o plano, e o checkpoint dos coletores evita refazer o que já está pronto.

A unidade de progresso é o par (ano, id no painel). `v1-painel_historico_progresso` guarda uma linha por par. O id no painel é o nome do coletor, exceto a juíza de WordPress e IPE, que é `juiza_wp`.

### 3.1 Bloqueio do Google News

O Google News bloqueia por IP e por ritmo. O sinal é HTTP 503 ("tráfego incomum") em todas as consultas, ou timeouts seguidos, sem `Retry-After`. Em 2026-10-28, a carga caiu assim: 181 consultas boas em 6 minutos, a 1 a cada 2 s, e depois 10 falhas seguidas. Na mesma manhã, o IP do executor já levava 503 desde a primeira consulta, por pelo menos 40 min. Desta máquina, a mesma URL respondia 200.

Quando `GOOGLE_NEWS_MAX_FALHAS_SEGUIDAS` (padrão 10) unidades seguidas falham, a trilha Google pausa em vez de abortar:

- A pausa dura `GOOGLE_NEWS_PAUSAS_MIN` minutos (padrão `30,60,120`). A pausa é a k-ésima da lista, em que k é o número de pausas seguidas sem sucesso: a 1ª dura 30 min, a 2ª 60 e a 3ª 120. Um sucesso zera a contagem.
- O painel mostra a pausa de duas formas: um log `aviso` e a unidade atual, `pausa por bloqueio ate HH:MM UTC (1/3)`. A pausa para com pausa ou cancelamento da carga em até 5 s.
- Depois da última pausa, sem nenhum sucesso entre elas, a coleta aborta com `... apos 3 pausas`. A trilha Google para, como em qualquer falha dela (seção 3).
- As unidades que falharam ficam com erro no checkpoint. Elas não são refeitas na mesma execução.

Com os valores padrão, uma falha que não passa leva 3,5 horas de pausas (30 + 60 + 120 min) antes de abortar, além do tempo das tentativas.

**Como retomar.** Dispare a carga de novo com `--modo pendentes` (padrão) e os mesmos anos. Os coletores com checkpoint pulam as unidades `ok` e refazem as que falharam. No painel, é o mesmo disparo de `historico`.

## 4. Modos

| Modo | O que faz |
|---|---|
| `pendentes` (padrão) | Roda o que falta. Coletores com `resume=True` pulam as unidades que já têm checkpoint `ok` em `raw_collection_runs`. Coletores com `resume=False` refazem tudo |
| `reprocessar_erros` | Roda só as unidades com erro no checkpoint, nos coletores. `finbert`, `juiza` e `build_entrega` não têm checkpoint: rodam como em `pendentes` |

## 5. Pausar, retomar e cancelar

Os três comandos chegam pelo painel (`POST /historico/pausar`, `/retomar`, `/cancelar`). Cada um vira um comando na fila do executor.

| Comando | Precisa estar em | Vai para | Efeito |
|---|---|---|---|
| `pausar` | `em_andamento` | `pausando`, depois `pausada` | Cada trilha termina a unidade em curso e estaciona. `pausada` só é gravada quando todas as trilhas ainda ativas estão paradas |
| `retomar` | `pausada` | `em_andamento` | Continua no mesmo passo e na mesma unidade |
| `cancelar` | `em_andamento`, `pausando` ou `pausada` | `cancelando`, depois `cancelada`. Vai direto a `cancelada` se a thread não estiver ativa | A unidade em curso termina e a carga para |

- **Ponto de parada.** A pausa e o cancelamento são checados entre unidades (coletores), entre blocos (`finbert`) e entre lotes (`juiza`). Nunca interrompem uma requisição no meio. A espera do portão de um ano também para com pausa e cancelamento. A pausa do Google News (seção 3.1) também para com pausa e cancelamento, em até 5 s. As esperas (do portão e da pausa) são laços com timeout, sem espera infinita. Uma falha da trilha principal aborta a outra no mesmo ponto; uma falha da Google só para a própria trilha (seção 3).
- **Transição inválida** é recusada. O executor confirma o comando com `ok: false` e uma mensagem, por exemplo `pausar exige carga em_andamento (atual: pausada)`.
- **Passo pausado no meio.** Ao pausar, o passo fica `pausado` em `status_segmento`. Ao retomar, o passo roda de novo: coletores com checkpoint continuam de onde pararam, e os com `resume=False` começam do zero.
- **Contadores.** Um mesmo objeto acompanha o passo inteiro, inclusive as retomadas. Os contadores não zeram quando a carga é pausada e retomada (ver seção 10 sobre um caso de contagem).

## 6. Retomada depois de reinício

Quando o processo do executor sobe, a carga é reconciliada com o banco:

| Status gravado | O que acontece ao subir |
|---|---|
| `em_andamento` ou `pausando` | Vira `em_andamento`, e as duas trilhas começam de novo. Os passos com `concluido` em `v1-painel_historico_progresso` não são refeitos. Os demais (`pendente`, `em_andamento`, `pausado`, `falhou`) rodam, cada um respeitando o portão do seu ano. Log de aviso: `carga <id> retomada após reinício` |
| `cancelando` | Vira `cancelada` |
| `pausada` | Continua `pausada` até um comando `retomar` |

A retomada decide pelo status de cada linha de progresso, e não pela posição única gravada em `v1-painel_historico`. Os passos que não estavam `concluido` rodam de novo a partir do checkpoint dos coletores, como na seção 5.

## 7. Prioridade sobre o daily

Enquanto a carga está `em_andamento` ou `pausando`, um daily ou uma etapa que chega é criada com `status = agendada` e `aguardando = historico`. Ela começa sozinha quando a carga entra em `pausada`, termina (`concluida`), é cancelada ou falha.

- Em `pausada`, o daily roda normalmente. Só `em_andamento` e `pausando` bloqueiam.
- Uma execução `agendada` conta como execução ativa. Um segundo daily é recusado enquanto ela existir.
- Pelo painel, o disparo nesse caso responde `202`, não `409` (contrato da API, seção "Prioridade").
- A **CLI** (`daily` e `historico`) não passa por essa regra. Ver seção 10.

## 8. Acompanhamento

- **`v1-painel_historico`**: uma linha por carga. O id tem o formato `hist-<AAAAMMDD>-<HHMMSS>`, com a hora da criação em `DAILY_TZ`. Exemplo: `hist-20261019-210512`. Colunas: `id`, `status`, `modo`, `anos` (JSON), `ano_atual`, `coletor_atual`, `unidade_atual`, `iniciada_em`, `finalizada_em`, `itens_por_min`, `erro`, `atualizada_em`. `ano_atual`, `coletor_atual` e `unidade_atual` são o último passo que gravou posição, de qualquer das duas trilhas. A posição de cada trilha está nas linhas de progresso.
- **`v1-painel_historico_progresso`**: uma linha por (ano, coletor). Criada com `status_segmento = pendente` no disparo, para o painel mostrar o plano antes de rodar. Valores: `pendente`, `em_andamento`, `pausado`, `concluido`, `falhou`. Colunas: `ano`, `coletor`, `unidades_total`, `unidades_feitas`, `unidades_erro`, `itens`, `status_segmento`, `unidade_atual`, `ultimo_erro_unidade`, `ultimo_erro_mensagem`, `atualizado_em`. `unidade_atual` é a unidade em processamento do próprio (ano, coletor), gravada antes de cada unidade; some ao concluir o passo e fica na última unidade se ele pausar ou falhar. `ultimo_erro_unidade` e `ultimo_erro_mensagem` guardam o último erro do passo: de uma unidade, ou da exceção que parou o passo (`Tipo: mensagem`).
- **`itens_por_min`** é uma média móvel dos últimos 10 minutos.
- **Logs.** Saem com `origem = 'historico'`. Na coluna `etapa` fica o nome do coletor.

Status da carga: `em_andamento`, `pausando`, `pausada`, `cancelando`, `cancelada`, `concluida`, `falhou`. Uma exceção fora de unidade (por exemplo, o banco fora do ar), ou a falha de um passo da trilha principal, leva a carga a `falhou`, com a mensagem em `erro`. A falha da trilha Google (seção 3) também leva a `falhou`, com o erro começando por `trilha google: `.

## 9. Tempos e custos esperados

| Item | Estimativa | Origem |
|---|---|---|
| Google News | Cerca de 7,5 horas por ano de notícias, só pt-BR: ~4.500 unidades por ano a ~6 s cada (5 s de throttle e média de 1 s de jitter), sem contar as subdivisões | Conta de 2026-10-28. A estimativa antiga (6 a 9 horas, da spec de entrega, seção 9.2, e da ADR) era de pt-BR e en-US, a 2 s, e não vale mais. Não há carga real neste ritmo |
| CVM, B3, BCB e Yahoo | Minutos para cinco anos | Spec de entrega, seção 9.2 |
| Juíza (Jev e Claude Haiku 5.5) e análise (Gemini) | Juíza do backlog: verba de US$ 15, cumulativa, somando Jev e LLM de fallback. Análise diária: teto de US$ 5/mês no Gemini | [04](04-juiza-e-llm.md), seção 2.5. A juíza diária tem teto próprio de US$ 3/mês |
| WordPress (InfoMoney, Money Times, Exame) | Medido em 2026-03: 11.306 posts de um mês em 122 requisições, sem erro, com throttle de 1,0 s. Por ano, 83,6 a 106,1 mil posts nos 3 sites | Teste real da API, em [09](09-historico-de-mudancas.md) e no [ADR](ADR.md) |
| Carga completa | Não medida neste código | Não há medição de uma carga multi-ano |

**Atenção ao custo da juíza.** Em cada segmento, a juíza roda em modo backlog: sem teto de itens, limitada pela verba de US$ 15 (`JUIZA_BACKLOG_VERBA_USD`), que é cumulativa entre execuções. A checagem é feita antes de cada lote, e o estouro máximo é de um lote. Antes de uma carga grande, confira `judge-news --status` (verba e gasto do backlog) e a estimativa em [04](04-juiza-e-llm.md).

A carga disputa o mesmo banco, o mesmo throttle e as mesmas fontes que o daily (ADR, decisão sobre a carga histórica no executor). Por isso ela não deve rodar ao mesmo tempo que o daily. A regra da seção 7 garante isso no caminho do painel.

## 10. Limitações e pontos em aberto

- **Janela de cinco anos: `DATA_START=2021-02-01`** (decisão do autor, 2026-10-23). O `.env.example` traz esse valor. O default de `config.py` continua `2025-09-01`, então cada ambiente (local e Railway) precisa definir `DATA_START` no `.env` ou nas variáveis do serviço.
  - Com `2021-02-01`, as janelas derivadas ficam: notícias e IPE a partir de 2021-01-01 (`DATA_START − 31 dias`), preços a partir de 2019-12-29 (`DATA_START − 400 dias`) e balanços CVM a partir de 2020.
  - `DATA_START` também move a janela do dataset v1 (`build-dataset`). O código não separa as duas coisas.
  - Comando da carga: `historico --anos 2021 --anos 2022 --anos 2023 --anos 2024 --anos 2025 --anos 2026`. CDI e IPCA só começam em 2021 se a carga incluir `--anos 2021` (`collectors/indices.py` usa 1º de janeiro do ano pedido).
  - O Google News devolve pouco para 2021 e 2022 (cobertura validada só para cerca de 2 anos). A contagem real por ano sai de `scripts/relatorio_qualidade.py`.
- **Anos aceitos pela API e pelo executor são diferentes.** A API aceita 2021 a 2026. O executor ignora os anos fora da faixa calculada por `DATA_START`, e recusa o comando se nenhum cair nela.
- **Contagem de passos com `resume=False`.** Pelo código de `ProgressoHistorico.plano`, quando um passo com `resume=False` é retomado depois de uma pausa, o total passa a somar as unidades já feitas com todas as unidades de novo. Isso pode contar unidades duas vezes em `unidades_total` e `unidades_feitas`. Não foi testado com uma carga real.
- **`reprocessar_erros` não se aplica** a `finbert`, `juiza` e `build_entrega`. Eles rodam igual a `pendentes`.
- **Uma carga por vez e um processo só.** Não há carga paralela, e o estado da thread fica em memória (ver [07](07-executor-e-painel.md), seção 9). As duas trilhas da carga são threads desse mesmo processo.
- **Deadlock entre as trilhas (não medido).** `google_news` (na gravação dos artigos) e `processar_wp` gravam em `raw_articles` ao mesmo tempo. O MySQL pode abortar uma das duas com o erro 1213 (deadlock). Se cair numa unidade do `google_news`, a unidade sai com erro. Se cair no `processar_wp`, o passo falha e a carga vai a `falhou`. Nos dois casos, uma nova carga refaz o que não ficou `ok` ou `concluido`. Não há retry automático para esse erro, e não houve carga real para medir a frequência.
- **Fronteira de ano em UTC.** O filtro `anos` do FinBERT e da juíza usa `published_at` em UTC. A agregação diária em `America/Sao_Paulo` fica para o daily. Por isso, uma notícia publicada perto da virada do ano pode cair no segmento vizinho nesta carga.
- **Verba do backlog gasta na ordem das juízas.** A verba da juíza (US$ 15, cumulativa) é gasta na ordem em que as juízas rodam. A juíza de WordPress e IPE de um ano roda antes de o Google terminar esse ano, então pode consumir a verba antes das notícias do Google daquele ano. Antes de uma carga grande, confira o gasto com `judge-news --status`.
- **A CLI não respeita a prioridade da carga.** A CLI `daily` roda mesmo com carga `em_andamento` no banco, e os dois disputam o banco e as fontes. A CLI `historico` recusa o disparo se já houver carga ativa no banco, mas não espera um daily em andamento.
- **Exemplos antigos de id.** Alguns documentos mostram `hist-20261019-2105`, sem segundos. O código gera o id com segundos.

Ver também: [02 Pipeline diária](02-pipeline-diaria.md), [04 Juíza e LLM](04-juiza-e-llm.md), [07 Executor e painel](07-executor-e-painel.md), [ADR](ADR.md).
