# Banco de dados: MySQL 8 único, dicionário das 44 tabelas e schema

Este documento cobre o banco do projeto: o servidor, o dicionário de todas as tabelas, o schema e as alterações de
schema, as regras de tipo e de escrita e os testes que gravam no banco. Absorve o antigo roteiro de migração MySQL, que foi removido.

## 1. Banco único

O projeto usa **um** banco: o MySQL 8 definido no `.env`. Não existe PostgreSQL, banco local nem banco de
desenvolvimento. Coleta, dataset, executor e a API do painel gravam e leem no mesmo servidor.

A URL é montada por `Settings.effective_url` (`src/tcc_scraper/config.py`) a partir de `IP_BANCO`, `USER_BANCO`,
`SENHA_BANCO`, `PORTA_BANCO` e `NOME_BANCO`, com driver `mysql+pymysql` e `charset=utf8mb4`. Se faltar
`IP_BANCO`, `USER_BANCO` ou `SENHA_BANCO`, o erro diz quais. Use o IP do servidor, não `localhost`: em algumas
configurações o `localhost` força conexão por socket Unix e ignora a porta.

| Item | Exigência | Motivo |
|---|---|---|
| Versão do servidor | MySQL 8.0 ou superior | Em 5.7 o schema não sobe: o índice de `url_hash` com `utf8mb4` não cabe |
| Charset do banco | `utf8mb4` | O `utf8` do MySQL guarda 3 bytes por caractere e corrompe emoji e parte dos acentos das manchetes |
| Collation | `utf8mb4_0900_ai_ci` (padrão do MySQL 8) | Ignora acento em `LIKE`; ver seção 5 |
| Driver | `pymysql` e `cryptography` | O `cryptography` é exigido pelo `caching_sha2_password`, padrão do MySQL 8 |
| Pool | `pool_recycle=3600` em `get_engine()` | O MySQL fecha conexão ociosa (`wait_timeout`, 8 h) e a coleta dura cerca de 10 h |

Servidor em produção, conforme registrado no roteiro anterior: MySQL 8.0.46 (Ubuntu 24.04),
`system_time_zone = -03`, `max_allowed_packet` de 64 MB, `wait_timeout` de 8 h.

Criação do banco, com o charset obrigatório:

```sql
CREATE DATABASE tcc_scraper
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
```

Usuários. Criar o schema e alterá-lo exige DDL (`CREATE`, `ALTER`, `DROP`, `INDEX`). O executor e a coleta precisam só de
`SELECT`, `INSERT`, `UPDATE` e `DELETE`. Para um servidor novo, restrinja o acesso ao IP de quem conecta:

```sql
CREATE USER 'tcc'@'SEU_HOST' IDENTIFIED BY 'TROQUE_ESTA_SENHA';
GRANT ALL PRIVILEGES ON tcc_scraper.* TO 'tcc'@'SEU_HOST';
FLUSH PRIVILEGES;
```

## 2. Camadas, prefixos e nomes

As camadas são **prefixo no nome da tabela**, não schema: no MySQL, "schema" e "database" são a mesma coisa.

| Prefixo | Camada | Exemplo |
|---|---|---|
| `raw_` | Dado bruto, gravado a partir da rede ou de chamadas externas | `raw_prices` |
| `staging_` | Derivado da raw por modelo ou regra | `staging_article_sentiment` |
| `features_` | Dataset materializado (v1 congelado) | `features_dataset_v1` |
| `painel_` | Estado do executor e da carga, lido pelo painel | `painel_logs` |
| sem prefixo de camada | Tabelas de entrega, com nomes fixos pela spec | `noticias` (com `v1-`) |

O prefixo `v1-` (`PREFIXO_TABELA` em `db/models.py`) marca as **30 tabelas criadas pela pipeline nova**
(criadas nas antigas migrations 0004 a 0007 e nas alterações posteriores), por pedido do dono: 8 `painel_*`, 13 de fontes, juíza e coleta WordPress e as 9 de entrega (incluindo `v1-composicao_ibov`).
As 14 tabelas que já existiam (antigas migrations 0001 a 0003) mantêm o nome original. `features_dataset_v1` também não
recebe o prefixo.

