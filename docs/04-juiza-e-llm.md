# Juíza de notícias e análise diária por LLM

Este documento cobre a juíza de notícias (julgamento de pares artigo-ticker sem busca na web) e a análise diária v3 (Gemini com busca), com custo, persistência, consultas e versionamento de prompt.

## 1. Duas chamadas ao Gemini, duas funções

| Camada | Função | Usa busca na web | Grava em | Entra na entrega |
|---|---|---|---|---|
| Juíza | Julga pares (artigo, ticker) já coletados | Não | `v1-raw_llm_juiza_runs`, `v1-staging_noticias_julgamento`, `v1-staging_juiza_tentativas` | Sim, como filtro e como colunas `papel`, `tipo_conteudo`, `evento`, `qualidade` de `v1-noticias` |
| Análise diária v3 | Pesquisa as notícias do dia e produz sinais por ação | Sim (`google_search`) | `raw_llm_prompts`, `raw_llm_runs`, `staging_llm_*` | Não. A montagem da entrega não lê estas tabelas |

A juíza roda na etapa `juiza` do daily (ordem 6), antes da montagem da entrega. A análise roda na etapa `analise` (ordem 9), depois dela.

## 2. Juíza de notícias

### 2.1 Fluxo por par pendente

1. **Pendentes.** Pares (artigo, ticker) com vínculo em `article_tickers` e sem linha em `v1-staging_noticias_julgamento` para a versão vigente do prompt (`VERSAO` em `llm/juiza_prompt.py`, hoje `1`). Pares que já gastaram `JUIZA_MAX_TENTATIVAS` tentativas (padrão 3) saem da fila. A leitura é paginada, com `JUIZA_PAGINA` pares por página. A ordem depende do modo (seção 2.5): o diário vai do mais recente para o mais antigo; o backlog, do mais antigo para o mais recente.
2. **Regra barata** (seção 2.2). Resolve o caso óbvio sem LLM e grava com `modelo = 'regra'`. A regra não conta para o teto de itens.
3. **Jev** (seção 2.4). Roda se `JEV_ENABLED=true` e houver `JEV_API_KEY`. Recebe o que a regra não resolveu, em lotes de `JEV_LOTE` pares. O que ele não aceita segue para o Gemini.
4. **Gemini.** Lotes de `JUIZA_LOTE` itens (padrão 50). Uma chamada por lote, sem `google_search`, com `temperature = 0` e saída em JSON conforme o schema.
5. **Validação.** A resposta é validada item a item. Um item inválido (enum fora da lista, nota fora de 0 a 1 etc.) vira erro da run e não derruba o lote: os demais itens válidos entram. Só entram os pares pedidos no lote, sem duplicatas. JSON inválido ou resposta sem a lista `itens` derrubam o lote inteiro.
6. **Gravação.** Cada chamada paga gera uma linha em `v1-raw_llm_juiza_runs`, com `custo_usd` e `finalidade`, inclusive as que falharam. Só os julgamentos aceitos vão para `v1-staging_noticias_julgamento`, na mesma transação da run. Cada par pedido ao Gemini e não aceito ganha uma tentativa em `v1-staging_juiza_tentativas` (seção 2.6).
7. **Teto.** Antes de cada lote pago, o teto em US$ é conferido (seção 2.5). Cada par que gasta LLM conta uma vez no teto de itens, mesmo que passe pelo Jev e depois pelo Gemini.

O processo é retomável. Um par sem julgamento para a versão vigente continua pendente na execução seguinte, até esgotar as tentativas. Os pares de um lote que falhou continuam pendentes, com uma tentativa a mais, exceto na falha da API (`erro_api`), que não conta tentativa.

| `status` da run | Significado | Tentativa para os pares não aceitos |
|---|---|---|
| `ok` | Todos os pedidos vieram julgados e válidos | Não se aplica |
| `parcial` | Parte dos itens veio inválida ou faltou. Os válidos foram gravados | Sim |
| `erro_validacao` | Nenhum item válido, ou a resposta não tem a lista `itens` | Sim |
| `erro_json` | A resposta não é JSON válido | Sim |
| `erro_api` | A chamada falhou depois das tentativas de retry | Não |

