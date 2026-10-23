# Operação e deploy: ambiente, CLI, variáveis, Railway, checklist de produção e problemas conhecidos

Este documento cobre como rodar o projeto no dia a dia: o ambiente de desenvolvimento, os 20 comandos do CLI com suas
opções, todas as variáveis de ambiente, o deploy do executor no Railway, a ordem de entrada em produção e os
problemas conhecidos com a solução de cada um. O banco está descrito em [Banco de dados](06-banco-de-dados.md); o
executor e o painel, em [Executor e painel](07-executor-e-painel.md).

## 1. Ambiente de desenvolvimento

Não existe banco local. O executor e os comandos locais gravam no MySQL do `.env`, que é o banco real do TCC. Por
isso, teste com cuidado e leia a seção 6 do [Banco de dados](06-banco-de-dados.md) antes de rodar testes com banco.

```bash
python -m venv .venv
.venv\Scripts\activate                       # Linux: source .venv/bin/activate
pip install -e ".[dev,llm]"                  # [sentiment] so se a maquina vai rodar o FinBERT (torch, ~2 GB)
cp .env.example .env                         # Windows: copy .env.example .env
python -m tcc_scraper.cli db-info            # confere o banco (so leitura)
```

No `.env`, preencha `IP_BANCO`, `USER_BANCO`, `SENHA_BANCO`, `PORTA_BANCO` e `NOME_BANCO`. Para testar o executor
com o painel local, use `PAINEL_API_URL=http://localhost:8077` e um `EXECUTOR_TOKEN` igual ao da API.

```bash
python -m tcc_scraper.cli executor           # worker; em outro terminal, a API do painel em MODO=live
python -m tcc_scraper.cli daily --etapa precos   # atalho sem painel: roda uma etapa e imprime o resumo
```

Testes e lint:

```bash
pytest                                       # sem rede e sem banco (fixtures em tests/fixtures/)
TCC_TESTES_COM_BANCO=1 pytest                # grava no MySQL do .env; ver 06, secao 6
ruff check src tests                         # line-length 100; regras E, F, I, UP, B, SIM
```

Scripts fora do CLI, em `scripts/`:

| Script | Para quê |
|---|---|
| `scripts/coleta-completa.sh` | Roda as etapas de coleta em sequência, com log por etapa, numa máquina com acesso direto ao banco. Fora do Railway. Não rode junto com o executor no mesmo banco |
| `scripts/gerar_mapas_b3_cvm.py` | Gera o mapa ticker para código CVM pelos endpoints da B3 (usa rede) |
| `scripts/capturar_fixtures.py` | Recaptura as fixtures reais de CVM, B3, BCB e Yahoo usadas nos testes. As fixtures não são editadas à mão |

## 2. Comandos do CLI

Todos os comandos são `python -m tcc_scraper.cli <comando>`, ou `tcc-scraper <comando>` após a instalação. São 20.
Opções repetíveis (`--anos`, `--tabela`) recebem a flag de novo para cada valor, porque o Typer não aceita lista.

### 2.1 Banco e diagnóstico

| Comando | Opções | O que faz |
|---|---|---|
| `db-info` | - | Mostra a qual banco o `.env` aponta e se as tabelas existem. Só leitura |
| `validate-tickers` | - | Confere se todos os tickers de `IBOV_TICKERS` respondem no Yahoo |
| `validate-cadastro` | - | Confere se cada ticker do universo tem código CVM e aparece na B3 com esse mesmo código. Deslistados (CRFB3, STBP3, SRNA3) saem como aviso, não como falha |

```bash
python -m tcc_scraper.cli db-info
python -m tcc_scraper.cli validate-tickers
python -m tcc_scraper.cli validate-cadastro
```

### 2.2 Pipeline de notícias e dataset v1

