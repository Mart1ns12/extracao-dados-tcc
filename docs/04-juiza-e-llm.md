# Juíza de notícias e análise diária por LLM

Este documento cobre a juíza de notícias (julgamento de pares artigo-ticker sem busca na web), com o Jev e a LLM de fallback (OpenAI ou Gemini), e a análise diária v3 (Gemini com busca), com custo, persistência, consultas e versionamento de prompt.

## 1. Duas chamadas de LLM, duas funções

| Camada | Função | Usa busca na web | Grava em | Entra na entrega |
|---|---|---|---|---|
| Juíza | Julga pares (artigo, ticker) já coletados: regra barata, depois Jev, depois LLM de fallback | Não | `v1-raw_llm_juiza_runs`, `v1-staging_noticias_julgamento`, `v1-staging_juiza_tentativas` | Sim, como filtro e como colunas `papel`, `tipo_conteudo`, `evento`, `qualidade` de `v1-noticias` |
| Análise diária v3 | Pesquisa as notícias do dia e produz sinais por ação | Sim (`google_search`) | `raw_llm_prompts`, `raw_llm_runs`, `staging_llm_*` | Não. A montagem da entrega não lê estas tabelas |

A juíza roda na etapa `juiza` do daily (ordem 6), antes da montagem da entrega. A análise roda na etapa `analise` (ordem 9), depois dela. A análise diária usa o Gemini. A juíza usa o Jev e, para o que ele não resolve, a LLM de fallback: OpenAI `gpt-5.6-luna` por padrão (`JUIZA_PROVEDOR=openai`), ou Gemini (`JUIZA_PROVEDOR=gemini`).

## 2. Juíza de notícias

### 2.1 Fluxo por par pendente

1. **Pendentes.** Pares (artigo, ticker) com vínculo em `article_tickers` e sem linha em `v1-staging_noticias_julgamento` para a versão vigente do prompt (`VERSAO` em `llm/juiza_prompt.py`, hoje `1`). Pares que já gastaram `JUIZA_MAX_TENTATIVAS` tentativas (padrão 3) saem da fila. A leitura é paginada, com `JUIZA_PAGINA` pares por página. A ordem depende do modo (seção 2.5): o diário vai do mais recente para o mais antigo; o backlog, do mais antigo para o mais recente.
2. **Regra barata** (seção 2.2). Resolve o caso óbvio sem LLM e grava com `modelo = 'regra'`. A regra não conta para o teto de itens.
3. **Jev** (seção 2.4). Roda se `JEV_ENABLED=true` e houver `JEV_API_KEY`. Recebe o que a regra não resolveu, em lotes de `JEV_LOTE` pares. Conforme `JUIZA_JEV_MODO` (`entrega` por padrão), o Jev decide os pares que tem segurança de decidir; o resto segue para a LLM. Um lote que o Jev não consegue julgar fica pendente e não vai à LLM.
4. **LLM de fallback.** Recebe só o que o Jev não decidiu. Provedor em `JUIZA_PROVEDOR`: OpenAI `gpt-5.6-luna` (padrão) ou Gemini (seção 2.3). Lotes de `JUIZA_LOTE` itens (padrão 50). Uma chamada por lote, sem busca na web, com saída em JSON conforme o schema.
5. **Validação.** A resposta é validada item a item. Um item inválido (enum fora da lista, nota fora de 0 a 1 etc.) vira erro da run e não derruba o lote: os demais itens válidos entram. Só entram os pares pedidos no lote, sem duplicatas. JSON inválido ou resposta sem a lista `itens` derrubam o lote inteiro.
6. **Gravação.** Cada chamada paga (Jev ou LLM) gera uma linha em `v1-raw_llm_juiza_runs`, com `custo_usd` e `finalidade`, inclusive as que falharam. Só os julgamentos aceitos vão para `v1-staging_noticias_julgamento`, na mesma transação da run. Cada par pedido à LLM de fallback e não aceito ganha uma tentativa em `v1-staging_juiza_tentativas` (seção 2.6).
7. **Teto.** Antes de cada lote pago, o teto em US$ é conferido (seção 2.5). Cada par que gasta chamada paga conta uma vez no teto de itens, mesmo que passe pelo Jev e depois pela LLM.

O processo é retomável. Um par sem julgamento para a versão vigente continua pendente na execução seguinte, até esgotar as tentativas. Os pares de um lote que falhou continuam pendentes, com uma tentativa a mais, exceto na falha da API (`erro_api`), que não conta tentativa.

| `status` da run | Significado | Tentativa para os pares não aceitos |
|---|---|---|
| `ok` | Todos os pedidos vieram julgados e válidos | Não se aplica |
| `parcial` | Parte dos itens veio inválida ou faltou. Os válidos foram gravados | Sim |
| `erro_validacao` | Nenhum item válido, ou a resposta não tem a lista `itens` | Sim |
| `erro_json` | A resposta não é JSON válido | Sim |
| `erro_api` | A chamada falhou depois das tentativas de retry | Não |

As runs do Jev usam `ok`, `parcial` ou `erro_api`. Os pares que o Jev não decide não ganham tentativa: seguem para a LLM de fallback.

Sem a chave do provedor escolhido (`OPENAI_API_KEY` ou `GEMINI_API_KEY`), os pares que sobram da regra barata (e do Jev) ficam pendentes e o log registra um aviso. A etapa `juiza` termina como concluída, com zero itens julgados pela LLM. Sem chave de nenhum dos dois, só a primeira página de regra roda. Com `JEV_ENABLED=true` e sem `JEV_API_KEY`, a LLM fica bloqueada (parada `jev_sem_chave`, seção 2.9).

### 2.2 Regra barata (`llm/juiza_regras.py`)

A regra é avaliada nesta ordem. A primeira condição que casar decide o par.

| Ordem | Condição | Resultado (`papel`, `tipo_conteudo`, `evento`, `qualidade`, `confianca`) |
|---|---|---|
| 1 | `origem = 'cvm_ipe'` | `protagonista`, `reportagem`, `fato_relevante`, 1.0, 1.0 |
| 2 | Domínio em `DOMINIOS_BLOQUEADOS` (youtube.com, youtu.be, spotify.com, open.spotify.com, podcasts.apple.com, tiktok.com) | `mencao`, `podcast_video`, `outro`, 0.0, 1.0 |
| 3 | Título casa um dos 9 padrões de `PADROES_PANORAMA` (ex.: "Ibovespa sobe", "agenda do dia", "o que esperar", "maiores altas") | `mencao`, `resumo_mercado`, `macro`, 0.1, 0.9 |
| 4 | Três ou mais empresas distintas do universo no título, fora o próprio ticker | Igual à linha 3 |
| 5 | Nenhuma condição casou | Segue para o Jev e, depois, para a LLM de fallback |