As runs do Jev usam `ok`, `parcial` ou `erro_api`. Os pares que o Jev não aceita não ganham tentativa: seguem para o Gemini.

Sem `GEMINI_API_KEY`, os pares que sobram da regra barata (e do Jev) ficam pendentes e o log registra um aviso. A etapa `juiza` termina como concluída, com zero itens julgados pela LLM. Sem chave de nenhum dos dois, só a primeira página de regra roda.

### 2.2 Regra barata (`llm/juiza_regras.py`)

A regra é avaliada nesta ordem. A primeira condição que casar decide o par.

| Ordem | Condição | Resultado (`papel`, `tipo_conteudo`, `evento`, `qualidade`, `confianca`) |
|---|---|---|
| 1 | `origem = 'cvm_ipe'` | `protagonista`, `reportagem`, `fato_relevante`, 1.0, 1.0 |
| 2 | Domínio em `DOMINIOS_BLOQUEADOS` (youtube.com, youtu.be, spotify.com, open.spotify.com, podcasts.apple.com, tiktok.com) | `mencao`, `podcast_video`, `outro`, 0.0, 1.0 |
| 3 | Título casa um dos 9 padrões de `PADROES_PANORAMA` (ex.: "Ibovespa sobe", "agenda do dia", "o que esperar", "maiores altas") | `mencao`, `resumo_mercado`, `macro`, 0.1, 0.9 |
| 4 | Três ou mais empresas distintas do universo no título, fora o próprio ticker | Igual à linha 3 |
| 5 | Nenhuma condição casou | Segue para o Gemini |

Os nomes casam como palavra inteira, sem acento e sem diferença de maiúscula. Termo com até 5 letras (Vale, Azul, Rumo, Vamos, Light) só casa se estiver com inicial maiúscula no título original, para não confundir o verbo "valer" com a empresa.

### 2.3 Gemini na juíza

- **Modelo:** `JUIZA_MODELO`, padrão `gemini-3.1-flash-lite`. É independente de `GEMINI_MODEL`, que é da análise diária.
- **Chamada:** método `classificar` do `GeminiClient`, que usa `_chamar_api_sem_busca`: sem `google_search`, com `temperature = 0` e `response_format` JSON com o schema. Falhas transitórias são repetidas com backoff, até `LLM_MAX_ATTEMPTS` tentativas e com timeout `LLM_TIMEOUT_S`.
- **Entrada por item:** `id`, `ticker`, `empresa`, `titulo`, `fonte` e `trecho`. O `trecho` são os primeiros 300 caracteres do corpo, quando o corpo existe. O universo, com nome e setor de cada ticker e os apelidos, vai uma vez no cabeçalho do prompt.
- **Saída por item:**

| Campo | Valores permitidos |
|---|---|
| `papel` | `protagonista`, `citada_com_fato`, `mencao`, `errada`, `matriz_estrangeira` |
| `tipo_conteudo` | `reportagem`, `opiniao`, `lista`, `agenda`, `resumo_mercado`, `podcast_video`, `newsletter`, `patrocinado`, `outro` |
| `evento` | `resultado`, `dividendo_jcp`, `fato_relevante`, `fusao_aquisicao`, `guidance`, `recomendacao`, `regulatorio`, `operacional`, `governanca`, `juridico`, `mudanca_ticker`, `macro`, `outro` |
| `tickers_adicionais` | Lista de tickers do universo que a matéria também trata. Tickers fora do universo são descartados |
| `qualidade` | Número de 0 a 1, pela rubrica do prompt |
| `confianca` | Número de 0 a 1 |

- **Prompt versionado:** template em `llm/prompts/juiza_v1.txt`, `VERSAO = 1` em `llm/juiza_prompt.py`. O sha256 cobre o template e o schema. `garantir_prompt` grava o prompt em `raw_llm_prompts` com `nome = 'juiza'` e recusa a execução se o texto mudar sem subir a versão.

### 2.4 Jev: segunda exceção paga, medida por concordância

O Jev (TypeSafe AI, modelo `jev-1.13.0`) é a segunda exceção paga do projeto, aprovada pelo dono em 2026-10-23. Não há conjunto-ouro: a qualidade do Jev é medida por concordância com o Gemini (seção 2.7). A decisão está no [ADR](ADR.md), em "Jev medido por concordância, sem conjunto-ouro".