| Comando | Opções | O que faz |
|---|---|---|
| `collect-prices` | `--resume` / `--no-resume` (padrão: resume); `--limit N` | Preços do Yahoo: ações, Ibovespa, Brent e dólar. Grava `raw_prices`. `--limit N` coleta só os N primeiros, para testar |
| `collect-macro` | `--resume` / `--no-resume` | Selic (SGS 432) e dólar PTAX do Banco Central. Grava `raw_macro` |
| `collect-news` | `--resume` / `--no-resume`; `--limit N` (janelas); `--ticker T`; `--desde AAAA-MM-DD` e `--ate AAAA-MM-DD` (fim exclusivo) | Notícias históricas pelo Google News RSS. Dura 6 a 9 h na carga completa e é retomável. `--desde` e `--ate` devem ser usados juntos |
| `enrich` | `--limit N` (lotes) | Resolve o link de cada artigo e extrai o corpo dos artigos da allowlist. Grava `raw_article_bodies` e `raw_articles.resolved_url` |
| `score` | `--batch-size N` (padrão 32); `--limit N` | Sentimento FinBERT (pt-BR e en), roteado por idioma. Grava `staging_article_sentiment`. Exige o extra `[sentiment]` |
| `build-dataset` | `--export` / `--no-export` (padrão: export); `--persist` / `--no-persist` (padrão: persist); `--keep-unlabeled` / `--no-keep-unlabeled` (padrão: não) | Monta as 14 colunas do dataset v1, grava `features_dataset_v1` e exporta Parquet e CSV em `EXPORT_DIR` |

```bash
python -m tcc_scraper.cli collect-prices --limit 5
python -m tcc_scraper.cli collect-news --ticker PETR4 --desde 2026-01-01 --ate 2026-02-01
python -m tcc_scraper.cli enrich --limit 3
python -m tcc_scraper.cli score --batch-size 16 --limit 200
python -m tcc_scraper.cli build-dataset --no-export
```

A ordem de uso é `collect-prices`, `collect-macro`, `collect-news`, `enrich`, `score` e `build-dataset`. O
`collect-news` respeita o teto de 100 itens por consulta do Google News: ao bater o teto, subdivide a janela.

Para rodar tudo de uma vez, com log por etapa, em Linux: `nohup ./scripts/coleta-completa.sh > logs/coleta.log 2>&1 &` e depois `tail -f logs/coleta.log`. A coleta de notícias é retomável: rodar o mesmo comando de novo continua de onde parou. Rode-a sob `tmux` ou `nohup` para sobreviver à queda da sessão SSH.

### 2.3 Análise diária por LLM

| Comando | Opções | O que faz |
|---|---|---|
| `analyze-llm` | `--data AAAA-MM-DD` (padrão: hoje em `LLM_TZ`); `--dry-run` (imprime o prompt, não chama a API nem grava); `--modelo M` (sobrescreve `GEMINI_MODEL`) | Chama o Gemini com busca no Google e grava `raw_llm_runs` e as `staging_llm_*` do dia. Exige `GEMINI_API_KEY` e o extra `[llm]` |

```bash
python -m tcc_scraper.cli analyze-llm --dry-run
python -m tcc_scraper.cli analyze-llm --data 2026-10-19
```

### 2.4 Fontes oficiais (CVM e B3)

| Comando | Opções | O que faz |
|---|---|---|
| `collect-cvm` | `--anos ANO` (repetível); `--resume` / `--no-resume` | ITR e DFP da CVM: grava `v1-raw_cvm_balancos` e `v1-raw_cvm_acoes`. Ao mudar `CONTAS_CVM`, a ordem é: ALTER (se houver coluna nova), deploy, `collect-cvm --no-resume`, `build-entrega --tabela balancos` |
| `collect-ipe` | `--anos ANO` (repetível); `--resume` / `--no-resume` | Fatos relevantes (IPE) da CVM: grava `v1-raw_ipe_docs` e os artigos correspondentes em `raw_articles` |
| `collect-cadastro` | `--resume` / `--no-resume` | Cadastro das companhias: CVM (`cad_cia_aberta`) e B3 (GetDetail por código CVM). Grava `v1-raw_cvm_cadastro` e `v1-raw_b3_cadastro` |
| `collect-proventos` | `--resume` / `--no-resume` | Proventos (dividendos, JCP e outros) da B3. Grava `v1-raw_b3_proventos` e `v1-raw_b3_suplemento` |
| `collect-indices` | `--anos ANO` (repetível); `--resume` / `--no-resume` | CDI (SGS 12) e IPCA (SGS 433) do Banco Central. Grava em `raw_macro` |

