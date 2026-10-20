# Executor e painel

O scraper roda no Railway como **worker sempre ligado**: `python -m tcc_scraper.cli executor` (ou
`tcc-scraper executor`). Não é um cron. O executor recebe comandos do painel, roda o `daily` de 9 etapas
(agenda das 18:30 BRT) e a carga histórica, e grava o progresso em tabelas `v1-painel_*` do MySQL, que a API
do painel lê.

## 1. Fluxo

```
painel -> API do painel -> v1-painel_comandos (fila)
                                 | POST /api/executor/comandos (a cada EXECUTOR_POLL_S)
                            executor -> daily / etapa / historico
                                 | grava
                            MySQL v1-painel_* -> lido pela API -> painel
```

- Contrato: `interface-acompanhamento/docs/contrato-api.md`. As três rotas do executor são `POST`.
- A API **nunca** chama o scraper. Ela grava o comando na fila; o executor busca com
  `POST /api/executor/comandos` (corpo `{}`) e confirma com `POST /api/executor/comandos/ack`
  (`id`, `ok`, `erro` no corpo). `204` confirma; `404` e `409` contam como já tratado e não são
  reenviados. Decisão registrada em [ADR.md](ADR.md).
- Entrega "pelo menos uma vez": o mesmo comando pode voltar. Ids já aplicados (`v1-painel_comandos_aplicados`)
  são ignorados e reconfirmados com `ok: true`.
- Heartbeat a cada `EXECUTOR_HEARTBEAT_S` em `POST /api/executor/heartbeat`, espelhado em `v1-painel_heartbeat`.
- Autenticação: `Authorization: Bearer <EXECUTOR_TOKEN>`.

## 2. Variáveis do serviço

| Variável | Para quê |
|---|---|
| `PAINEL_API_URL` | Base da API do painel, sem barra final |
| `EXECUTOR_TOKEN` | Bearer da API; igual ao `.env` da API do painel |
| `EXECUTOR_POLL_S` | Intervalo entre buscas de comandos (padrão 5) |
| `EXECUTOR_HEARTBEAT_S` | Intervalo do heartbeat (padrão 15) |
| `EXECUTOR_HOST` | Nome deste executor no painel; vazio = nome da máquina |
| `DAILY_HORA` | Horário do daily automático, HH:MM no `DAILY_TZ` (padrão `18:30`) |
| `DAILY_TZ` | Fuso de `DAILY_HORA` e do dia (padrão `America/Sao_Paulo`) |
| `DAILY_ORCAMENTO_MIN` | Orçamento, em minutos, para as etapas de coleta (padrão 90) |
| `DAILY_PRAZO_HORA` | Hora limite, no `DAILY_TZ`, para publicar o dia (padrão 20) |
| `DATA_FIM_COLETA` | Deixar **vazio**: o fim da coleta é hoje. Fixar só para reproduzir um dia. Não muda a janela do v1 (`DATA_START`/`DATA_END`) |
| `IP_BANCO`, `USER_BANCO`, `SENHA_BANCO`, `PORTA_BANCO`, `NOME_BANCO` | Banco do painel e do dataset (o único do projeto: MySQL 8) |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Etapa `analise`; sem chave, ela sai `pulada` |
| `JUIZA_MODELO`, `JUIZA_MAX_ITENS_DIA`, `JUIZA_LOTE`, `JUIZA_CONFIANCA_MINIMA` | Juíza de qualidade (etapa `juiza`) |
| `ENTREGA_FILTRO_JUIZA` | Se `true`, a entrega filtra pela juíza (padrão `false`) |
| `EXPORT_DIR` | Pasta do Parquet e dos CSVs (padrão `exports/`). Efêmera no Railway |
| `JEV_ENABLED`, `JEV_API_KEY`, `JEV_MODELO` | Fonte JEV; desligada por padrão (`JEV_ENABLED=false`) |
| `PAINEL_LOGS_DIAS` | Dias de `v1-painel_logs` mantidos (padrão 7) |