- **Estado.** `JEV_ENABLED=false` no padrão. Com `JEV_ENABLED=true` e `JEV_API_KEY` definida, `julgar_pendentes` cria o cliente sozinho (`JevClient`, em `llm/jev_client.py`). Sem a chave, o Jev não roda e não há erro.
- **Chamada.** Uma requisição `POST /v1/systemone` por par (a API não aceita lote). As requisições do lote correm em paralelo (`JEV_CONCORRENCIA`, padrão 8), com retry em falhas transitórias e timeout de `JEV_TIMEOUT_S` (30 s). O estado enviado traz empresa, apelidos, fonte, título e trecho.
- **Perguntas.** `papel`, `tipo_conteudo` e `evento` são escolhas com os rótulos do schema da juíza. `qualidade` é uma escala de 5 níveis, convertida para `(nível − 1) / 4`. O Jev não devolve `tickers_adicionais`, que sai sempre vazio.
- **Versão fixa.** Modelo `jev-1.13.0`, nunca `latest`. As perguntas são versionadas em `JEV_PERGUNTAS_VERSAO` (hoje `1`): mudou texto ou critério, sobe o número. A staging registra o modelo como `jev-1.13.0/q1`.
- **Confiança.** A confiança do julgamento é a menor entre a de `papel` e a de `tipo_conteudo`.
- **Preço.** US$ 0,042 por milhão de tokens de entrada. A saída é grátis. Chamada real de teste, em 2026-10-23: HTTP 200, 507 ms, 453 tokens.
- **Critério de aceite.** Depende de `JUIZA_JEV_DECIDE`:
  - `false` (triagem, padrão): o Jev só aceita **descarte**. Aceita se a confiança for pelo menos `JUIZA_CONFIANCA_TRIAGEM` (0,9) e a regra de entrega reprovar o par. O resto vai ao Gemini.
  - `true` (o Jev decide): aceita o julgamento se a confiança for pelo menos `JUIZA_CONFIANCA_MINIMA` (0,5). O que ele não aceita vai ao Gemini.
- **Falha.** Um par que falha no Jev segue para o Gemini, sem tentativa. Se o Jev lançar exceção, ou falhar em todos os pares do lote, o lote inteiro vai ao Gemini. A run fica como `erro_api`, com o modelo do Jev.
- **Custo.** O custo do Jev entra nas mesmas somas da juíza (seção 2.5). Não tem teto próprio.

### 2.5 Limites e custo

| Limite | Valor padrão | Onde atua |
|---|---|---|
| `JUIZA_MAX_ITENS_DIA` | 2.000 | Teto de pares pagos na etapa `juiza` do daily. Não é um contador por dia civil |
| `JUIZA_ORCAMENTO_MENSAL_USD` | 3,0 | Teto em US$ da juíza diária: soma das runs `producao` desde o dia 1 do mês (fuso `DAILY_TZ`) |
| `JUIZA_BACKLOG_VERBA_USD` | 15,0 | Verba única do backlog: soma das runs `backlog`, de todas as execuções. Vale para a carga histórica e para `judge-news --backlog` |
| `--orcamento-usd` (`judge-news --backlog`) | Sem valor | Teto extra da rodada. Vale o menor entre ele e a verba que sobra |
| `--limite` (`judge-news`) | Sem valor | Teto de itens da rodada. No backlog, sem valor, não há teto de itens: vale só a verba |
| `JUIZA_MAX_TENTATIVAS` | 3 | Tentativas por par antes de sair da fila (seção 2.6) |
| `JUIZA_PAGINA` | 5.000 | Pares lidos por página, com cursor |
| `JUIZA_LOTE` | 50 | Itens por chamada ao Gemini |
| `JEV_LOTE` | 50 | Pares por lote enviado ao Jev (a API recebe um par por requisição) |