O hífen obriga crase em SQL escrito à mão:

```sql
SELECT * FROM `v1-painel_logs` ORDER BY ts DESC LIMIT 20;
```

A função `tabela_do_projeto(name)` (`db/models.py`) diz se uma tabela pertence ao projeto. Ela tira o `v1-` antes de
comparar o prefixo de camada.

## 3. Dicionário das 44 tabelas

Chave primária (PK) segundo o modelo SQLAlchemy. "Escreve" aponta o módulo que grava a tabela. "Linhas" aparece
só onde o ledger do trabalho registra um número; caso contrário, `-`.

### 3.1 Tabelas sem prefixo `v1-` (14)

| Tabela | Camada | Escreve | Chave (PK) | Migration | Linhas |
|---|---|---|---|---|---|
| `raw_articles` | raw | `collectors/google_news.py` (notícias), `collectors/cvm_ipe.py` (fatos relevantes, `origem` IPE), `enrich/article_fetcher.py` (UPDATE de `resolved_url`) | `id`; único `url_hash` | 0001 (`origem` e `published_precision` na 0004) | - |
| `raw_article_bodies` | raw | `enrich/article_fetcher.py` | `article_id` | 0001 | - |
| `raw_prices` | raw | `collectors/prices_yahoo.py` | `ticker`, `date` | 0001 | - |
| `raw_macro` | raw | `collectors/macro_bcb.py` (Selic, PTAX) e `collectors/indices.py` (CDI, IPCA) | `series`, `date` | 0001 | - |
| `raw_collection_runs` | raw (checkpoint) | `collectors/base.py` (`Collector._record`), a cada unidade de trabalho | `id`; único (`collector`, `unit_key`) | 0001; NOT NULL na 0002 | - |
| `staging_article_tickers` | staging | `collectors/google_news.py` e `collectors/cvm_ipe.py` (vínculo notícia-ticker) | `article_id`, `ticker` | 0001 | - |
| `staging_article_sentiment` | staging | `sentiment/scoring.py` (FinBERT) | `article_id`, `model` | 0001 | - |
| `features_dataset_v1` | features (v1 congelado) | `features/build.py` (`build_dataset_v1`) | `data`, `ticker` | 0001 | - |
| `raw_llm_prompts` | raw (LLM) | `llm/job.py` (template versionado) | `id`; único (`nome`, `versao`) | 0003 | - |
| `raw_llm_runs` | raw (LLM) | `llm/job.py`: uma linha por tentativa, inclusive as com erro | `id` | 0003 | - |
| `staging_llm_noticias_empresas` | staging (LLM) | `llm/job.py`, substituída por dia | `data_referencia`, `ticker`, `item_hash` | 0003 | - |
| `staging_llm_noticias_macro` | staging (LLM) | `llm/job.py`, substituída por dia | `data_referencia`, `item_hash` | 0003 | - |
| `staging_llm_acoes_sinais` | staging (LLM) | `llm/job.py`, substituída por dia | `data_referencia`, `ticker` | 0003 | - |
| `staging_llm_mercados` | staging (LLM) | `llm/job.py`, substituída por dia | `data_referencia`, `nome` | 0003 | - |

### 3.2 Tabelas com prefixo `v1-` (30)

Painel (migration 0004). Quem escreve é o executor, salvo `v1-painel_comandos`, que a API do painel cria e confirma.