Os nomes casam como palavra inteira, sem acento e sem diferença de maiúscula. Termo com até 5 letras (Vale, Azul, Rumo, Vamos, Light) só casa se estiver com inicial maiúscula no título original, para não confundir o verbo "valer" com a empresa.

### 2.3 LLM de fallback da juíza

O que o Jev não decide vai à LLM. O provedor é escolhido por `JUIZA_PROVEDOR`:

- **`openai` (padrão).** Modelo `gpt-5.6-luna`, pela Responses API, com `reasoning.effort = none` (o menor valor aceito; não existe `minimal`), Structured Outputs com `json_schema` estrito e `store = false`. Timeout de `OPENAI_TIMEOUT_S` (120 s), até `OPENAI_MAX_ATTEMPTS` tentativas (3) e teto de saída de `OPENAI_MAX_OUTPUT_TOKENS` (16.000). Chave em `OPENAI_API_KEY`; a base da API é `OPENAI_BASE_URL`. Um 429 com `insufficient_quota` é a parada `sem_credito`: não há retry.
- **`gemini`.** Método `classificar` do `GeminiClient`, que usa `_chamar_api_sem_busca`: sem `google_search`, com `temperature = 0` e `response_format` JSON com o schema. Falhas transitórias são repetidas com backoff, até `LLM_MAX_ATTEMPTS` tentativas e com timeout `LLM_TIMEOUT_S`. Um 402 (crédito esgotado) é a parada `sem_credito`.

Em qualquer provedor:

- **Modelo:** `JUIZA_MODELO`. Vazio = padrão do provedor (`gpt-5.6-luna` na OpenAI; `gemini-3.1-flash-lite` no Gemini). `JUIZA_MODELO` é independente de `GEMINI_MODEL`, que é da análise diária. Um `JUIZA_MODELO=gemini-...` com `JUIZA_PROVEDOR=openai` gera o aviso `modelo_incompativel`: remova a variável do `.env`.
- **Prompt versionado:** o prompt da OpenAI é registrado em `raw_llm_prompts` com `nome = 'juiza_openai'` e a mesma versão `1` do prompt do Gemini. O schema estrito é diferente, mas a versão do julgamento (`prompt_versao`) não mudou. Por isso os julgamentos feitos com o Gemini continuam válidos.
- **Entrada por item:** `id`, `ticker`, `empresa`, `titulo`, `fonte` e `trecho`. O `trecho` são os primeiros 300 caracteres do corpo, quando o corpo existe (o Jev vê 1.500; ver seção 2.4). O universo, com nome e setor de cada ticker e os apelidos, vai uma vez no cabeçalho do prompt.
- **Saída por item:**

| Campo | Valores permitidos |
|---|---|
| `papel` | `protagonista`, `citada_com_fato`, `mencao`, `errada`, `matriz_estrangeira` |
| `tipo_conteudo` | `reportagem`, `opiniao`, `lista`, `agenda`, `resumo_mercado`, `podcast_video`, `newsletter`, `patrocinado`, `outro` |
| `evento` | `resultado`, `dividendo_jcp`, `fato_relevante`, `fusao_aquisicao`, `guidance`, `recomendacao`, `regulatorio`, `operacional`, `governanca`, `juridico`, `mudanca_ticker`, `macro`, `outro` |
| `tickers_adicionais` | Lista de tickers do universo que a matéria também trata. Tickers fora do universo são descartados |
| `qualidade` | Número de 0 a 1, pela rubrica do prompt |
| `confianca` | Número de 0 a 1 |

- **Template:** `llm/prompts/juiza_v1.txt`, `VERSAO = 1` em `llm/juiza_prompt.py`. O sha256 cobre o template e o schema. `garantir_prompt` grava o prompt do Gemini em `raw_llm_prompts` com `nome = 'juiza'` e recusa a execução se o texto mudar sem subir a versão.

### 2.4 Jev: segunda exceção paga, medida por concordância

O Jev (TypeSafe AI, modelo `jev-1.13.0`) é a segunda exceção paga do projeto, aprovada pelo dono em 2026-10-23. Não há conjunto-ouro: a qualidade do Jev é medida por concordância com a referência (seção 2.7). A decisão está no [ADR](ADR.md), em "Jev medido por concordância, sem conjunto-ouro". O modo de decisão de 2026-10-26 está no ADR "Juíza: Jev decide por entrega segura; fallback OpenAI gpt-5.6-luna (effort none)".

- **Estado.** `JEV_ENABLED=false` no padrão. Com `JEV_ENABLED=true` e `JEV_API_KEY` definida, `julgar_pendentes` cria o cliente sozinho (`JevClient`, em `llm/jev_client.py`). Com `JEV_ENABLED=true` e sem a chave, o Jev não roda, a LLM fica bloqueada e a etapa para com `jev_sem_chave` (seção 2.9). Com `JEV_ENABLED=false`, o aviso `jev_desligado` aparece no `--status`.
- **Chamada.** Uma requisição `POST /v1/systemone` por par (a API não aceita lote). As requisições do lote correm em paralelo (`JEV_CONCORRENCIA`, padrão 8), com retry em falhas transitórias e timeout de `JEV_TIMEOUT_S` (30 s). O estado enviado traz empresa, apelidos, fonte, título e trecho. O trecho tem `JEV_TRECHO` caracteres do corpo (padrão 1.500, faixa de 300 a 8.000). A LLM de fallback continua com 300.
- **Perguntas.** `papel`, `tipo_conteudo` e `evento` são escolhas com os rótulos do schema da juíza. `qualidade` é uma escala de 5 níveis, convertida para `(nível − 1) / 4`. O Jev não devolve `tickers_adicionais`, que sai sempre vazio. Esse dado se perde nos pares que o Jev decide (seção 4). Teste de 2026-10-26: com só `papel` e `tipo_conteudo`, o prompt caiu de 1.155 para 725 tokens por par (−37%). Os rótulos coincidiram em 47 de 50, e a decisão de entrega mudou em 1 de 50. O dono decidiu manter as 4 perguntas.
- **Versão fixa.** Modelo `jev-1.13.0`, nunca `latest`. As perguntas são versionadas em `JEV_PERGUNTAS_VERSAO` (hoje `1`): mudou texto ou critério, sobe o número. A staging registra o modelo como `jev-1.13.0/q1/t1500`, em que `t1500` é o `JEV_TRECHO` usado.
- **Confiança.** Nos modos `triagem` e `decide`, a confiança do julgamento é a menor entre a de `papel` e a de `tipo_conteudo`. No modo `entrega`, é a probabilidade do lado decidido (seção 2.4.1).
- **Preço.** US$ 0,042 por milhão de tokens de entrada. A saída é grátis. Medido em 2026-10-26, com 50 pares já julgados pelo Gemini (nada gravado): cerca de 1.155 tokens de entrada por par com as 4 perguntas (as descrições dos critérios contam como entrada), ou cerca de US$ 0,000049 por par. Os 50 pares foram respondidos em 3,7 s. As `probabilities` vieram só com rótulos do enum e somaram cerca de 1.
- **Modo.** `JUIZA_JEV_MODO`, com três valores. O padrão é `entrega`.
  - `entrega` (padrão): o Jev tem a palavra final quando está seguro de que o par entra ou não entra no dataset. Seção 2.4.1.
  - `triagem`: o Jev só aceita **descarte**. Aceita se a confiança for pelo menos `JUIZA_CONFIANCA_TRIAGEM` (0,9) e a regra de entrega reprovar o par. O resto vai à LLM.
  - `decide`: aceita o julgamento se a confiança for pelo menos `JUIZA_CONFIANCA_MINIMA` (0,5). O que ele não aceita vai à LLM.
  - `JUIZA_JEV_DECIDE` está obsoleta. Só vale se `JUIZA_JEV_MODO` não foi definido (`true` equivale a `decide`).