```bash
python -m tcc_scraper.cli collect-cvm --anos 2025 --anos 2026
python -m tcc_scraper.cli collect-ipe --anos 2026
python -m tcc_scraper.cli collect-cadastro
python -m tcc_scraper.cli collect-proventos
python -m tcc_scraper.cli collect-indices --anos 2026
```

### 2.5 Juíza e entrega

| Comando | Opções | O que faz |
|---|---|---|
| `judge-news` | `--limite N` (teto de itens da rodada); `--backlog` (julga o passado, em ordem crescente, com teto em US$); `--orcamento-usd X` (teto extra em US$ da rodada, só com `--backlog`); `--anos ANO` (repetível, só com `--backlog`); `--status` (mostra pendentes, gasto e verbas, sem julgar); `--concordancia N` (mede a concordância Jev × Gemini em N pares, sem gravar julgamento); `--semente S` (semente da amostra de `--concordancia`, padrão 42) | Juíza de notícias: regra barata, Jev (se `JEV_ENABLED=true`) e Gemini sobre as pendentes. Sem `--backlog`, roda o modo diário, com teto mensal em US$. Grava `v1-raw_llm_juiza_runs`, `v1-staging_noticias_julgamento` e `v1-staging_juiza_tentativas` |
| `build-entrega` | `--csv` (grava também cada tabela em CSV); `--tabela NOME` (repetível, nomes como em `MODELOS`: `noticias`, `precos`, `cadastro`, `mudancas_ticker`, `balancos`, `acoes_emitidas`, `proventos`, `indices`, `composicao_ibov`) | Monta as 9 tabelas da entrega no banco. Os CSVs vão para `ENTREGA_CSV_DIR`. Com `--tabela`, monta só as indicadas |

```bash
python -m tcc_scraper.cli judge-news --limite 50
python -m tcc_scraper.cli judge-news --status
python -m tcc_scraper.cli judge-news --concordancia 200
python -m tcc_scraper.cli judge-news --backlog --limite 50 --anos 2025
python -m tcc_scraper.cli build-entrega --csv
python -m tcc_scraper.cli build-entrega --tabela noticias --tabela precos
```

O CSV da entrega é gerado localmente. No Railway, o disco é efêmero: rode `build-entrega --csv` na sua máquina.

### 2.6 Operação do executor

| Comando | Opções | O que faz |
|---|---|---|
| `executor` | - | Worker do painel: busca comandos, roda o `daily` e a carga histórica e confirma cada comando. Fica em loop. É o processo que roda no Railway |
| `daily` | `--etapa ID` (roda só uma etapa; ids em [Executor e painel](07-executor-e-painel.md), seção 5); `--origem TEXTO` (padrão `manual`) | Roda o daily agora, de forma bloqueante, e imprime o resumo. Não passa pela fila do painel |
| `historico` | `--anos ANO` (repetível; ex.: `--anos 2026 --anos 2025`); `--modo pendentes` ou `reprocessar_erros` (padrão `pendentes`) | Carga histórica: dispara e espera, de forma bloqueante, a coleta dos anos pedidos. Roda dentro do executor |

```bash
python -m tcc_scraper.cli executor
python -m tcc_scraper.cli daily --etapa noticias --origem manual
python -m tcc_scraper.cli historico --anos 2026 --anos 2025 --modo pendentes
```

## 3. Variáveis de ambiente

