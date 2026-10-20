# Juíza de notícias e análise diária por LLM

Este documento cobre a juíza de notícias (julgamento de pares artigo-ticker sem busca na web) e a análise diária v3 (Gemini com busca), com custo, persistência, consultas e versionamento de prompt.

## 1. Duas chamadas ao Gemini, duas funções

| Camada | Função | Usa busca na web | Grava em | Entra na entrega |
|---|---|---|---|---|
| Juíza | Julga pares (artigo, ticker) já coletados | Não | `v1-raw_llm_juiza_runs`, `v1-staging_noticias_julgamento` | Sim, como filtro e como colunas `papel`, `tipo_conteudo`, `evento`, `qualidade` de `v1-noticias` |
| Análise diária v3 | Pesquisa as notícias do dia e produz sinais por ação | Sim (`google_search`) | `raw_llm_prompts`, `raw_llm_runs`, `staging_llm_*` | Não. A montagem da entrega não lê estas tabelas |

A juíza roda na etapa `juiza` do daily (ordem 6), antes da montagem da entrega. A análise roda na etapa `analise` (ordem 9), depois dela.

## 2. Juíza de notícias

### 2.1 Fluxo por par pendente

1. **Pendentes.** Pares (artigo, ticker) com vínculo em `article_tickers` e sem linha em `v1-staging_noticias_julgamento` para a versão vigente do prompt (`VERSAO` em `llm/juiza_prompt.py`, hoje `1`). A ordem é a publicação mais recente primeiro. O limite é `JUIZA_MAX_ITENS_DIA`.
2. **Regra barata** (seção 2.2). Resolve o caso óbvio sem LLM e grava com `modelo = 'regra'`.
3. **Jev** (seção 2.4). Só roda se `JEV_ENABLED=true` e um cliente for injetado. Nem a CLI nem a etapa injetam cliente, então hoje o passo não executa.
4. **Gemini.** Lotes de `JUIZA_LOTE` itens (padrão 50). Uma chamada por lote, sem `google_search`, com `temperature = 0` e saída em JSON conforme o schema.
5. **Validação.** A resposta é validada pelo modelo `LoteJulgamento`. Um item com valor de enum inválido faz o lote inteiro falhar. Só entram os pares pedidos no lote, sem duplicatas.
6. **Gravação.** Cada chamada gera uma linha em `v1-raw_llm_juiza_runs`, inclusive as que falharam. Só os julgamentos aceitos vão para `v1-staging_noticias_julgamento`, na mesma transação da run.

O processo é retomável. Um par sem linha de julgamento para a versão vigente volta como pendente na execução seguinte. Lote que falha deixa a run com status de erro e não grava julgamento; os pares desse lote continuam pendentes.

| `status` da run | Significado |
|---|---|
| `ok` | Resposta validada. Os julgamentos foram gravados |
| `erro_api` | A chamada ao Gemini falhou depois das tentativas de retry |
| `erro_json` | A resposta não é JSON válido |
| `erro_validacao` | O JSON não segue o schema (enum fora da lista, nota fora de 0 a 1 etc.) |

Sem `GEMINI_API_KEY`, os pares que sobram da regra barata ficam pendentes e o log registra um aviso. A etapa `juiza` termina como concluída com zero itens julgados pela LLM.

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

### 2.4 Jev: desligado, e por quê

- **Estado atual:** `JEV_ENABLED=false` no padrão. `llm/jev_client.py` tem o adaptador, mas `classificar_lote` levanta `NotImplementedError`. Não existe chamada real ao Jev no código.
- **Por que está desligado** (desenho, seção 6.3, e ADR do Jev):
  1. O Jev é API paga (a spec cita US$ 0,042 por milhão de tokens de entrada). O projeto tem custo zero, com exceção única do Gemini. Ligar o Jev exige exceção explícita do dono, com teto próprio. Essa aprovação está pendente.
  2. O teste em português ainda não foi feito. A documentação do Jev diz que o inglês é o idioma principal. O conjunto-ouro, que serviria para esse teste, não existe ainda (seção 2.7).
  3. Conta, lista de espera e forma de pagamento não foram confirmadas (spec 6.3, risco 2).
  4. A versão precisa ser fixada (`jev-1.13.0`, nunca `jev-latest`).
- **Efeito de falha:** se o Jev falhar, o lote segue para o Gemini e o aviso vai para o log.

### 2.5 Limites e custo

| Limite | Valor padrão | Onde atua |
|---|---|---|
| `JUIZA_MAX_ITENS_DIA` | 2.000 | Teto de pares julgados por execução da etapa `juiza` do daily e do `historico`. Não é um contador por dia civil |
| `--limite` em `judge-news` | Não informado: usa o teto acima | Troca o teto da rodada. Pode ser maior que `JUIZA_MAX_ITENS_DIA` |
| `JUIZA_LOTE` | 50 | Itens por chamada ao Gemini |
| `JUIZA_CONFIANCA_MINIMA` | 0,5 | Ver a seção 4: hoje só vale para o Jev |

- A juíza compartilha o teto de **US$ 5/mês** do Gemini com a análise diária (`CLAUDE.md`, seção 9).
- Preço do `gemini-3.1-flash-lite`, conforme a tabela comparativa da seção 3.6 (verificada em 2026-10-15): US$ 0,25 por milhão de tokens de entrada e US$ 1,50 de saída.
- **Custo real da juíza ainda não foi medido.** O spike de custo está pendente (seção 4).
- A etapa `juiza` termina como `concluida` mesmo com lotes em erro. Os erros aparecem no campo `erros` dos detalhes. Ela só fica `falhou` quando a execução é interrompida por orçamento ou cancelamento.