- **Falha.** Um lote do Jev que não consegue julgar fica pendente e **não** vai à LLM. A run fica como `erro_api`, com o modelo do Jev. Se o lote inteiro falhar 2 vezes seguidas, ou se todos os itens voltarem com 401, 402 ou 403, a etapa para com `erro_jev` (seção 2.9). Um par que o Jev não decide e não falha segue para a LLM de fallback, sem tentativa.
- **Custo.** O custo do Jev entra nas mesmas somas da juíza (seção 2.5). Não tem teto próprio.

#### 2.4.1 Modo `entrega`: entrega segura

Regra de entrega: o par entra na entrega se `tipo_conteudo == reportagem` e `papel` está em {`protagonista`, `citada_com_fato`}. O Jev decide "entra" ou "não entra" mesmo com dúvida em `evento` ou em `qualidade`, desde que esteja seguro sobre a regra acima.

A partir das `probabilities` que o Jev devolve, para `papel` e para `tipo_conteudo`:

- Uma probabilidade só é válida se os valores estão em [0, 1] e a soma fica entre 0,95 e 1,05. Chaves fora do enum não contam para nenhum lado.
- `a` = massa dos papéis que entregam (`protagonista`, `citada_com_fato`). `b` = massa dos demais papéis do enum.
- `c` = P(`reportagem`). `d` = massa dos demais tipos do enum.
- `p_entra = a · c`.
- `p_nao_entra = b + d − b · d`.
- Sem `probabilities` válidas, usa a `confidence` do rótulo escolhido.

Decisão:

- **Entra**: `p_entra >= JUIZA_JEV_LIMIAR_ENTREGA` (padrão 0,9) **e** os rótulos escolhidos também entram.
- **Não entra**: `p_nao_entra >= JUIZA_JEV_LIMIAR_ENTREGA` **e** os rótulos escolhidos não entram.
- Senão, o par vai à LLM de fallback.

A `confianca` gravada é a probabilidade do lado decidido. O limiar padrão, 0,9, foi medido em 2026-10-26 com `judge-news --concordancia 500` e é o limiar recomendado (seção 2.7). Não há mudança no `.env`.

### 2.5 Limites e custo

| Limite | Valor padrão | Onde atua |
|---|---|---|
| `JUIZA_MAX_ITENS_DIA` | 2.000 | Teto de pares pagos na etapa `juiza` do daily. Não é um contador por dia civil |
| `JUIZA_ORCAMENTO_MENSAL_USD` | 3,0 | Teto em US$ da juíza diária: soma das runs `producao` (Jev e LLM de fallback) desde o dia 1 do mês (fuso `DAILY_TZ`) |
| `JUIZA_BACKLOG_VERBA_USD` | 15,0 | Verba única do backlog: soma das runs `backlog` (Jev e LLM de fallback), de todas as execuções. Vale para a carga histórica e para `judge-news --backlog` |
| `--orcamento-usd` (`judge-news --backlog`) | Sem valor | Teto extra da rodada. Vale o menor entre ele e a verba que sobra |
| `--limite` (`judge-news`) | Sem valor | Teto de itens da rodada. No backlog, sem valor, não há teto de itens: vale só a verba |
| `JUIZA_MAX_TENTATIVAS` | 3 | Tentativas por par antes de sair da fila (seção 2.6) |
| `JUIZA_PAGINA` | 5.000 | Pares lidos por página, com cursor |
| `JUIZA_LOTE` | 50 | Itens por chamada à LLM de fallback |
| `JEV_LOTE` | 50 | Pares por lote enviado ao Jev (a API recebe um par por requisição) |

- **Teto em US$.** Antes de cada lote pago, o custo já gasto mais o custo do lote anterior é comparado com o teto. Se ultrapassar, a execução para com `parada=orcamento_usd`. O estouro máximo é de um lote.
- **Diário.** O teto é `JUIZA_ORCAMENTO_MENSAL_USD` menos o gasto de `producao` no mês. Não se reinicia a cada execução.
- **Backlog e carga histórica.** A carga histórica roda a juíza em modo `backlog`, uma vez por ano, sem teto de itens. O que limita é a verba de US$ 15, cumulativa entre execuções. A verba não entra no teto mensal do diário.
- **Teto separado da análise.** A análise diária tem teto de US$ 5/mês no Gemini (seção 3.6). A juíza não usa esse teto: os tetos dela cobrem o Jev e a LLM de fallback, seja OpenAI ou Gemini. A juíza diária usa até US$ 3/mês, e o backlog tem verba de US$ 15.
- **Concordância não tem teto.** O `judge-news --concordancia` não consulta nenhum teto e não chama a LLM: só o Jev é chamado. O custo aparece no resumo (seção 2.7).
- **Preço da LLM de fallback (OpenAI).** `gpt-5.6-luna`, preço oficial de 2026-10-26: US$ 0,20 por milhão de tokens de entrada, US$ 0,02 de entrada em cache e US$ 1,20 de saída. Com `JUIZA_PRECO_*` vazios, vale a tabela do provedor. O raciocínio do `effort none` já está em `output_tokens` e não é somado de novo. O custo cobra o cache à parte (`custo_llm`). Medido em 2026-10-26, em um lote de 50 pares: schema estrito aceito de primeira, 50 de 50 itens válidos, `reasoning_tokens = 0`, cerca de 5.976 tokens de entrada e 2.750 de saída. Sem cache, isso dá cerca de US$ 0,00009 por par. Numa repetição do mesmo prompt, o cache cobriu 5.973 tokens (cerca de US$ 0,00007 por par). Em produção o cache cobre só o prefixo fixo, então o custo real fica entre os dois. Latência de cerca de 18 s por lote. Concordância com o Gemini nos mesmos 50: entrega 44 de 50, papel 34 de 50, tipo 36 de 50.
- **Preço da LLM de fallback (Gemini).** Se `JUIZA_PROVEDOR=gemini`: `gemini-3.1-flash-lite`, conforme a tabela da seção 3.6 (verificada em 2026-10-15): US$ 0,25 por milhão de tokens de entrada e US$ 1,50 de saída. O pensamento, quando o modelo informa, é cobrado como saída.
- **Batch API: rejeitada.** Vale para o Gemini e para a OpenAI. O desconto seria de 50%, mas o job assíncrono pode levar até 24 h, o que não cabe no daily. Ver o ADR "Juíza: Jev decide por entrega segura; fallback OpenAI gpt-5.6-luna (effort none)".
- **Custo por par.** Medido em 2026-10-26: Jev cerca de US$ 0,000049 por par (seção 2.4); Luna entre US$ 0,00007 e US$ 0,00009 por par (acima). Projeção com Jev em todos os pares e Luna nos cerca de 31% que o Jev não decide: cerca de US$ 0,00008 por par. Os 13.144 pares do backlog de 24/10 sairiam por cerca de US$ 0,97, contra US$ 2,24 pagos ao Gemini. Com esses preços, Jev mais Luna custa quase o mesmo que a Luna sozinha (cerca de US$ 0,90 a 1,18 no backlog). A economia vem sobretudo de a Luna ser mais barata que o Gemini. O custo real do backlog ainda não foi medido: ele sai de `v1-raw_llm_juiza_runs`.
- **Paradas.** As paradas da juíza (`jev_sem_chave`, `erro_jev`, `sem_credito`, `orcamento_usd`, teto de itens) estão na seção 2.9. Parada por orçamento ou por teto de itens não é falha. As paradas `jev_sem_chave`, `erro_jev` e `sem_credito` deixam a etapa `falhou` e o daily `parcial`. Com lotes de LLM em erro (`erro_json`, `erro_validacao`, `erro_api`), a etapa segue `concluida`, com os erros no campo `erros` dos detalhes.