## 3. Comandos, prioridade e agenda

| Comando (`tipo`) | Parâmetros | O que faz |
|---|---|---|
| `daily` | `{}` | Roda as 9 etapas em ordem (seção 5) |
| `etapa` | `{"etapa": "noticias"}` | Roda uma etapa do catálogo, sozinha |
| `historico` | `{"anos": [2024, 2025], "modo": "pendentes"}` | Carga histórica por ano e coletor (seção 3.1). `modo`: `pendentes` coleta o que falta; `reprocessar_erros` refaz as unidades com erro |
| `pausar` / `retomar` / `cancelar` | `{}` | Controlam a carga histórica entre unidades (coletor × ano) |

- **Uma execução por vez.** Com um daily ou etapa em andamento, um novo `daily`/`etapa` é recusado
  (`ack ok: false`, motivo no `erro`).
- **Carga histórica tem prioridade.** Enquanto `historico` estiver `em_andamento` ou `pausando`, um daily
  que chegar nasce `agendada` com `aguardando = historico` e começa sozinho quando a carga pausar, terminar
  ou for cancelada.
- **Agenda.** Às `DAILY_HORA` (18:30 BRT) o executor cria o daily automático, com origem `cron`, uma vez por
  dia. Id da execução no formato `2026-10-19-daily-183000` (data, tipo, hora da criação).
- **Orçamento e prazo.** O daily tem `DAILY_ORCAMENTO_MIN` minutos. Estourado o orçamento, as etapas de coleta
  restantes saem `pulada` (`orçamento esgotado`); `entrega` e `analise` rodam mesmo assim. `prazo_em`
  (`DAILY_PRAZO_HORA`, 20:00 SP) é a hora-alvo para o dia estar publicado, e aparece no painel.

### 3.1 Carga histórica

Dispara pelo painel (`historico`) ou pela CLI: `python -m tcc_scraper.cli historico --anos 2025 --anos 2026
--modo pendentes`. A CLI é bloqueante: espera a carga terminar. Roda em thread própria, dentro do executor.

- **Segmentos.** Um segmento é um ano civil dentro de `[inicio_noticias, fim_coleta + 1 dia]`, do mais
  recente ao mais antigo. `inicio_noticias` é `data_start` menos 31 dias. Anos pedidos fora dessa faixa
  são ignorados.
- **Ordem dos passos.** No primeiro ano (o mais recente), vêm os globais: `yahoo`, `bcb`, `indices_bcb`,
  `cvm_cadastro`, `b3_cadastro`, `b3_proventos`, `cvm_itr_dfp`. Depois, ano a ano: `google_news`,
  `cvm_ipe`, `enriquecimento`, `finbert`, `juiza`, `build_entrega`.
- **Unidades e progresso.** A unidade de trabalho é coletor × ano. O progresso fica em
  `v1-painel_historico_progresso` (um registro por ano e coletor); a posição atual, em `v1-painel_historico`.
- **Pausa e cancelamento.** Checados **entre** unidades, nunca no meio de uma requisição. `pausar` vai para
  `pausando` e termina em `pausada`. `cancelar` vai para `cancelando` e termina em `cancelada`; numa carga
  já `pausada` (sem thread rodando), vai direto para `cancelada`. Os coletores são retomáveis por checkpoint
  (`v1-raw_collection_runs`), então retomar recomeça a unidade interrompida.
- **Modos.** `pendentes` roda o que falta. `reprocessar_erros` refaz as unidades com erro.
- **Ritmo.** `itens_por_min` é uma média móvel de 10 minutos.
- **Retomada após reinício.** Ao subir, o executor retoma sozinho uma carga `em_andamento` ou `pausando`
  (log: `carga ... retomada após reinício`). Uma carga `cancelando` vira `cancelada`. Uma `pausada` continua
  pausada até um comando `retomar`.
- **Id.** Formato `hist-20261019-2105` (data e hora de criação).

## 4. Tabelas `v1-painel_*`