Cada variável é o nome do campo de `Settings` em `src/tcc_scraper/config.py`, em maiúsculas. Vazia ou ausente
usa o padrão, porque o `Settings` tem `env_ignore_empty=True`. "No `.env.example`" indica se a chave está documentada
ali. Os caminhos de `EXPORT_DIR`, `ENTREGA_CSV_DIR` e `HF_CACHE_DIR` são padrões que dependem da pasta do projeto.

### 3.1 Banco

| Variável | Padrão | No `.env.example` | Uso |
|---|---|---|---|
| `GEMINI_API_KEY` | vazio | sim | Chave do Gemini. Sem ela, `analyze-llm` falha com mensagem clara |
| `GEMINI_MODEL` | `gemini-3.8-flash` | sim | Modelo da análise diária. Só Gemini 3 ou superior aceita busca e JSON schema juntos |
| `LLM_TIMEOUT_S` | `180` | sim | Timeout de cada chamada |
| `LLM_MAX_ATTEMPTS` | `3` | sim | Tentativas por chamada |
| `LLM_TZ` | `America/Sao_Paulo` | sim | Fuso da data de referência da análise diária |
| `JUIZA_MODELO` | `gemini-3.1-flash-lite` | sim | Modelo da juíza de notícias |
| `JUIZA_MAX_ITENS_DIA` | `2000` | sim | Teto de pares pagos na etapa diária. Junto com o teto em US$, protege o custo |
| `JUIZA_ORCAMENTO_MENSAL_USD` | `3.0` | sim | Teto em US$ da juíza diária, por mês |
| `JUIZA_BACKLOG_VERBA_USD` | `15.0` | sim | Verba única, em US$, do backlog (cumulativa entre execuções) |
| `JUIZA_LOTE` | `50` | sim | Itens por lote enviado à juíza |
| `JUIZA_PAGINA` | `5000` | sim | Pares lidos do banco por página |
| `JUIZA_MAX_TENTATIVAS` | `3` | sim | Tentativas por par antes de sair da fila |
| `JUIZA_CONFIANCA_MINIMA` | `0.5` | sim | Confiança mínima do Jev para aceitar, com `JUIZA_JEV_DECIDE=true`. Não filtra o Gemini |
| `JUIZA_JEV_DECIDE` | `false` | sim | `true`: o Jev decide. `false`: o Jev só aceita descartes. Ligar só depois de `judge-news --concordancia` (seção 5.1) |
| `JUIZA_CONFIANCA_TRIAGEM` | `0.9` | sim | Confiança mínima do Jev para aceitar um descarte, com `JUIZA_JEV_DECIDE=false` |
| `JUIZA_PRECO_ENTRADA_USD_MTOK`, `JUIZA_PRECO_SAIDA_USD_MTOK` | `0.25`, `1.50` | sim | Preço do Gemini da juíza, em US$ por milhão de tokens |
| `JEV_ENABLED` | `false` | sim | Liga o Jev. Sem `JEV_API_KEY`, não roda |
| `JEV_API_KEY` | vazio | sim | Chave do Jev |
| `JEV_MODELO` | `jev-1.13.0` | sim | Modelo do Jev, fixado. Nunca `latest` |
| `JEV_PRECO_ENTRADA_USD_MTOK` | `0.042` | sim | Preço do Jev, em US$ por milhão de tokens de entrada. Saída grátis |
| `JEV_LOTE`, `JEV_CONCORRENCIA`, `JEV_TIMEOUT_S` | `50`, `8`, `30.0` | sim | Pares por lote, requisições em paralelo e timeout de cada uma |

### 3.5 Entrega, executor e daily