### 2.6 Persistência

**`v1-raw_llm_juiza_runs`** (append-only, uma linha por chamada paga e por run-resumo da concordância)

| Coluna | Conteúdo |
|---|---|
| `id` | Chave primária |
| `prompt_id` | FK para `raw_llm_prompts` |
| `modelo`, `n_itens` | Modelo usado (`jev-1.13.0/q1/t1500` no Jev, onde `t1500` é o `JEV_TRECHO`; `gpt-5.6-luna` na OpenAI; o nome do modelo no Gemini; `concordancia` na run-resumo) e quantidade de itens do lote |
| `tokens_entrada`, `tokens_saida`, `latencia_ms` | Medidas da chamada. O Jev não informa tokens de saída |
| `custo_usd` | Custo da chamada em US$, calculado pelo código. `NULL` na run-resumo da concordância |
| `finalidade` | `producao` (etapa diária), `backlog` (carga histórica e `--backlog`) ou `concordancia` (medição, sem teto). Padrão `producao` |
| `status`, `erro` | Ver a tabela de status na seção 2.1 |
| `resposta_bruta` | Texto devolvido pelo modelo, para auditoria |
| `iniciado_em`, `finalizado_em` | Início e fim da chamada, em UTC |

Os tetos em US$ são somas de `custo_usd` por `finalidade`, com o índice `ix_raw_llm_juiza_runs_finalidade` (`finalidade`, `iniciado_em`).

**`v1-staging_noticias_julgamento`** (um julgamento por par e versão)

| Coluna | Conteúdo |
|---|---|
| `article_id`, `ticker`, `prompt_versao` | Chave primária. `article_id` referencia `raw_articles` |
| `run_id` | FK para `v1-raw_llm_juiza_runs`. Vazio quando o julgamento veio da regra barata |
| `modelo` | `'regra'`, o nome do modelo da LLM de fallback (`gpt-5.6-luna` ou o modelo do Gemini), `jev-1.13.0/q1/t1500` quando o Jev decidiu, ou `codigo/r1/<camada>` quando o classificador por código decidiu (ver a nota abaixo) |
| `papel`, `tipo_conteudo`, `evento` | Valores da seção 2.3 |
| `tickers_adicionais` | JSON com a lista de tickers do universo |
| `qualidade`, `confianca` | `Double` |
| `julgado_em` | Momento do julgamento, em UTC |

**Julgamento por código (notícias WordPress).** `llm/juiza_codigo.py` decide, sem LLM, os pares de InfoMoney, Money Times e Exame que a evidência do casador resolve. Quem grava é o `processar_wp`, dentro da etapa `noticias`, e não a juíza: `run_id` fica vazio e `modelo` vale `codigo/r1/<camada>`, com as camadas K1, K2, K3 e K6 (`CAMADAS_ATIVAS`). Os pares das camadas R, T, K4 e K7 não têm julgamento de código e seguem como pendentes para a juíza. A regra de entrega não muda. Os números medidos estão em [09](09-historico-de-mudancas.md) e no [ADR](ADR.md).

A gravação usa `bulk_upsert` sem colunas para atualizar. Se já existe linha para (artigo, ticker, versão), a linha antiga é mantida. Uma nova versão do prompt cria linhas novas, e por isso os pares voltam a ser pendentes quando `VERSAO` sobe.

**`v1-staging_juiza_tentativas`** (uma linha por par que a LLM de fallback não aceitou, por versão)

| Coluna | Conteúdo |
|---|---|
| `article_id`, `ticker`, `prompt_versao` | Chave primária. `article_id` referencia `raw_articles` (cascata na exclusão) |
| `tentativas` | Número de tentativas feitas. Ao chegar a `JUIZA_MAX_TENTATIVAS`, o par sai da fila |
| `ultimo_status`, `ultimo_erro` | Status e erro da última tentativa |
| `atualizado_em` | Momento da última tentativa, em UTC |

A tabela existe só para contar tentativas. Em `v1-staging_noticias_julgamento`, "linha existe" continua querendo dizer "julgado". O contador é calculado no código, a partir do valor lido, e gravado com `bulk_upsert`.

### 2.7 Concordância Jev × referência (`judge-news --concordancia`)

Substitui o conjunto-ouro, que foi removido em 2026-10-23. Não há rótulo feito por pessoa: a medição compara o Jev com a referência, que são os julgamentos já gravados de um modelo de referência.