No banco, estas tabelas se chamam `v1-painel_*` (prefixo `v1-`, migration 0008). Em SQL escrito à mão, o
nome precisa de crases: ``SELECT * FROM `v1-painel_logs` ``. As 8 tabelas de entrega (`v1-noticias` … `v1-indices`) seguem a mesma regra; o CSV de entrega
continua com o nome de arquivo da spec, sem prefixo.

Fonte: `db/models_painel.py` (migration 0004). O executor grava tudo, exceto `v1-painel_comandos`, que a API
cria e confirma (`ack_em`, `ok`, `erro`). Ponto flutuante é `Double`; datas são `UTCDateTime` (UTC); status são
strings, sem Enum do banco.

- `v1-painel_comandos` (escreve a API): `id` PK String(36), `tipo` String(16), `parametros` JSON, `criado_em`,
  `ack_em`, `ok`, `erro`. Índice em `ack_em`.
- `v1-painel_comandos_aplicados` (executor): `id` PK String(36), `aplicado_em`, `ok`, `erro`. Idempotência.
- `v1-painel_heartbeat` (executor): `host` PK String(64), `versao`, `ocupado_com` (`daily` | `historico` | null),
  `visto_em`.
- `v1-painel_execucoes` (executor): `id` PK String(48), `tipo` (`daily` | `etapa`), `etapa_alvo`, `origem`
  (`cron` | `manual`), `status` (`agendada` | `em_andamento` | `concluida` | `parcial` | `falhou`),
  `aguardando` (null | `historico`), `iniciada_em`, `finalizada_em`, `prazo_em`, `erro`, `atualizada_em`.
  Índice em `iniciada_em`.
- `v1-painel_etapas` (executor): PK (`execucao_id`, `etapa_id`); FK `execucao_id` → `v1-painel_execucoes.id`
  `ON DELETE CASCADE`. `ordem`, `status` (`pendente` | `em_andamento` | `concluida` | `falhou` | `pulada`),
  `unidades_total` (null até o plano), `unidades_feitas`, `unidades_erro`, `unidades_saturadas`, `itens`,
  `iniciada_em`, `finalizada_em`, `ultimo_erro_unidade`, `ultimo_erro_mensagem`, `atualizada_em`.
- `v1-painel_historico` (executor): `id` PK String(32) (ex.: `hist-20261019-2105`), `status`
  (`em_andamento` | `pausando` | `pausada` | `cancelando` | `cancelada` | `concluida` | `falhou`), `modo`
  (`pendentes` | `reprocessar_erros`), `anos` JSON, `ano_atual`, `coletor_atual`, `unidade_atual`,
  `iniciada_em`, `finalizada_em`, `itens_por_min`, `erro`, `atualizada_em`.
- `v1-painel_historico_progresso` (executor): PK (`ano`, `coletor`); `unidades_total`, `unidades_feitas`,
  `unidades_erro`, `itens`, `status_segmento` (`concluido` | `em_andamento` | `pausado` | `pendente` |
  `falhou`), `atualizado_em`.
- `v1-painel_logs` (executor): `id` BigInteger PK autoincrement, `ts`, `nivel` (`info` | `aviso` | `erro`),
  `origem` (`daily` | `historico`), `etapa`, `mensagem`. Índice em `ts`. Sem FK: o log sobrevive à limpeza.
  Retenção por `PAINEL_LOGS_DIAS`.

## 5. Etapas do daily e coletores

Catálogo em `orquestrador/etapas.py` (`EtapaDef`). Os ids e os nomes são o contrato com o painel: não renomear
sem combinar com ele.