| Variável | Padrão | No `.env.example` | Uso |
|---|---|---|---|
| `ENTREGA_FILTRO_JUIZA` | `false` | sim | Se `true`, a entrega filtra pela juíza. Fica `false` no código até o backlog zerar (seção 5.1) |
| `PAINEL_API_URL` | vazio | sim | URL base da API do painel, sem barra final. Vazio = sem painel |
| `EXECUTOR_TOKEN` | vazio | sim | Bearer do executor. Tem que ser igual ao da API do painel |
| `EXECUTOR_POLL_S` | `5` | sim | Segundos entre buscas de comandos |
| `EXECUTOR_HEARTBEAT_S` | `15` | sim | Segundos entre batimentos |
| `EXECUTOR_HOST` | vazio = nome da máquina | sim | Nome deste executor no painel |
| `PAINEL_LOGS_DIAS` | `7` | sim | Dias de `v1-painel_logs` mantidos |
| `DAILY_HORA` | `08:00` | sim | Horário-alvo do daily automático (HH:MM, no `DAILY_TZ`) |
| `DAILY_TZ` | `America/Sao_Paulo` | sim | Fuso do daily |
| `DAILY_ORCAMENTO_MIN` | `90` | sim | Orçamento das etapas de coleta, em minutos |
| `DAILY_PRAZO_HORA` | `20` | sim | Hora limite para publicar o dia, no `DAILY_TZ` |

### 3.6 Caminhos e outras

| Variável | Padrão | No `.env.example` | Uso |
|---|---|---|---|
| `EXPORT_DIR` | `exports/` na pasta do projeto | comentada | Pasta do Parquet e dos CSVs do dataset. Efêmera no Railway |
| `ENTREGA_CSV_DIR` | `exports/entrega/` | não | Pasta dos CSVs da entrega (`build-entrega --csv`) |
| `HF_CACHE_DIR` | `.hf_cache/` na pasta do projeto | não | Cache dos modelos do FinBERT. A imagem do Railway fixa `/app/.hf_cache` |

**GPU para o FinBERT.** Com `SENTIMENT_DEVICE=auto` (padrão) o FinBERT usa a GPU se o torch instalado tiver CUDA; senão roda na CPU. Para ter CUDA, instale o torch antes do projeto: `pip install torch --index-url https://download.pytorch.org/whl/cu124` e depois `pip install -e ".[llm,sentiment]"`. `SENTIMENT_DEVICE` aceita `auto`, `cuda`, `cuda:N` ou `cpu`; pedir `cuda` sem GPU falha com erro, em vez de cair na CPU. `SENTIMENT_BATCH_SIZE` (padrão 32; com GPU, 128 costuma caber em 6 GB de VRAM). O Dockerfile do Railway continua CPU.

## 4. Deploy no Railway

O scraper roda no Railway como **worker sempre ligado**: o serviço executa `python -m tcc_scraper.cli executor`. Não
é cron e não serve HTTP.

- **`railway.json`**: build pelo `Dockerfile`; política de reinício `ON_FAILURE`, com até 10 tentativas. Não há
  cron nem healthcheck.
- **`Dockerfile`** (`python:3.12-slim`):
  1. Instala torch da roda CPU do PyTorch (`https://download.pytorch.org/whl/cpu`). A wheel padrão traz CUDA, que
     o worker não usa.
  2. Instala o projeto com `pip install ".[llm,sentiment]"`. A instalação com o extra `sentiment` está na imagem do
     Railway; o `pyproject` deixa esse extra opcional.
  3. Baixa os modelos do FinBERT (pt e en) no build, em `HF_CACHE_DIR=/app/.hf_cache`. Assim a subida do serviço
     não baixa cerca de 900 MB a cada deploy.
  4. `CMD ["python", "-m", "tcc_scraper.cli", "executor"]`.
- **Variáveis**: no painel do Railway, nunca no repositório. Pelo menos: `IP_BANCO`, `USER_BANCO`, `SENHA_BANCO`,
  `PORTA_BANCO`, `NOME_BANCO`, `PAINEL_API_URL`, `EXECUTOR_TOKEN`, `GEMINI_API_KEY` e `JUIZA_*`, conforme o uso.