- **Comando:** `python -m tcc_scraper.cli judge-news --concordancia N [--semente S] [--referencia MODELO]`. Semente padrão: 42. Modelo de referência padrão: `gemini-3.1-flash-lite`. Exige `JEV_ENABLED=true` e `JEV_API_KEY`. Não exige chave da LLM, porque a LLM não é chamada. Sem elas, falha antes de qualquer chamada.
- **Amostra.** N pares (artigo, ticker), sorteados por estrato (ano de publicação × fonte). As 10 fontes mais frequentes ficam em estratos próprios; as demais caem no estrato `outras`. A alocação é proporcional ao tamanho do estrato, com pelo menos 1 par por estrato não vazio, ajustada pelo maior resto. Pares que a regra barata resolve são descartados e contados, mas não entram. Artigos do IPE (`cvm_ipe`) ficam fora.
- **Reprodutível.** A ordem dentro de cada estrato é `crc32(id|ticker|semente)`. A mesma semente gera a mesma amostra.
- **Julgamento.** Só o Jev é chamado, em lotes de `JEV_LOTE`. A referência não é chamada: o comando lê os julgamentos já gravados do modelo de referência em `v1-staging_noticias_julgamento`.
- **O que não grava.** Nada vai para `v1-staging_noticias_julgamento` nem para `v1-staging_juiza_tentativas`, e nenhum teto é consultado. Cada chamada paga ao Jev vira uma run com `finalidade='concordancia'` e `custo_usd` preenchido. A run-resumo tem `modelo='concordancia'`, `custo_usd` nulo e, em `resposta_bruta`, o JSON com semente, alocação, modelos, métricas e pares. Para N = 500, o custo foi de US$ 0,0245 (medido em 2026-10-26).
- **Métricas antigas.** Para `papel`, `tipo_conteudo` e `entrega` (se o par seria aprovado para a entrega com o filtro da juíza): concordância (fração de pares com o mesmo rótulo), kappa de Cohen e n.
- **Bloco `entrega_segura`.** Para cada limiar L em {0,80; 0,85; 0,90; 0,95}: cobertura (fração de pares que o Jev decide com L), concordância e kappa nos decididos, `n_decididos`, precisão por direção (entra e não entra), falsos, `perda_tickers_adicionais` e a contagem de `prob_fonte` (de onde veio a probabilidade usada, seção 2.4.1).
- **Limiar recomendado.** `limiar_recomendado` é o menor L com `n_decididos >= 100`, concordância >= 0,95 e precisão >= 0,90 em cada direção que tenha pelo menos 30 decididos.
- **Decisão.** O campo `decisao` é `entrega` (quando há limiar recomendado) ou `triagem`.
- **Quem decide.** O comando só imprime as métricas e a decisão, e não altera a configuração. Quem ajusta `JUIZA_JEV_LIMIAR_ENTREGA` e `JUIZA_JEV_MODO` é o dono.
- **Limitação.** Concordância não é acurácia. Dois modelos que erram do mesmo jeito concordam. Sem rótulo humano, não há precisão nem revocação por rótulo. A "precisão por direção" mede a concordância com a referência, não a verdade. O número para o TCC é a concordância, com essa ressalva.
- **Limitação de comparação.** A referência viu 300 caracteres do corpo e o Jev vê 1.500. Parte da discordância pode ser acerto do Jev, e isso deve constar no TCC.
- **Resultado medido (2026-10-26).** `judge-news --concordancia 500`, semente 42, 21 estratos, só o Jev, custo de US$ 0,0245. Decisão: `entrega`, com `limiar_recomendado` = 0,9. O limiar é o menor L com `n_decididos >= 100`, concordância >= 0,95 e precisão >= 0,90 em cada direção. Por limiar:

| L | Cobertura | Concordância | Precisão entra | Precisão não entra |
|---|---|---|---|---|
| 0,80 | 78,4% | 92,4% | 94,8% | 89,6% |
| 0,85 | 74,6% | 94,1% | 95,0% | 93,1% |
| **0,90** | **68,8%** | **95,9%** | **96,7%** | **95,0%** |
| 0,95 | 57,0% | 96,1% | 97,4% | 94,8% |

Em L = 0,90, são 344 pares decididos, com kappa de 0,918. As métricas antigas (`papel`, `tipo_conteudo`, `entrega`) deram concordância de 64,2% (kappa 0,446), 75,6% (kappa 0,423) e 85,2%. Por isso o modo `decide` segue reprovado (`decisao_decide = llm_decide`). Em 97 dos 344 pares decididos (28%), o Gemini tinha tickers adicionais que se perdem quando o Jev decide (`perda_tickers_adicionais`). Passo a passo: [08-operacao-e-deploy.md](08-operacao-e-deploy.md), seção 5.2, passo 6.

### 2.8 Consultas de leitura

Estado da última execução por status:

```sql
SELECT status, COUNT(*) AS runs
  FROM `v1-raw_llm_juiza_runs`
 GROUP BY status;
```

Julgamentos por papel na versão vigente do prompt:

```sql
SELECT papel, COUNT(*) AS pares
  FROM `v1-staging_noticias_julgamento`
 WHERE prompt_versao = 1
 GROUP BY papel;
```

Quantos julgamentos vieram da regra, do Jev e da LLM de fallback (ou do Gemini, em julgamentos antigos):

```sql
SELECT modelo, COUNT(*) AS pares
  FROM `v1-staging_noticias_julgamento`
 GROUP BY modelo;
```

Gasto da juíza por finalidade (os tetos em US$ usam essas somas):

```sql
SELECT finalidade, ROUND(SUM(custo_usd), 4) AS usd
  FROM `v1-raw_llm_juiza_runs`
 GROUP BY finalidade;
```

Nomes de tabela com hífen exigem crase no MySQL, como nos exemplos acima.

### 2.9 Paradas e avisos

Paradas da juíza:

| Parada | Quando | Efeito |
|---|---|---|
| `orcamento_usd` | Teto em US$ atingido antes de um lote pago (seção 2.5) | Não é falha. Etapa `concluida` |
| `jev_sem_chave` | `JEV_ENABLED=true` sem `JEV_API_KEY`. Só a regra roda, e a LLM fica bloqueada | Etapa `falhou`; daily `parcial` |
| `erro_jev` | Lote do Jev falhou 2 vezes seguidas, ou todos os itens voltaram com 401, 402 ou 403. O lote fica pendente e não vai à LLM | Etapa `falhou`; daily `parcial` |
| `sem_credito` | OpenAI com 429 `insufficient_quota`, ou Gemini com 402. Sem retry | Etapa `falhou`; daily `parcial` |

Avisos (`avisos_config`, no log da etapa e no `judge-news --status`):

| Aviso | Quando |
|---|---|
| `jev_desligado` | `JEV_ENABLED=false`. O Jev não roda e não há erro |
| `jev_sem_chave` | `JEV_ENABLED=true` sem `JEV_API_KEY` (também é parada, acima) |
| `openai_sem_chave` ou `gemini_sem_chave` | Falta a chave do provedor escolhido em `JUIZA_PROVEDOR`. Os pares que sobram ficam pendentes |
| `modelo_incompativel` | `JUIZA_MODELO=gemini-...` com `JUIZA_PROVEDOR=openai`. O erro é claro. Remova `JUIZA_MODELO` do `.env` |

`judge-news --status` mostra: se o Jev está ligado ou desligado, se a chave do Jev está presente ou AUSENTE, o modo (`JUIZA_JEV_MODO`), o limiar (`JUIZA_JEV_LIMIAR_ENTREGA`), o trecho (`JEV_TRECHO`), o provedor, o modelo, se a chave da LLM está presente, e os avisos.