| Tabela | Escreve | Chave (PK) | Migration | Linhas |
|---|---|---|---|---|
| `v1-painel_comandos` | API do painel (fila de comandos) | `id` (String 36) | 0004 | - |
| `v1-painel_comandos_aplicados` | executor (`orquestrador/repositorio.py`), idempotência | `id` | 0004 | - |
| `v1-painel_heartbeat` | executor, a cada `EXECUTOR_HEARTBEAT_S` | `host` | 0004 | - |
| `v1-painel_execucoes` | executor (daily e etapa) | `id` | 0004 | - |
| `v1-painel_etapas` | executor (uma linha por etapa da execução) | `execucao_id`, `etapa_id` | 0004 | - |
| `v1-painel_historico` | executor (carga histórica) | `id` | 0004 | - |
| `v1-painel_historico_progresso` | executor (um registro por ano e coletor) | `ano`, `coletor` | 0004 | - |
| `v1-painel_logs` | executor; limpeza por `PAINEL_LOGS_DIAS` | `id` (BigInteger, autoincremento) | 0004 | - |

Fontes oficiais e juíza (migration 0005).

| Tabela | Escreve | Chave (PK) | Migration | Linhas |
|---|---|---|---|---|
| `v1-raw_ipe_docs` | `collectors/cvm_ipe.py` (IPE da CVM) | `protocolo_entrega` | 0005; larguras na 0007 | 2.714 |
| `v1-raw_cvm_balancos` | `collectors/cvm_demonstracoes.py` (ITR e DFP, demonstrações) | `cnpj`, `tipo_documento`, `dt_refer`, `versao`, `demonstracao`, `cd_conta`, `dt_ini_exerc`, `dt_fim_exerc` | 0005 | - |
| `v1-raw_cvm_acoes` | `collectors/cvm_demonstracoes.py` (composição do capital) | `cnpj`, `tipo_documento`, `dt_refer`, `versao` | 0005 | - |
| `v1-raw_cvm_cadastro` | `collectors/cvm_cadastro.py` (cad_cia_aberta) | `cnpj` | 0005 | - |
| `v1-raw_b3_cadastro` | `collectors/b3_cadastro.py` (GetDetail por código CVM) | `cd_cvm` | 0005 | - |
| `v1-raw_b3_proventos` | `collectors/b3_proventos.py` | `registro_hash` | 0005 | - |
| `v1-raw_b3_suplemento` | `collectors/b3_proventos.py` (suplemento de proventos) | `registro_hash` | 0005 | - |
| `v1-raw_llm_juiza_runs` | `llm/juiza.py`: cada chamada paga (Jev ou LLM de fallback, OpenAI ou Gemini) e a run-resumo da concordância, inclusive as com falha | `id` | 0005; `custo_usd` e `finalidade` em 2026-10-23 | - |
| `v1-staging_juiza_tentativas` | `llm/juiza.py`: tentativas por par que a LLM de fallback não aceitou | `article_id`, `ticker`, `prompt_versao` | 2026-10-23 (`sql/alteracoes/2026-10-23_juiza_custo_tentativas.mysql.sql`) | - |
| `v1-staging_noticias_julgamento` | `llm/juiza.py` (julgamento por artigo e ticker) | `article_id`, `ticker`, `prompt_versao` | 0005 | - |

Na juíza, cada chamada paga (Jev ou LLM de fallback) gera uma run. O campo `modelo` das tabelas da juíza vale `regra` (regra barata), `jev-1.13.0/q1/t1500` (Jev; `t1500` é o `JEV_TRECHO` usado), `gpt-5.6-luna` (OpenAI), o nome do modelo do Gemini ou `codigo/r1/<camada>` (julgamento por código das notícias WordPress, sem run; ver [04](04-juiza-e-llm.md)). Os prompts ficam em `raw_llm_prompts`: `juiza` (Gemini) e `juiza_openai` (OpenAI), ambos na versão 1. Ver [04](04-juiza-e-llm.md), seções 2.3 e 2.6.

Coleta WordPress (alteração de 2026-10-27, `sql/alteracoes/2026-10-27_wordpress_historias.mysql.sql`, aplicada em 2026-10-27):