### 2.6 Persistência

**`v1-raw_llm_juiza_runs`** (append-only, uma linha por chamada ao Gemini)

| Coluna | Conteúdo |
|---|---|
| `id` | Chave primária |
| `prompt_id` | FK para `raw_llm_prompts` |
| `modelo`, `n_itens` | Modelo usado e quantidade de itens do lote |
| `tokens_entrada`, `tokens_saida`, `latencia_ms` | Medidas da chamada |
| `status`, `erro` | Ver a tabela de status na seção 2.1 |
| `resposta_bruta` | Texto devolvido pelo modelo, para auditoria |
| `iniciado_em`, `finalizado_em` | Início e fim da chamada, em UTC |

**`v1-staging_noticias_julgamento`** (um julgamento por par e versão)

| Coluna | Conteúdo |
|---|---|
| `article_id`, `ticker`, `prompt_versao` | Chave primária. `article_id` referencia `raw_articles` |
| `run_id` | FK para `v1-raw_llm_juiza_runs`. Vazio quando o julgamento veio da regra barata |
| `modelo` | `'regra'`, ou o nome do modelo que julgou |
| `papel`, `tipo_conteudo`, `evento` | Valores da seção 2.3 |
| `tickers_adicionais` | JSON com a lista de tickers do universo |
| `qualidade`, `confianca` | `Double` |
| `julgado_em` | Momento do julgamento, em UTC |

A gravação usa `bulk_upsert` sem colunas para atualizar. Se já existe linha para (artigo, ticker, versão), a linha antiga é mantida. Uma nova versão do prompt cria linhas novas, e por isso os pares voltam a ser pendentes quando `VERSAO` sobe.

### 2.7 Conjunto-ouro e `judge-news --avaliar`

- **Comando:** `python -m tcc_scraper.cli judge-news --avaliar`. Não grava nada.
- **Arquivo esperado:** `tests/fixtures/juiza_gold.csv`, com as colunas `article_id`, `ticker`, `titulo`, `papel` e `tipo_conteudo`.
- **Estado atual:** o arquivo **não existe** no repositório. O comando imprime "conjunto ouro ausente" e encerra. A rotulagem é tarefa do dono do projeto (cerca de 300 pares, estratificados por caso: homônimos, marcas, panoramas, matriz estrangeira, opinião).
- **O que mede:** precisão, revocação e suporte por `papel`, acurácia total e a contagem de quantos pares foram resolvidos pela regra e quantos pela LLM. Itens sem resposta válida contam como `sem_resposta`.
- **Limites da medição:**
  - Só `papel` entra na métrica. `tipo_conteudo` está no CSV mas não é avaliado.
  - O conjunto não traz origem, domínio nem trecho. A regra barata roda sem esses campos e o Gemini não vê o trecho. A medição, portanto, é mais pobre que a produção. Isso é uma limitação da avaliação, não do juiz em produção.

Número para o TCC: a precisão e a revocação por classe saem daqui, depois que o conjunto estiver rotulado.

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
| `JUIZA_MAX_ITENS_DIA` | `2000` | Teto de pares por execução (seção 2.5) |
| `JUIZA_LOTE` | `50` | Itens por chamada |
| `JUIZA_CONFIANCA_MINIMA` | `0.5` | Ver a seção 4 |
| `JEV_ENABLED` | `false` | Liga o adaptador do Jev. Sem cliente injetado, não roda |
| `JEV_API_KEY`, `JEV_MODELO` | vazio, `jev-1.13.0` | Chave e versão fixada do Jev |
| `ENTREGA_FILTRO_JUIZA` | `false` | Aplica o filtro da juíza na `v1-noticias` (ver [05-entrega.md](05-entrega.md)) |

### 3.6 Custo

**Teto aprovado: US$ 5/mês** para o Gemini, somando análise e juíza. É a única dependência paga do projeto, com exceção explícita à regra de custo zero (`CLAUDE.md`, seção 9).

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

- **Conjunto-ouro não existe.** `tests/fixtures/juiza_gold.csv` está ausente. A rotulagem depende do dono do projeto. Sem ela, não há precisão nem revocação da juíza para o TCC.
- **`JUIZA_CONFIANCA_MINIMA` só vale para o Jev.** O código aplica o limiar apenas em `_etapa_jev`. O julgamento do Gemini não é filtrado por `confianca`. O comentário em `.env.example` diz que a variável define a confiança mínima para aceitar um item, o que não corresponde ao código atual.
- **`JUIZA_MAX_TOKENS_DIA` não existe.** A spec (seção 6.2) prevê um teto de tokens por dia. O código tem só o teto de itens.
- **Chamada sem busca não confirmada em produção.** O método `classificar` envia `generation_config` com `temperature = 0` na Interactions API. A confirmação numa chamada real é o spike S7, ainda pendente em `progress.md`.
- **Custo real da juíza não medido.** Está pendente o spike de custo com um dia de artigos, antes de processar o histórico.
- **Avaliação mede só `papel`.** `tipo_conteudo` não entra na métrica do `--avaliar`.
- **Jev não implementado.** O adaptador levanta `NotImplementedError`. Ligar o Jev depende de aprovação do dono, de conta e de teste em português. A entrada "Fonte JEV implementada, mas desligada" do [ADR](ADR.md) registra o motivo.
- **Julgamentos de versões diferentes.** Um par pode ter julgamento de mais de uma versão do prompt. A montagem da entrega usa a versão mais alta.
- **Limite por execução, não por dia civil.** O teto `JUIZA_MAX_ITENS_DIA` vale por execução de `daily` ou de `historico`. Duas execuções no mesmo dia somam o dobro.