## 3. Análise diária v3 (Gemini com busca)

### 3.1 O que é e o que não é

**É** uma camada independente do scraper. Uma vez por dia, a etapa `analise` chama o Gemini com `google_search` e grava o resultado de forma estruturada:

- notícias recentes por empresa do IBOV;
- notícias macro, com escopo global ou brasileiro e as commodities afetadas;
- sinais por ação, com direção esperada e confiança;
- mercados em alta e o motivo.

**Não é:**

- uma alteração do dataset v1. `features_dataset_v1` fica intacta;
- uma fonte que alimenta `raw_articles`. A própria LLM pesquisa;
- um loop. O processo roda uma vez, grava e encerra;
- reprodutível bit a bit. A busca muda de um dia para outro. Por isso o JSON bruto, o prompt renderizado, as buscas e as citações ficam guardados.

### 3.2 Tabelas

| Tabela | Conteúdo | Chave | Regra de gravação |
|---|---|---|---|
| `raw_llm_prompts` | Template versionado, schema e sha256 | (`nome`, `versao`) | Só insere |
| `raw_llm_runs` | Uma linha por tentativa, inclusive erro: prompt renderizado, resposta bruta, JSON, buscas, citações, tokens, latência, `status`, `erro` | `id` | Append-only |
| `staging_llm_noticias_empresas` | Notícias por empresa, com `fonte`, `url`, `sentimento`, `relevancia` | (`data_referencia`, `ticker`, `item_hash`) | Substituída por dia |
| `staging_llm_noticias_macro` | Notícias macro, com `escopo`, `commodities_afetadas`, `direcao_commodity`, `setores_afetados` | (`data_referencia`, `item_hash`) | Substituída por dia |
| `staging_llm_acoes_sinais` | Por ação: `direcao_esperada` (alta, baixa, neutra), `confianca`, `motivo`, `setor`, `noticias_relacionadas` | (`data_referencia`, `ticker`) | Substituída por dia |
| `staging_llm_mercados` | Mercados em alta: `tipo` (indice, commodity, moeda, setor), `variacao_pct`, `motivo` | (`data_referencia`, `nome`) | Substituída por dia |

Estas tabelas mantêm o nome original, sem o prefixo `v1-`. Foram criadas nas etapas 0001 a 0003 de `sql/schema.mysql.sql`, e o RENAME da etapa 0008 só alcança as tabelas criadas a partir da 0004 (ver [06-banco-de-dados.md](06-banco-de-dados.md)).

A gravação do dia (`_gravar_staging` em `llm/job.py`) apaga as linhas da `data_referencia` e grava as novas na mesma sessão.

### 3.3 Execução

A etapa `analise` do daily segue estes passos:

1. Com `ANALISE_ENABLED=false` (padrão desde 2026-10-26: o Gemini está sem crédito e a análise vai migrar para a OpenAI), a etapa sai como `pulada`, sem chamada. O comando `analyze-llm` continua rodando à mão.
2. Se não houver `GEMINI_API_KEY`, a etapa sai como `pulada`, sem chamada.
3. A data de referência é hoje no fuso `LLM_TZ` (padrão `America/Sao_Paulo`).
4. O prompt é renderizado e registrado em `raw_llm_prompts` (`garantir_prompt`).
5. O Gemini é chamado com busca e schema JSON.
6. A resposta passa por `parse_resposta` e pelo filtro do universo. Os links de redirecionamento do Google (`vertexaisearch`) são trocados pela URL real por `resolver_redirect_http`. Quando a resolução falha, o link original fica.
7. A run é gravada em `raw_llm_runs`, com o status.
8. Se o status for `ok`, as staging do dia são substituídas.

Status possíveis da run: `ok`, `erro_api`, `erro_json`, `erro_validacao` e `erro_gravacao`.

Comandos:

```bash
python -m tcc_scraper.cli analyze-llm --dry-run          # imprime o prompt; não chama a API nem grava
python -m tcc_scraper.cli analyze-llm                    # execução real, data de hoje em LLM_TZ
python -m tcc_scraper.cli analyze-llm --data 2026-10-13  # reexecuta um dia anterior e substitui as staging
python -m tcc_scraper.cli analyze-llm --modelo gemini-3.8-flash
```

Operação:

- O daily automático roda uma vez por dia. Um disparo manual pelo painel ou por `daily --etapa analise` é uma chamada a mais, com custo.
- Se o processo reiniciar durante a análise, a execução vira `falhou` com a mensagem `interrompida por reinício do executor` e não é refeita automaticamente. Refazer seria uma nova chamada paga.
- Antes de subir, valide a imagem (opcional, com Docker): `docker build -t tcc-llm .` e `docker run --rm -e GEMINI_API_KEY=x tcc-llm python -m tcc_scraper.cli analyze-llm --dry-run`. Confirme que o template `prompts/*.txt` está na imagem: o wheel inclui o diretório inteiro de `src/tcc_scraper`.

### 3.4 Versionar o prompt

Vale para a análise (`analise_diaria_v{N}.txt`, `VERSAO` em `llm/prompt.py`, hoje `3`) e para a juíza (`juiza_v{N}.txt`, `VERSAO` em `llm/juiza_prompt.py`, hoje `1`):

1. Crie o arquivo com o próximo número (`analise_diaria_v4.txt` ou `juiza_v2.txt`). Não edite a versão antiga no lugar.
2. Suba `VERSAO` no módulo correspondente.
3. Faça o deploy normal.

Se o template da versão atual mudar sem subir `VERSAO`, `garantir_prompt` recusa a execução (sha256 diferente do gravado). Com `VERSAO` subida, as linhas novas registram a versão usada. Na juíza, os pares precisam ser julgados de novo, porque a chave inclui `prompt_versao`.

### 3.5 Variáveis