| Tabela | Escreve | Chave (PK) | Migration | Linhas |
|---|---|---|---|---|
| `v1-raw_wp_posts` | `collectors/wordpress.py`: posts candidatos a ticker (pré-filtro bruto), com o HTML do corpo | `site`, `wp_id` | 2026-10-27 | - |
| `v1-staging_historias` | `enrich/processar_wp.py`: a história a que cada artigo pertence (representante, ou duplicata do Google apontando para ele) | `article_id` | 2026-10-27 | - |
| `v1-staging_wp_processamento` | `enrich/processar_wp.py`: um registro por post processado, com a versão de regras e de prompt | `site`, `wp_id` | 2026-10-27 | - |

`v1-raw_wp_posts` é append-only (`update_columns=None`), por isso não entra na exceção da seção 3.3. As duas tabelas de staging são regravadas por upsert.

Carga histórica: unidade em curso e último erro por (ano, coletor) (alteração de 2026-10-27, `sql/alteracoes/2026-10-27_historico_progresso_unidade_erro.mysql.sql`, aplicada em 2026-10-27):

| Tabela | Escreve | Chave (PK) | Migration | Linhas |
|---|---|---|---|---|
| `v1-painel_historico_progresso` (colunas novas `unidade_atual`, `ultimo_erro_unidade`, `ultimo_erro_mensagem`) | `orquestrador/historico.py` (`ProgressoHistorico`): a unidade em processamento e o último erro de cada passo | `ano`, `coletor` | 2026-10-27 (aplicada em 2026-10-27) | - |

As duas trilhas paralelas da carga (Google e principal) precisam de uma posição por (ano, coletor): a posição única de `v1-painel_historico` não basta.

Entrega (migration 0006). Os nomes e as colunas seguem a spec em `spec-entrega-etapa-seguinte.md`. Quem escreve é
`entrega/builder.py`, que faz upsert e, em `v1-noticias`, remove as reprovadas. `v1-composicao_ibov` (alteração de 2026-10-23, `sql/alteracoes/2026-10-23_composicao_ibov.mysql.sql`) é escrita pelo builder e pelo coletor `b3_carteira_ibov`, na etapa `precos` do daily.

| Tabela | Chave (PK) | Linhas |
|---|---|---|
| `v1-noticias` | `id`, `ticker` | 4.980 |
| `v1-precos` | `data`, `ticker` | 48.561 (verificação de ponta a ponta, ver seção 7) |
| `v1-cadastro` | `ticker` | 88 |
| `v1-mudancas_ticker` | `ticker_antigo` | 10 |
| `v1-balancos` | `cnpj`, `tipo_documento`, `data_inicio_periodo`, `data_fim_periodo` | 247 |
| `v1-acoes_emitidas` | `cnpj`, `data_referencia` | 170 |
| `v1-proventos` | `ticker`, `tipo`, `data_com` | 8.392 |
| `v1-indices` | `data`, `indice` | 210 |
| `v1-composicao_ibov` | `ticker`, `vigencia_inicio` | - |
| `v1-composicao_ibov` | `ticker`, `vigencia_inicio` | ainda não gravada (a reconstrução local dá 133 linhas) |

As regras de origem de cada coluna das 9 tabelas estão em [Entrega](05-entrega.md).

### 3.3 Regra de atualização

Por padrão, o `bulk_upsert` (seção 5) grava só linhas novas: com `update_columns=None`, uma linha que já existe não
é sobrescrita. Estas tabelas atualizam linhas existentes:

| Tabela | O que é atualizado |
|---|---|
| `raw_prices` | `open`, `high`, `low`, `close`, `adj_close`, `volume`, `source` |
| `raw_macro` | `value` |
| `raw_articles` | `resolved_url`, por UPDATE em `enrich/article_fetcher.py` |
| `raw_cvm_cadastro` | `cd_cvm`, `denom_social`, `sit`, `setor_ativ`, `fetched_at` |
| `raw_b3_cadastro` | `cnpj`, `trading_name`, `issuing_company`, `industry_classification`, `other_codes`, `payload`, `fetched_at` |
| `staging_article_sentiment` | `prob_pos`, `prob_neu`, `prob_neg`, `text_source`, `scored_at` |
| `features_dataset_v1` | Colunas que não são chave (`atualizaveis` em `features/build.py`) |
| `staging_llm_*` | Apagadas por `data_referencia` e regravadas no mesmo passo (`llm/job.py`) |
| `v1-noticias`, `v1-precos` e demais entregas | Colunas que não são chave (`update_columns` em `entrega/builder.py`) |