- **Banco remoto para o Railway.** O worker grava no MySQL do servidor pela internet. Crie um usuário próprio para o Railway, sem DDL:

  ```sql
  CREATE USER 'tcc_railway'@'%' IDENTIFIED BY '...';
  GRANT SELECT, INSERT, UPDATE, DELETE ON tcc_scraper.* TO 'tcc_railway'@'%';
  ```

  `DELETE` é necessário: as `staging_llm_*` são substituídas por dia. O host é `'%'` porque o Railway, no plano básico, não tem IP de saída fixo. Com a porta 3306 aberta a qualquer origem:
  - restrinja a 3306 no firewall do servidor ao máximo possível;
  - confirme que o `bind-address` permite conexão remota;
  - se o servidor tiver certificado, considere `REQUIRE SSL` no usuário;
  - use senha forte e única (32 caracteres aleatórios ou mais) e nunca a grave em arquivo versionado.

  O usuário `tcc`, com DDL, continua sendo o da máquina local.
- **Alterações de schema não rodam no serviço.** O usuário do executor não tem DDL. Os ALTERs de `sql/alteracoes/`
  são aplicados da máquina local (seção 5).
- **Um processo só.** O estado do daily e da carga histórica fica em memória. Não escale para mais de um executor no
  mesmo banco.
- **Disco efêmero.** Parquet e CSV gerados no contêiner somem no próximo deploy.

## 5. Checklist de entrada em produção

Esta é a ordem. Cada passo depende do anterior.

1. **Confirmar o servidor.** MySQL 8.0 ou superior, banco `tcc_scraper` com `utf8mb4`. Fazer backup antes de
   qualquer DDL. Ver [Banco de dados](06-banco-de-dados.md), seções 1 e 4.
2. **Conferir o schema.** `sql/schema.mysql.sql` tem as 41 tabelas do estado atual (14 sem prefixo e 27
   com `v1-`). O MySQL de produção ainda não recebeu os ALTERs de 2026-10-23: são o passo 1 da seção 5.1. Confira com `python -m tcc_scraper.cli db-info`, que só lê. Para banco vazio, aplique
   `sql/schema.mysql.sql` com usuário que tem DDL. Alterações futuras seguem [06](06-banco-de-dados.md), seção 4.2.
3. **Conferir o `.env` local.** Pendência do dono: remover `DB_TARGET` e `DATABASE_URL`, variáveis do antigo
   PostgreSQL. O código não as lê, mas elas confundem. O `.env` não é versionado.
4. **Deploy do executor no Railway.** Variáveis da seção 4, com `EXECUTOR_TOKEN` igual ao da API. Confirme nos logs
   que o worker sobe e envia heartbeat.
5. **Bloqueio do modo live.** Antes de ligar a API em `MODO=live`: no repositório do painel (`interface-acompanhamento`), as rotas `/api/executor/*` respondem `501`, porque `FilaComandosMySQL` (`api/app/fila.py`) e `fontes/mysql.py` (`api/app/fontes/mysql.py`) ainda são TODO. Enquanto isso, o executor não recebe comandos da API live: o loop segue rodando, sem aplicar nada. Só avance quando essas duas peças estiverem implementadas do lado do painel. Detalhes em [Executor e painel](07-executor-e-painel.md), seção 9.
6. **API do painel em `MODO=live`.** Mesmo banco e mesmo `EXECUTOR_TOKEN`. Confirme `executor: ok` em
   `/api/componentes`.
7. **Primeiro daily pelo painel.** Dispare um daily (ou só a etapa `precos`) e confirme que a execução termina em `concluida` ou
   `parcial`, com as 9 etapas e `unidades_total` preenchido. O daily automático roda às `DAILY_HORA` (08:00).

Carga histórica: depois do passo 7, dispare `historico` pelo painel, com `--anos` dos anos desejados. Ela tem
prioridade sobre o daily, e o daily espera a carga pausar ou terminar.

### 5.1 Juíza: julgar 100% dos pares (2026-10-23)