| Variável | Padrão | Uso |
|---|---|---|
| `ANALISE_ENABLED` | `false` | Liga a etapa `analise` do daily. Desligada até a análise migrar para a OpenAI |
| `GEMINI_API_KEY` | vazio | Chave do Google AI Studio. Sem ela, a análise é pulada. A juíza só a usa com `JUIZA_PROVEDOR=gemini` |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Modelo da análise diária |
| `LLM_TIMEOUT_S` | `180` | Timeout de cada chamada |
| `LLM_MAX_ATTEMPTS` | `3` | Tentativas com backoff em falhas transitórias |
| `LLM_TZ` | `America/Sao_Paulo` | Fuso que define a `data_referencia` |
| `JUIZA_PROVEDOR` | `openai` | LLM de fallback da juíza: `openai` ou `gemini` (seção 2.3) |
| `JUIZA_MODELO` | vazio | Override do modelo da juíza. Vazio = padrão do provedor (`gpt-5.6-luna` ou `gemini-3.1-flash-lite`). Com `openai`, um `gemini-...` gera `modelo_incompativel`: remova a variável |
| `JUIZA_MAX_ITENS_DIA` | `2000` | Teto de pares pagos na etapa diária (seção 2.5) |
| `JUIZA_LOTE` | `50` | Itens por chamada à LLM de fallback |
| `JUIZA_PAGINA` | `5000` | Pares lidos do banco por página |
| `JUIZA_MAX_TENTATIVAS` | `3` | Tentativas por par antes de sair da fila |
| `JUIZA_JEV_MODO` | `entrega` | `entrega`: o Jev decide por entrega segura (seção 2.4.1). `triagem`: o Jev só descarta. `decide`: o Jev decide com `JUIZA_CONFIANCA_MINIMA` |
| `JUIZA_JEV_LIMIAR_ENTREGA` | `0.9` | Limiar de probabilidade do modo `entrega` (maior que 0,5 e até 1). Medido em 2026-10-26: 0,9 é o limiar recomendado de `--concordancia` (seção 2.7) |
| `JUIZA_CONFIANCA_MINIMA` | `0.5` | Confiança mínima do Jev para aceitar, com `JUIZA_JEV_MODO=decide`. Não filtra a LLM |
| `JUIZA_CONFIANCA_TRIAGEM` | `0.9` | Confiança mínima do Jev para aceitar um descarte, com `JUIZA_JEV_MODO=triagem` |
| `JUIZA_JEV_DECIDE` | `false` | **Obsoleta.** Só vale se `JUIZA_JEV_MODO` não foi definido (`true` equivale a `decide`). Use `JUIZA_JEV_MODO` |
| `JUIZA_ORCAMENTO_MENSAL_USD` | `3.0` | Teto em US$ da juíza diária, por mês |
| `JUIZA_BACKLOG_VERBA_USD` | `15.0` | Verba única, em US$, do backlog (cumulativa entre execuções) |
| `JUIZA_PRECO_ENTRADA_USD_MTOK`, `JUIZA_PRECO_SAIDA_USD_MTOK`, `JUIZA_PRECO_CACHE_USD_MTOK` | vazios (tabela do provedor: OpenAI `0.20`, `1.20`, `0.02`; Gemini `0.25`, `1.50`, `0.25`) | Preço da LLM de fallback, em US$ por milhão de tokens. Entrada, saída e entrada em cache |
| `JEV_ENABLED` | `false` | Liga o Jev. Com `true` e sem `JEV_API_KEY`, a LLM fica bloqueada (`jev_sem_chave`) |
| `JEV_API_KEY`, `JEV_MODELO` | vazio, `jev-1.13.0` | Chave e versão fixada do Jev |
| `JEV_PRECO_ENTRADA_USD_MTOK` | `0.042` | Preço do Jev, em US$ por milhão de tokens de entrada. Saída grátis |
| `JEV_LOTE`, `JEV_CONCORRENCIA` | `50`, `8` | Pares por lote e requisições em paralelo |
| `JEV_TIMEOUT_S` | `30.0` | Timeout de cada requisição ao Jev |
| `JEV_TRECHO` | `1500` | Caracteres do corpo que o Jev recebe (faixa de 300 a 8.000). A LLM continua com 300 |
| `OPENAI_API_KEY` | vazio | Chave da OpenAI, da LLM de fallback com `JUIZA_PROVEDOR=openai`. Sem ela, aviso `openai_sem_chave` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base da API da OpenAI |
| `OPENAI_TIMEOUT_S` | `120` | Timeout de cada chamada à OpenAI |
| `OPENAI_MAX_ATTEMPTS` | `3` | Tentativas por chamada à OpenAI em falhas transitórias. Não há retry em `insufficient_quota` (parada `sem_credito`) |
| `OPENAI_REASONING_EFFORT` | `none` | Esforço de raciocínio do `gpt-5.6-luna`. `none` é o menor valor aceito |
| `OPENAI_MAX_OUTPUT_TOKENS` | `16000` | Teto de tokens de saída por chamada à OpenAI |
| `ENTREGA_FILTRO_JUIZA` | `false` | Filtro da juíza na `v1-noticias` (ver [05-entrega.md](05-entrega.md)). `false` no código até o backlog zerar. O dono decidiu que o filtro deve ficar ligado; a troca do padrão é o último passo de [08](08-operacao-e-deploy.md), seção 5.1 |

### 3.6 Custo

Há duas frentes pagas, com tetos separados:

- **Análise diária (Gemini).** **Teto aprovado: US$ 5/mês** para o Gemini da análise. Não inclui a juíza, que não usa mais o Gemini por padrão. Desde 2026-10-26 a etapa está desligada (`ANALISE_ENABLED=false`), porque o crédito pré-pago acabou em 2026-10-24. O teto fica como referência até a migração para a OpenAI.
- **Juíza (Jev e LLM de fallback).** Teto de US$ 3/mês para a juíza diária (`JUIZA_ORCAMENTO_MENSAL_USD`) e verba única de US$ 15 para o backlog (`JUIZA_BACKLOG_VERBA_USD`, seção 2.5). Os dois tetos somam o Jev e a LLM de fallback, seja OpenAI ou Gemini. O Jev custa US$ 0,042 por milhão de tokens de entrada. A LLM de fallback padrão é a OpenAI `gpt-5.6-luna`, com preço oficial de 2026-10-26: US$ 0,20 de entrada, US$ 0,02 de entrada em cache e US$ 1,20 de saída, por milhão de tokens.

Essas exceções estão em `CLAUDE.md`, seções 1 e 9.

Se a OpenAI devolver `insufficient_quota` (crédito esgotado), a juíza para com `sem_credito` e não repete a chamada. Recarregue o crédito antes de retomar. O mesmo vale para o 402 do Gemini. Isso já aconteceu: em 2026-10-24, o crédito pré-pago do Gemini acabou no meio da carga histórica, com 402. Naquela carga, a juíza gastou US$ 2,24 em 13.144 pares, julgados só pelo Gemini `gemini-3.1-flash-lite` (cerca de 139 tokens de entrada e 91 de saída por par; quase 80% do custo era saída).

Preço do `gemini-3.8-flash`, verificado em 2026-10-15: US$ 0,75 por milhão de tokens de entrada e US$ 3,75 por milhão de saída (inclui o raciocínio). Buscas do Google: 5.000 por mês grátis; depois, US$ 14 por mil. O preço vale até 31/12/2026 e dobra em 2027.

Uso medido com o prompt v3, em 3 execuções de 2026-10-15: cerca de 3,2 mil tokens de entrada, de 6,7 a 8,5 mil de saída, de 8 a 14 mil de raciocínio (cobrado como saída), 14 buscas e de 85 a 133 segundos por execução.

| Medida | Valor |
|---|---|
| Custo por chamada | US$ 0,06 a 0,09 |
| Custo mensal (1 chamada por dia) | US$ 1,8 a 2,6, até 31/12/2026 |
| Custo mensal com preço dobrado em 2027 | US$ 3,6 a 5,1, no limite do teto |