Atenção: a regra de projeto "a camada `raw` é imutável e append-only" não vale literalmente para as linhas acima.
Ver seção 7.

## 4. Schema e alterações

Até 2026-10-20 o schema era versionado por Alembic (migrations 0001 a 0008). Com a unificação do banco, o Alembic foi
descontinuado: o schema atual fica em um arquivo SQL, e cada alteração é escrita e aplicada à mão.

### 4.1 Banco vazio

`sql/schema.mysql.sql` tem o schema completo do estado atual, com as 44 tabelas. Para criar um banco vazio, aplique esse
arquivo com um usuário que tem DDL. Ele não cria a tabela `alembic_version`. Banco que já tem as tabelas não precisa dele.

### 4.2 Alterar o schema

Toda alteração segue estes passos:

1. Mude o model em `src/tcc_scraper/db/`.
2. Atualize `sql/schema.mysql.sql` para refletir o estado novo.
3. Escreva o ALTER em `sql/alteracoes/AAAA-MM-DD_descricao.mysql.sql`, uma alteração por arquivo.
4. Com aprovação do dono (o banco é produção), aplique o ALTER à mão, da máquina local, com usuário que tem DDL, no
   banco do `.env`.

Não há mais `alembic check` para detectar divergência entre o model e o banco: a conferência é revisão manual.
`db-info` mostra o destino e confere se as tabelas existem; não compara colunas com o model.

**DDL não tem rollback no MySQL.** Uma alteração que quebra no meio deixa o banco parcialmente alterado, e o
`RENAME TABLE` também não é transacional. Antes de aplicar em banco com dados, confira o SQL e tenha backup. Reverter é
outra alteração escrita à mão (por exemplo, um `RENAME TABLE` de volta), com a mesma aprovação.

### 4.3 Executor

O executor no Railway não tem DDL e nunca aplica alteração de schema. Alterações saem sempre da máquina local.

### 4.4 Resquícios do Alembic

- A tabela `alembic_version` ainda existe no MySQL de produção, sem uso. Pode ser removida com
  `DROP TABLE alembic_version`, quando o dono aprovar. Ainda não foi removida.
- As revisões antigas do Alembic estão no histórico do git, anteriores à remoção da pasta `alembic/`.

## 5. Regras de tipo e de escrita

| Regra | Onde | Por quê |
|---|---|---|
| `sqlalchemy.Double`, nunca `Float` | Todos os modelos | `Float` vira 4 bytes no MySQL e truncaria as probabilidades do FinBERT em cerca de 7 dígitos, mudando o dataset conforme o banco. Há teste de regressão |
| `UTCDateTime`, sempre aware em UTC | `db/models.py` | O MySQL não guarda fuso. Sem isso, uma matéria das 22h cairia em outro dia. A conversão para `America/Sao_Paulo` só acontece na agregação diária |
| Sem `RETURNING` | `collectors/google_news.py` | O MySQL não tem `RETURNING`. A gravação é em lote, depois os ids são relidos pela chave natural (`url_hash`), e só então os vínculos são gravados. São três consultas por janela, qualquer que seja o número de matérias |
| Upsert em lote por `bulk_upsert` | `db/upsert.py` | Traduz para `INSERT ... ON DUPLICATE KEY UPDATE`, em blocos de 500 linhas. Deduplica dentro do lote mantendo a última ocorrência. Lança `NotImplementedError` fora de MySQL |
| Uma chave única relevante por tabela | `db/upsert.py` | O MySQL dispara o conflito em qualquer chave única violada, não só na indicada em `index_elements`. Uma segunda chave única exige revisar o módulo |
| `update_columns=None` preserva a linha | `db/upsert.py` | O MySQL não tem "do nothing". O código atribui a primeira coluna da chave a si mesma, o que só neutraliza o conflito. `INSERT IGNORE` não é usado porque engole erro de truncagem |
| Dedupe por `url_hash` | `db/models.py` (`canonical_url`, `url_hash`) | A URL crua varia por querystring de rastreamento |
| Texto longo em `LONGTEXT` | Antigas migrations 0003 e 0005 | `TEXT` no MySQL tem 64 KB; uma resposta do Gemini passa disso |
| Índice em `url_hash` | Antigas migrations 0001 e 0008 | Exige MySQL 8 com `utf8mb4` |