| # | Etapa (id) | Nome no painel | Coletor | O que faz |
|---|---|---|---|---|
| 1 | `precos` | Preços e índices | `yahoo` | Preços do Yahoo (ações, Ibovespa, Brent, dólar) |
| 2 | `macro` | Macro do BCB | `bcb` | Selic e PTAX; depois CDI e IPCA do ano corrente (`indices_bcb`) |
| 3 | `noticias` | Notícias | `google_news` | Google News de hoje − 2 dias a hoje + 1 |
| 4 | `ipe` | Fatos relevantes | `cvm_ipe` | Fatos relevantes (IPE) do ano corrente, gravados como notícias |
| 5 | `enriquecimento` | Corpo e sentimento | `finbert` | Corpo dos artigos + FinBERT. Sem o extra `[sentiment]`, falha sem derrubar o daily |
| 6 | `juiza` | Juíza de qualidade | `juiza` | Julga os pares pendentes: regra, Jev (desligado) e Gemini, sem busca na web |
| 7 | `cadastro` | Cadastro e eventos | `cvm_b3` | CVM e B3 (cadastro), proventos da B3 e ITR/DFP do ano corrente (`cvm_itr_dfp`) |
| 8 | `entrega` | Montagem das 8 tabelas | `build_entrega` | Monta as 8 tabelas da entrega; falha se alguma tabela falhar |
| 9 | `analise` | Análise diária | `gemini` | `run_daily_analysis(hoje)` |

A carga histórica (seção 3.1) usa os mesmos coletores desta tabela, com os nomes de passo listados lá.

## 6. Ambiente de desenvolvimento

```
cp .env.example .env                     # IP_BANCO etc. do MySQL do projeto; PAINEL_API_URL=http://localhost:8077; EXECUTOR_TOKEN=dev-executor
pip install -e ".[dev,llm]"              # [sentiment] opcional local
python -m tcc_scraper.cli db-info        # confere o banco (so leitura)
python -m tcc_scraper.cli executor       # worker; em outro terminal, a API do painel (MODO=live, mesmo banco, mesmo EXECUTOR_TOKEN)
python -m tcc_scraper.cli daily --etapa precos   # atalho sem painel
```

Não há banco de desenvolvimento: o executor local grava no MySQL do `.env`, que é o banco real
do TCC. Migrations só com `alembic upgrade head`, seguindo [MIGRACAO-MYSQL.md](MIGRACAO-MYSQL.md), seção 4.

## 7. Deploy no Railway

- Serviço criado a partir deste repositório. O `railway.json` manda construir pelo `Dockerfile`, sem cron e
  sem healthcheck (é worker, não serve HTTP). Restart `ON_FAILURE`, até 10 tentativas.
- Imagem: torch CPU (índice do PyTorch, sem CUDA). Os modelos do FinBERT são baixados no build, em
  `/app/.hf_cache` (`HF_CACHE_DIR`), então a subida do serviço não baixa modelo.
- Variáveis: no painel do Railway, nunca no repositório. `EXECUTOR_TOKEN` tem que ser igual ao da API do painel.
- Migrations: `alembic upgrade head` roda da máquina local, com um usuário que tem DDL. O usuário do executor
  não tem DDL; nunca rode migration no serviço. Banco vazio sem Alembic: ver `sql/completo_ate_0008.mysql.sql`.
- MySQL remoto e usuário: ver [MIGRACAO-MYSQL.md](MIGRACAO-MYSQL.md).

## 8. Limitações

- **Um processo.** O estado de execução (a thread do daily e a da carga histórica) fica em memória. Não escale
  para mais de um executor no mesmo banco.
- **Disco efêmero.** Parquet e CSV gerados no contêiner somem no próximo deploy. Para CSV, rode
  `build-entrega --csv` localmente.
- **Reinício do daily.** Um daily em andamento quando o processo cai vira `falhou` (`interrompida por reinício do
  executor`). Não é retomado: dispare de novo pelo painel. (A carga histórica, sim: ver seção 3.1.)
- **Sem o extra `[sentiment]`** (instalação local mínima), a etapa `enriquecimento` falha e o daily sai
  `parcial`. A imagem do Railway tem o extra.
- **Fonte JEV** desligada: a juíza roda sem ela (ver a ADR "Fonte JEV implementada, mas desligada").