Com preço dobrado, o cenário pessimista ultrapassa o teto. Reavalie o modelo ou a frequência antes da virada do ano. O `gemini-3.1-flash-lite` custaria cerca de um terço.

Comparativo com outros modelos (página oficial de preços, 2026-10-15; US$ por 1M tokens; estimativa com ~10 mil tokens de entrada e ~15 mil de saída por chamada, 1 chamada por dia). Os modelos 2.5 **não** aceitam `google_search` e JSON schema na mesma chamada: exigiriam 2 chamadas por dia e mudança no cliente.

| Modelo | Entrada | Saída | Chamadas/dia | US$/mês estimado | Observação |
|---|---|---|---|---|---|
| `gemini-3.1-flash-lite` | 0,25 | 1,50 | 1 | ~0,75 | mais barato com JSON + grounding juntos |
| `gemini-3.5-flash-lite` | 0,30 | 2,50 | 1 | ~1,22 | |
| `gemini-3-flash-preview` | 0,50 | 3,00 | 1 | ~1,50 | preview |
| `gemini-3.8-flash` (padrão) | 0,75 | 3,75 | 1 | ~1,91 | promocional até 31/12/2026; dobra em 2027 |
| `gemini-3.1-pro-preview` | 2,00 | 12,00 | 1 | ~6,00 | sem free tier; estoura o teto |
| `gemini-4-argon` | 2,00 | 10,00 | 1 | ~5,10 | até 30/11/2026; de 1/12 vira 4,00 / 20,00 (~10,20/mês) |
| `gemini-2.5-flash` | 0,30 | 2,50 | 2 | ~2,10 | precisa de 2 chamadas |

Antes de trocar `GEMINI_MODEL`, confirme o id com `client.models.list()` e o preço na página oficial: só o `gemini-3.8-flash` foi exercitado no spike.

Cenário pessimista (2x tokens: ~20 mil de entrada e ~30 mil de saída por chamada): ~US$ 0,128 por dia, ~US$ 3,83 por mês. Em 2027, com preço dobrado, ~US$ 7,65 por mês, acima do teto. Para sair da faixa grátis de 5.000 buscas por mês com 1 chamada por dia, seriam necessárias ~167 buscas por chamada.

Alerta de orçamento: crie no Google Cloud Billing um orçamento de US$ 5 no projeto do Gemini, com alertas em 50%, 90% e 100%.

### 3.7 Consultas de leitura da análise

Últimas execuções:

```sql
SELECT id, data_referencia, pregao_referencia, status, tokens_entrada, tokens_saida, latencia_ms
  FROM `raw_llm_runs`
 ORDER BY id DESC
 LIMIT 10;
```

Sinais do dia por ação:

```sql
SELECT ticker, direcao_esperada, confianca, motivo
  FROM `staging_llm_acoes_sinais`
 WHERE data_referencia = CURDATE()
 ORDER BY confianca DESC;
```

Notícias de uma empresa no dia:

```sql
SELECT ticker, titulo, fonte, url, sentimento, relevancia
  FROM `staging_llm_noticias_empresas`
 WHERE data_referencia = CURDATE() AND ticker = 'PETR4';
```

`CURDATE()` usa o fuso do servidor MySQL, e `data_referencia` usa o calendário de `LLM_TZ`. Entre 21:00 e 23:59 de Brasília (00:00 a 02:59 UTC), as duas datas podem diferir. Para um dia específico, troque `CURDATE()` por uma data literal.

## 4. Lacunas e pendências

- **Sem conjunto-ouro.** Foi removido em 2026-10-23. A qualidade da juíza e do Jev é medida por concordância (seção 2.7). Concordância não é acurácia, e não há precisão nem revocação por rótulo humano.
- **`JUIZA_CONFIANCA_MINIMA` só vale para o Jev, com `JUIZA_JEV_MODO=decide`.** A LLM de fallback não é filtrada por confiança.
- **Perda de `tickers_adicionais`.** O Jev não devolve essa lista. Os pares que ele decide ficam sem os tickers extras. Medido em 2026-10-26: 28% dos pares decididos no limiar 0,9 (97 de 344) perdem tickers. Limitação para o TCC.
- **Assimetria de contexto na comparação.** A referência viu 300 caracteres e o Jev vê 1.500. Parte da discordância pode ser acerto do Jev. Limitação para o TCC. Na amostra de 2026-10-26, o trecho médio tinha só 88 caracteres (muitos artigos de 2024 não têm corpo), então o Jev julgou quase só pela manchete. A concordância medida reflete esse caso, não o do corpo completo.
- **Mesma versão de prompt para dois provedores.** O prompt da OpenAI (`juiza_openai`) e o do Gemini (`juiza`) são a versão 1, com schemas diferentes. Os julgamentos se distinguem pelo campo `modelo`, não pela versão.
- **`.env.example` com `JEV_ENABLED=true`, código com `false`.** É proposital: o exemplo mostra a configuração de produção (Jev ligado), e o padrão `false` do código protege a máquina sem `JEV_API_KEY` (com `true` e sem chave, a juíza para com `jev_sem_chave`).
- **Chave do Jev.** A chave atual vazou em uma transcrição. Gere uma nova antes de implantar.
- **`JUIZA_MAX_TOKENS_DIA` não existe.** A spec (seção 6.2) prevê um teto de tokens por dia. O código tem só o teto de itens e o teto em US$.
- **Chamada sem busca não confirmada em produção.** O método `classificar` envia `generation_config` com `temperature = 0` na Interactions API. A confirmação numa chamada real é o spike S7, ainda pendente em `progress.md`.
- **Custo do backlog não medido.** O custo por par foi medido no spike de 2026-10-26 (seção 2.5). Falta o custo real do backlog, com as runs gravadas em `v1-raw_llm_juiza_runs`.
- **Implantação na máquina do executor.** `JEV_ENABLED=false` no padrão. A medição de 2026-10-26 foi feita, mas o executor ainda não está com o Jev e a OpenAI configurados no modo `entrega`. O checklist está em [08-operacao-e-deploy.md](08-operacao-e-deploy.md), seção 5.2.
- **Análise diária desligada.** `ANALISE_ENABLED=false` desde 2026-10-26 (seção 3.3). A migração para a OpenAI está pendente: ela exige trocar a busca do Google, que hoje é embutida no Gemini. Até lá, `staging_llm_*` não é atualizada pelo daily.
- **Julgamentos de versões diferentes.** Um par pode ter julgamento de mais de uma versão do prompt. A montagem da entrega usa a versão mais alta.
- **Teto de itens por execução, não por dia civil.** `JUIZA_MAX_ITENS_DIA` vale por execução de `daily`. Duas execuções no mesmo dia somam o dobro. O teto em US$ do diário é mensal e não reinicia por execução.