- **Teto em US$.** Antes de cada lote pago, o custo já gasto mais o custo do lote anterior é comparado com o teto. Se ultrapassar, a execução para com `parada=orcamento_usd`. O estouro máximo é de um lote.
- **Diário.** O teto é `JUIZA_ORCAMENTO_MENSAL_USD` menos o gasto de `producao` no mês. Não se reinicia a cada execução.
- **Backlog e carga histórica.** A carga histórica roda a juíza em modo `backlog`, uma vez por ano, sem teto de itens. O que limita é a verba de US$ 15, cumulativa entre execuções. A verba não entra no teto mensal do diário.
- **Teto compartilhado.** O total de US$ 5/mês do Gemini continua compartilhado com a análise diária (seção 3.6). A juíza diária usa até US$ 3 desse total.
- **Concordância não tem teto.** O `judge-news --concordancia` não consulta nenhum teto; o custo aparece no resumo (seção 2.7).
- **Preço do Gemini.** `gemini-3.1-flash-lite`, conforme a tabela da seção 3.6 (verificada em 2026-10-15): US$ 0,25 por milhão de tokens de entrada e US$ 1,50 de saída. O pensamento, quando o modelo informa, é cobrado como saída.
- **Gemini Batch API: rejeitada por ora.** O desconto seria pequeno em valor absoluto, e o job assíncrono aumenta a complexidade. Ver o ADR "Teto em US$ e verba do backlog; Batch API rejeitada".
- **Custo real.** O custo da juíza em produção ainda não foi medido. A concordância mede o custo de uma amostra.
- A etapa `juiza` termina como `concluida` mesmo com lotes em erro. Os erros aparecem no campo `erros` dos detalhes. Ela só fica `falhou` quando a execução é cancelada. Parada por orçamento ou por teto de itens não é falha.

### 2.6 Persistência

**`v1-raw_llm_juiza_runs`** (append-only, uma linha por chamada paga e por run-resumo da concordância)

| Coluna | Conteúdo |
|---|---|
| `id` | Chave primária |
| `prompt_id` | FK para `raw_llm_prompts` |
| `modelo`, `n_itens` | Modelo usado (`jev-1.13.0/q1` no Jev, `concordancia` na run-resumo) e quantidade de itens do lote |
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
| `modelo` | `'regra'`, o nome do modelo do Gemini, ou `jev-1.13.0/q1` quando o Jev decidiu |
| `papel`, `tipo_conteudo`, `evento` | Valores da seção 2.3 |
| `tickers_adicionais` | JSON com a lista de tickers do universo |
| `qualidade`, `confianca` | `Double` |
| `julgado_em` | Momento do julgamento, em UTC |

A gravação usa `bulk_upsert` sem colunas para atualizar. Se já existe linha para (artigo, ticker, versão), a linha antiga é mantida. Uma nova versão do prompt cria linhas novas, e por isso os pares voltam a ser pendentes quando `VERSAO` sobe.

**`v1-staging_juiza_tentativas`** (uma linha por par que o Gemini não aceitou, por versão)

| Coluna | Conteúdo |
|---|---|
| `article_id`, `ticker`, `prompt_versao` | Chave primária. `article_id` referencia `raw_articles` (cascata na exclusão) |
| `tentativas` | Número de tentativas feitas. Ao chegar a `JUIZA_MAX_TENTATIVAS`, o par sai da fila |
| `ultimo_status`, `ultimo_erro` | Status e erro da última tentativa |
| `atualizado_em` | Momento da última tentativa, em UTC |

A tabela existe só para contar tentativas. Em `v1-staging_noticias_julgamento`, "linha existe" continua querendo dizer "julgado". O contador é calculado no código, a partir do valor lido, e gravado com `bulk_upsert`.

### 2.7 Concordância Jev × Gemini (`judge-news --concordancia`)

Substitui o conjunto-ouro, que foi removido em 2026-10-23. Não há rótulo feito por pessoa: a medição compara o Jev com o Gemini.