Passo a passo para levar a juíza ao estado novo: julga todos os pares, com teto em US$, e filtra a entrega. Cada passo depende do anterior. O DDL é aplicado à mão, com usuário com DDL, backup e aprovação do dono ([06](06-banco-de-dados.md), seção 4.2). A partir do passo 2, tudo é feito pelo painel: as ações e a tela **Qualidade** de 2026-10-23 estão em [Executor e painel](07-executor-e-painel.md), seção 4.1.

1. **Aplicar o lote de 2026-10-23 à mão** no MySQL do `.env`, da máquina local: `sql/lotes/2026-10-23_lote.mysql.sql`. Ele junta, na ordem, os três ALTERs de `sql/alteracoes/2026-10-23_*`: a coluna `raw_articles.published_time_source`, as colunas `custo_usd` e `finalidade` em `v1-raw_llm_juiza_runs` com a tabela `v1-staging_juiza_tentativas`, e a tabela `v1-composicao_ibov`. Rode uma vez só: DDL do MySQL não tem rollback. O fim do arquivo traz a conferência, que deve devolver 5 linhas.
2. **Subir o código novo.** Só depois do passo 1. Se a juíza roda no Railway, as variáveis `JEV_*` e `JUIZA_*` vão no painel do Railway, e o executor não aplica ALTER.
3. **Conferir pelo painel.** Na tela **Qualidade**, o card **Status da juíza** mostra `pendentes`, `esgotados`, o gasto do mês e o do backlog contra as verbas. Se uma seção aparecer em `erros` (por exemplo, coluna ainda inexistente), o lote não foi aplicado completo.
4. **Medir a concordância pelo painel.** Na tela **Qualidade**, o formulário "Medir concordância" (N e semente), com a confirmação "gasta API real (Jev + Gemini)". Exige `JEV_ENABLED=true`, `JEV_API_KEY` e `GEMINI_API_KEY` no executor; sem elas, a execução falha com a mensagem `concordancia exige o Jev` ou `concordancia exige o Gemini`. Anote a decisão, a concordância e o kappa de `papel` e de `tipo_conteudo`, e o custo do resumo. A medição não grava julgamento e não tem teto.
5. **Decidir o modo do Jev (dono).** Pela regra da seção 2.7: se o mínimo entre a concordância de `papel` e a de `tipo_conteudo` for pelo menos 0,85, a decisão é `jev_decide`, e o dono pode definir `JUIZA_JEV_DECIDE=true`. Senão, `JUIZA_JEV_DECIDE=false` (triagem). O painel só mostra a decisão; não altera a config.
6. **Julgar o backlog pelo painel.** Na tela **Faltantes**, a verificação "Pares sem julgamento" (`juiza_pendentes`): marque os anos e confirme. O painel envia o comando `coletar` com o item `{"coletor": "juiza", "anos": [...]}`, e a juíza roda em modo backlog, com a verba de US$ 15, que é cumulativa (o que a carga histórica já gastou conta). Para conferir o custo, comece por um ano só e olhe o card **Status da juíza** antes de seguir.
7. **Conferir.** O card **Status da juíza** mostra `pendentes`, `esgotados`, `gasto_backlog_usd` contra `verba_backlog_usd` e o gasto do mês. Repita o passo 6 até `pendentes` ficar perto de zero. Pares `esgotados` já gastaram as tentativas e saem da fila.
8. **Ligar o filtro da entrega.** Com `pendentes` perto de zero, defina `ENTREGA_FILTRO_JUIZA=true` (variável do executor, não é ação do painel) e monte a entrega pelo painel: na tela de disparo, a etapa `entrega` monta as 9 tabelas. O CSV continua sendo gerado localmente (`build-entrega --csv`), porque o disco do Railway é efêmero. Ligar o filtro antes de julgar o backlog derruba cerca de 92% da `v1-noticias`, porque o par sem julgamento fica de fora. Por isso o padrão em código só passa a `true` depois deste passo (a troca de padrão é a última fase do plano de 2026-10-23).

## 6. Problemas conhecidos e soluções