**Collation e `LIKE`.** A collation `utf8mb4_0900_ai_ci` ignora acento: `WHERE title LIKE '%ã%'` traz mais linhas
que uma busca sensível a acento. Para busca sensível, force a collation:

```sql
SELECT * FROM `raw_articles` WHERE title LIKE '%ã%' COLLATE utf8mb4_0900_as_cs;
```

Nenhuma consulta do pipeline usa `LIKE`, então o dataset não é afetado. A pegadinha vale para consultas de análise.

## 6. Testes com banco

A suíte roda sem banco por padrão. Os testes que gravam no MySQL do `.env` são **opt-in**:

```bash
TCC_TESTES_COM_BANCO=1 pytest        # PowerShell: $env:TCC_TESTES_COM_BANCO=1; pytest
```

Sem a variável, esses testes são pulados com a mensagem `teste com banco desligado`. Com ela, gravam no banco real.
Por isso, cada teste limpa só as linhas que criou, pelos prefixos `TESTE-`, `REGR`, `t27.`, `ZZZZ9` e pelas datas de
2099, antes e depois da execução. A fixture `engine` fica em `tests/conftest.py`. Os testes que usam banco incluem
`tests/test_entrega_banco.py` e `tests/test_juiza.py`.

## 7. Pontos em aberto e limitações

- **Preços: 48.564 em `raw_prices` e 48.561 em `v1-precos`.** `raw_prices` tem 48.564 linhas: é a coleta, que inclui IDIV, SMLL, IFIX e os símbolos macro. `v1-precos` tem 48.561 linhas, montadas na verificação de ponta a ponta, no Postgres local, antes da migração para o banco único.
- **`raw_b3_cadastro.cd_cvm` vira `AUTO_INCREMENT`.** A PK é um `Integer` simples, e o SQLAlchemy gera
  `AUTO_INCREMENT` no DDL (ver `sql/schema.mysql.sql`). É inofensivo, porque o coletor sempre informa
  o `cd_cvm`. A correção certa é `autoincrement=False` no modelo e um ALTER em `sql/alteracoes/`. Não foi feita.
- **"Raw append-only" não é literal.** Há UPDATEs em `raw_prices`, `raw_macro`, `raw_articles.resolved_url`, `v1-raw_cvm_cadastro` e `v1-raw_b3_cadastro` (seção 3.3). São reingestões e preenchimento de campo, não reescrita de histórico. O `CLAUDE.md` (seção 5) registra essa exceção.
- **Nenhum servidor MySQL de teste.** Alterações de schema e testes com banco rodam contra o MySQL do `.env`. A paridade do
  dataset entre dois bancos, que existia com PostgreSQL, não foi reexecutada depois da migração para banco único.
- **Números de linhas da seção 3.** Os valores vêm do ponta a ponta (T30), executado na noite de 2026-10-19 para
  2026-10-20 no PostgreSQL local, antes da migração para o MySQL único. Não foram lidos do MySQL de produção.
- **Schema de produção.** O MySQL de produção está com o schema atual, equivalente à antiga migration 0008, aplicada pelo
  dono. A tabela `alembic_version` ainda existe, sem uso (seção 4.4).