- **Comando:** `python -m tcc_scraper.cli judge-news --concordancia N [--semente S]`. Semente padrão: 42. Exige `JEV_ENABLED=true`, `JEV_API_KEY` e `GEMINI_API_KEY`. Sem eles, falha antes de qualquer chamada.
- **Amostra.** N pares (artigo, ticker), sorteados por estrato (ano de publicação × fonte). As 10 fontes mais frequentes ficam em estratos próprios; as demais caem no estrato `outras`. A alocação é proporcional ao tamanho do estrato, com pelo menos 1 par por estrato não vazio, ajustada pelo maior resto. Pares que a regra barata resolve são descartados e contados, mas não entram. Artigos do IPE (`cvm_ipe`) ficam fora.
- **Reprodutível.** A ordem dentro de cada estrato é `crc32(id|ticker|semente)`. A mesma semente gera a mesma amostra.
- **Julgamento.** Cada par vai ao Jev (em lotes de `JEV_LOTE`) e ao Gemini (em lotes de `JUIZA_LOTE`). O Gemini é a referência.
- **O que não grava.** Nada vai para `v1-staging_noticias_julgamento` nem para `v1-staging_juiza_tentativas`, e nenhum teto é consultado. Cada chamada paga vira uma run com `finalidade='concordancia'` e `custo_usd` preenchido. A run-resumo tem `modelo='concordancia'`, `custo_usd` nulo e, em `resposta_bruta`, o JSON com semente, alocação, modelos, métricas e pares.
- **Métricas.** Para `papel`, `tipo_conteudo` e `entrega` (se o par seria aprovado para a entrega com o filtro da juíza): concordância (fração de pares com o mesmo rótulo), kappa de Cohen e n. Também a cobertura do Jev (fração dos pares com confiança do Jev de pelo menos `JUIZA_CONFIANCA_MINIMA`) e a concordância só nesses pares confiantes.
- **Regra de decisão.** Se o mínimo entre a concordância de `papel` e a de `tipo_conteudo` for pelo menos 0,85, a decisão é `jev_decide`. Senão, é `gemini_decide`.
  - `jev_decide`: o dono pode ligar `JUIZA_JEV_DECIDE=true`. O Jev decide, e o Gemini só entra abaixo de `JUIZA_CONFIANCA_MINIMA`.
  - `gemini_decide`: `JUIZA_JEV_DECIDE` fica `false`. O Jev vira triagem: só aceita descarte com confiança de pelo menos 0,9. O resto vai ao Gemini.
- **Quem decide.** O comando só imprime a decisão e não altera a configuração. Quem liga `JUIZA_JEV_DECIDE` é o dono.
- **Limitação.** Concordância não é acurácia. Dois modelos que erram do mesmo jeito concordam. Sem rótulo humano, não há precisão nem revocação. O número para o TCC é a concordância, com essa ressalva.
- **Resultado.** Ainda não medido. O passo a passo que o produz está em [08-operacao-e-deploy.md](08-operacao-e-deploy.md), seção 5.1.

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

Quantos julgamentos vieram da regra e quantos do Gemini:

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