| Sintoma | Causa | Solução |
|---|---|---|
| Falha ao aplicar um ALTER de `sql/alteracoes/` | Erro de SQL no meio, ou o ALTER não bate com o estado do banco | Confira o estado do banco antes de reexecutar. DDL não tem rollback: tenha backup (doc 06, seção 4) |
| Erro de tamanho de coluna em `v1-raw_ipe_docs` | Dados reais da CVM maiores que os tamanhos antigos | Resolvido com o alargamento das colunas (antiga 0007). Se voltar, o valor é maior que os tamanhos atuais: novo ALTER em `sql/alteracoes/` |
| `IntegrityError` na chave de `raw_collection_runs` ao carregar dados antigos | Sequências do PostgreSQL atrás dos ids copiados | Não se aplica mais: o banco é só MySQL, com `AUTO_INCREMENT` |
| Busca em `LIKE` traz linhas a mais | A collation `utf8mb4_0900_ai_ci` ignora acento | Use `COLLATE utf8mb4_0900_as_cs` em consulta de análise. O pipeline não usa `LIKE` |
| Conexão cai no meio de uma coleta longa | O MySQL fecha conexão ociosa | `pool_recycle=3600` já está em `get_engine()`. Não remover |
| `enriquecimento` falha e o daily sai `parcial` | Instalação local sem o extra `[sentiment]` | Instale `pip install -e ".[sentiment]"`. A imagem do Railway já tem o extra |
| `daily` recusado | Já há um daily ou etapa em andamento (uma execução por vez) | Aguarde a execução atual terminar. O motivo vem no `erro` do ack |
| Daily nasce `agendada` e não começa | Carga histórica em `em_andamento` ou `pausando` tem prioridade | Esperado. O daily começa quando a carga pausar, terminar ou for cancelada |
| Daily virou `falhou` após reinício | O executor caiu com o daily em andamento | Dispare de novo pelo painel. A carga histórica, ao contrário, retoma sozinha |
| Coleta continua com a API do painel fora do ar | Ele não depende do painel para coletar | Esperado. Comandos e heartbeat voltam quando a API voltar |
| Google News devolve 100 itens em uma janela | O limite da consulta é de 100 itens | O coletor subdivide a janela sozinho. Para repetir um recorte, use `collect-news --desde ... --ate ...` |
| `NATU3` com série curta no Yahoo | O código novo tem histórico a partir de 2025-06 | Esperado. `items_found` denuncia a série curta |
| `CRFB3` sem código CVM e sem CNPJ | Companhia fechou capital e saiu da B3 | Esperado. Fica no universo só para preço e notícia. `validate-cadastro` trata como aviso |
| `historico --anos 2026 2025` não funciona | Opções repetíveis no Typer | Use `--anos 2026 --anos 2025` |
| `analyze-llm` falha sem chave | `GEMINI_API_KEY` vazia | Preencha a chave. O job exige a chave e falha com mensagem clara |
| `sql/completo_ate_*` ou `sql/0008_*` não existem mais | Arquivos gerados pelo Alembic, removidos na descontinuação | Use `sql/schema.mysql.sql` para banco vazio, e `sql/alteracoes/` para mudanças |
| Testes com banco apagam ou sujam dados | Eles gravam no MySQL real do `.env` | Só rode com `TCC_TESTES_COM_BANCO=1` quando aceitar essa gravação. Limpeza restrita aos prefixos de teste |

### 6.1 Não verificado

Estes pontos ficaram sem medição ou sem teste no fim do trabalho:

- **`docker build` não foi executado.** A imagem do Railway está escrita, mas a construção completa não foi testada.
- **Memória do FinBERT no Railway não foi medida.** O spike S4 previa `docker stats` e um plano B, ainda sem
  resultado.
- **Subida do executor no Railway não foi confirmada.** O passo 5 da seção 5 é o teste real.
- **`THROTTLE_LINK_RESOLVE` (0,4 s)** foi medido em 2026-09-21, com os números registrados no `.env.example`. A
  medição não foi repetida neste trabalho.