1. Se não houver `GEMINI_API_KEY`, a etapa sai como `pulada`, sem chamada.
2. A data de referência é hoje no fuso `LLM_TZ` (padrão `America/Sao_Paulo`).
3. O prompt é renderizado e registrado em `raw_llm_prompts` (`garantir_prompt`).
4. O Gemini é chamado com busca e schema JSON.
5. A resposta passa por `parse_resposta` e pelo filtro do universo. Os links de redirecionamento do Google (`vertexaisearch`) são trocados pela URL real por `resolver_redirect_http`. Quando a resolução falha, o link original fica.
6. A run é gravada em `raw_llm_runs`, com o status.
7. Se o status for `ok`, as staging do dia são substituídas.

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
| `GEMINI_API_KEY` | vazio | Chave do Google AI Studio. Sem ela, a análise é pulada e a juíza deixa os pares pendentes |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Modelo da análise diária |
| `LLM_TIMEOUT_S` | `180` | Timeout de cada chamada |
| `LLM_MAX_ATTEMPTS` | `3` | Tentativas com backoff em falhas transitórias |
| `LLM_TZ` | `America/Sao_Paulo` | Fuso que define a `data_referencia` |
| `JUIZA_MODELO` | `gemini-3.1-flash-lite` | Modelo da juíza |
| `JUIZA_MAX_ITENS_DIA` | `2000` | Teto de pares pagos na etapa diária (seção 2.5) |
| `JUIZA_LOTE` | `50` | Itens por chamada ao Gemini |
| `JUIZA_PAGINA` | `5000` | Pares lidos do banco por página |
| `JUIZA_MAX_TENTATIVAS` | `3` | Tentativas por par antes de sair da fila |
| `JUIZA_CONFIANCA_MINIMA` | `0.5` | Confiança mínima do Jev para aceitar, com `JUIZA_JEV_DECIDE=true`. Não filtra o Gemini |
| `JUIZA_CONFIANCA_TRIAGEM` | `0.9` | Confiança mínima do Jev para aceitar um descarte, com `JUIZA_JEV_DECIDE=false` |
| `JUIZA_JEV_DECIDE` | `false` | `true`: o Jev decide. `false`: triagem, o Jev só descarta. Ligar só depois de medir com `--concordancia` (seção 2.7) |
| `JUIZA_ORCAMENTO_MENSAL_USD` | `3.0` | Teto em US$ da juíza diária, por mês |
| `JUIZA_BACKLOG_VERBA_USD` | `15.0` | Verba única, em US$, do backlog (cumulativa entre execuções) |
| `JUIZA_PRECO_ENTRADA_USD_MTOK`, `JUIZA_PRECO_SAIDA_USD_MTOK` | `0.25`, `1.50` | Preço do Gemini da juíza, em US$ por milhão de tokens |
| `JEV_ENABLED` | `false` | Liga o Jev. Sem `JEV_API_KEY`, não roda |
| `JEV_API_KEY`, `JEV_MODELO` | vazio, `jev-1.13.0` | Chave e versão fixada do Jev |
| `JEV_PRECO_ENTRADA_USD_MTOK` | `0.042` | Preço do Jev, em US$ por milhão de tokens de entrada. Saída grátis |
| `JEV_LOTE`, `JEV_CONCORRENCIA` | `50`, `8` | Pares por lote e requisições em paralelo |
| `JEV_TIMEOUT_S` | `30.0` | Timeout de cada requisição ao Jev |
| `ENTREGA_FILTRO_JUIZA` | `false` | Filtro da juíza na `v1-noticias` (ver [05-entrega.md](05-entrega.md)). `false` no código até o backlog zerar. O dono decidiu que o filtro deve ficar ligado; a troca do padrão é o último passo de [08](08-operacao-e-deploy.md), seção 5.1 |

### 3.6 Custo

**Teto aprovado: US$ 5/mês** para o Gemini, somando análise diária e juíza. A juíza diária tem teto próprio de US$ 3/mês (`JUIZA_ORCAMENTO_MENSAL_USD`), e o backlog tem verba única de US$ 15 (`JUIZA_BACKLOG_VERBA_USD`, seção 2.5). O Jev é a segunda exceção paga, com US$ 0,042 por milhão de tokens de entrada; o custo dele entra nas somas da juíza. Essas exceções estão em `CLAUDE.md`, seção 9.

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
- **`JUIZA_CONFIANCA_MINIMA` só vale para o Jev, com `JUIZA_JEV_DECIDE=true`.** O Gemini não é filtrado por confiança. O `.env.example` ainda descreve a variável como mínimo para qualquer item. Isso não bate com o código.
- **`JUIZA_MAX_TOKENS_DIA` não existe.** A spec (seção 6.2) prevê um teto de tokens por dia. O código tem só o teto de itens e o teto em US$.
- **Chamada sem busca não confirmada em produção.** O método `classificar` envia `generation_config` com `temperature = 0` na Interactions API. A confirmação numa chamada real é o spike S7, ainda pendente em `progress.md`.
- **Custo real da juíza não medido.** Está pendente o spike de custo com um dia de artigos, antes de processar o histórico.
- **Jev ainda desligado.** Aprovado como exceção paga, mas `JEV_ENABLED=false` no padrão. Para medir a concordância, é preciso ligá-lo e definir `JEV_API_KEY`.
- **Julgamentos de versões diferentes.** Um par pode ter julgamento de mais de uma versão do prompt. A montagem da entrega usa a versão mais alta.
- **Teto de itens por execução, não por dia civil.** `JUIZA_MAX_ITENS_DIA` vale por execução de `daily`. Duas execuções no mesmo dia somam o dobro. O teto em US$ do diário é mensal e não reinicia por execução.
