# Executor e painel

O scraper roda no Railway como **worker sempre ligado**: `python -m tcc_scraper.cli executor`. Não é mais
um cron. O executor recebe comandos do painel, roda o `daily` de 9 etapas (agenda das 18:30 BRT) e a carga
histórica, e grava o progresso em tabelas `painel_*` do MySQL, que a API do painel lê.

## 1. Fluxo

```
painel -> API do painel -> painel_comandos (fila)
                                 | GET /api/executor/comandos (a cada EXECUTOR_POLL_S)
                            executor -> daily / etapa / historico
                                 | grava
                            MySQL painel_* -> lido pela API -> painel
```

- A API **nunca** chama o scraper. Ela grava o comando na fila; o executor busca por HTTP, aplica e confirma
  com `POST /api/executor/comandos/{id}/ack` (`ok`, `erro`).
- Entrega "pelo menos uma vez": o mesmo comando pode voltar. Ids já aplicados (`painel_comandos_aplicados`)
  são ignorados e reconfirmados com `ok: true`.
- Heartbeat a cada `EXECUTOR_HEARTBEAT_S` em `POST /api/executor/heartbeat`, espelhado em `painel_heartbeat`.
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
| `DATA_FIM_COLETA` | Deixar **vazio**: o fim da coleta é hoje. Fixar só para reproduzir um dia |
| `DB_TARGET=mysql` + `IP_BANCO`, `USER_BANCO`, `SENHA_BANCO`, `PORTA_BANCO`, `NOME_BANCO` | Banco do painel e do dataset |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Etapa `analise`; sem chave, ela sai `pulada` |
| `JUIZA_*` | Juíza de qualidade (etapa 6) |
| `JEV_*` | Fonte JEV; desligada por padrão (`JEV_ENABLED=false`) |
| `PAINEL_LOGS_DIAS` | Dias de `painel_logs` mantidos (padrão 7) |

## 3. Comandos, prioridade e agenda

| Comando (`tipo`) | Parâmetros | O que faz |
|---|---|---|
| `daily` | `{}` | Roda as 9 etapas em ordem (seção 5) |
| `etapa` | `{"etapa": "noticias"}` | Roda uma etapa do catálogo, sozinha |
| `historico` | `{"anos": [2024, 2025], "modo": "pendentes"}` | Carga histórica por ano e coletor. `modo`: `pendentes` coleta o que falta; `reprocessar_erros` refaz as unidades com erro |
| `pausar` / `retomar` / `cancelar` | `{}` (aceita `{"execucao_id": ...}`) | Controlam a carga histórica entre unidades (coletor × ano) |

**Carga histórica ainda não implementada.** Até ela entrar, `historico`, `pausar`, `retomar` e `cancelar`
são recusados com `carga histórica ainda não implementada` (`ack ok: false`).

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

## 4. Tabelas `painel_*`

Fonte: `db/models_painel.py` (migration 0004). O executor grava tudo, exceto `painel_comandos`, que a API
cria e confirma (`ack_em`, `ok`, `erro`). Ponto flutuante é `Double`; datas são `UTCDateTime` (UTC); status são
strings, sem Enum do banco.

- `painel_comandos` (escreve a API): `id` PK String(36), `tipo` String(16), `parametros` JSON, `criado_em`,
  `ack_em`, `ok`, `erro`. Índice em `ack_em`.
- `painel_comandos_aplicados` (executor): `id` PK String(36), `aplicado_em`, `ok`, `erro`. Idempotência.
- `painel_heartbeat` (executor): `host` PK String(64), `versao`, `ocupado_com` (`daily` | `historico` | null),
  `visto_em`.
- `painel_execucoes` (executor): `id` PK String(48), `tipo` (`daily` | `etapa`), `etapa_alvo`, `origem`
  (`cron` | `manual`), `status` (`agendada` | `em_andamento` | `concluida` | `parcial` | `falhou`),
  `aguardando` (null | `historico`), `iniciada_em`, `finalizada_em`, `prazo_em`, `erro`, `atualizada_em`.
  Índice em `iniciada_em`.
- `painel_etapas` (executor): PK (`execucao_id`, `etapa_id`); FK `execucao_id` → `painel_execucoes.id`
  `ON DELETE CASCADE`. `ordem`, `status` (`pendente` | `em_andamento` | `concluida` | `falhou` | `pulada`),
  `unidades_total` (null até o plano), `unidades_feitas`, `unidades_erro`, `unidades_saturadas`, `itens`,
  `iniciada_em`, `finalizada_em`, `ultimo_erro_unidade`, `ultimo_erro_mensagem`, `atualizada_em`.
- `painel_historico` (executor): `id` PK String(32) (ex.: `hist-20261019-2105`), `status`
  (`em_andamento` | `pausando` | `pausada` | `cancelando` | `cancelada` | `concluida` | `falhou`), `modo`
  (`pendentes` | `reprocessar_erros`), `anos` JSON, `ano_atual`, `coletor_atual`, `unidade_atual`,
  `iniciada_em`, `finalizada_em`, `itens_por_min`, `erro`, `atualizada_em`.
- `painel_historico_progresso` (executor): PK (`ano`, `coletor`); `unidades_total`, `unidades_feitas`,
  `unidades_erro`, `itens`, `status_segmento` (`concluido` | `em_andamento` | `pausado` | `pendente` |
  `falhou`), `atualizado_em`.
- `painel_logs` (executor): `id` BigInteger PK autoincrement, `ts`, `nivel` (`info` | `aviso` | `erro`),
  `origem` (`daily` | `historico`), `etapa`, `mensagem`. Índice em `ts`. Sem FK: o log sobrevive à limpeza.
  Retenção por `PAINEL_LOGS_DIAS`.

## 5. Etapas do daily e coletores

| # | Etapa (id) | Nome no painel | Coletor | Situação |
|---|---|---|---|---|
| 1 | `precos` | Preços e índices | `yahoo` | Pronta |
| 2 | `macro` | Macro do BCB | `bcb` | Pronta |
| 3 | `noticias` | Notícias | `google_news` | Pronta; janela curta em torno de hoje |
| 4 | `ipe` | Fatos relevantes | `cvm_ipe` | Placeholder (`pulada`) até a implementação |
| 5 | `enriquecimento` | Corpo e sentimento | `finbert` | Corpo dos artigos + FinBERT. Sem o extra `[sentiment]`, falha sem derrubar o daily |
| 6 | `juiza` | Juíza de qualidade | `juiza` | Placeholder (`pulada`) |
| 7 | `cadastro` | Cadastro e eventos | `cvm_b3` | Placeholder (`pulada`) |
| 8 | `entrega` | Montagem das 8 tabelas | `build_entrega` | Placeholder (`pulada`) |
| 9 | `analise` | Análise diária | `gemini` | `run_daily_analysis(hoje)` |

Na carga histórica, os coletores são `google_news`, `enriquecimento`, `finbert`, `yahoo`, `cvm_ipe`,
`cvm_itr_dfp` e `b3_proventos`. Os dois últimos não são etapas do daily.

## 6. Ambiente de desenvolvimento

```
docker compose up -d postgres            # ou powershell scripts\pg-start.ps1
cp .env.example .env                     # DB_TARGET=postgres; PAINEL_API_URL=http://localhost:8077; EXECUTOR_TOKEN=dev-executor
pip install -e ".[dev,mysql,llm]"        # [sentiment] opcional local
alembic upgrade head && python -m tcc_scraper.cli db-info
python -m tcc_scraper.cli executor       # worker; em outro terminal, a API do painel (MODO=live, mesmo banco, mesmo EXECUTOR_TOKEN)
python -m tcc_scraper.cli daily --etapa precos   # atalho sem painel
```

## 7. Deploy no Railway

- Serviço criado a partir deste repositório. O `railway.json` manda construir pelo `Dockerfile`, sem cron e
  sem healthcheck (é worker, não serve HTTP). Restart `ON_FAILURE`, até 10 tentativas.
- Imagem: torch CPU (índice do PyTorch, sem CUDA). Os modelos do FinBERT são baixados no build, em
  `/app/.hf_cache` (`HF_CACHE_DIR`), então a subida do serviço não baixa modelo.
- Variáveis: no painel do Railway, nunca no repositório. `EXECUTOR_TOKEN` tem que ser igual ao da API do painel.
- Migrations: `alembic upgrade head` roda da máquina local, com um usuário que tem DDL. O usuário do executor
  não tem DDL; nunca rode migration no serviço.
- MySQL remoto e usuário: ver [MIGRACAO-MYSQL.md](MIGRACAO-MYSQL.md).

## 8. Limitações

- **Um processo.** O estado de execução (a thread do `Daily`) fica em memória. Não escale para mais de um
  executor no mesmo banco.
- **Disco efêmero.** Parquet e CSV gerados no contêiner somem no próximo deploy. O CSV só sai localmente,
  com `build-entrega --csv` (previsto).
- **Reinício.** Um daily em andamento quando o processo cai vira `falhou` (`interrompida por reinício do
  executor`). Não é retomado: dispare de novo pelo painel.
- **Sem o extra `[sentiment]`** (instalação local mínima), a etapa `enriquecimento` falha e o daily sai
  `parcial`. A imagem do Railway tem o extra.
- **Placeholders:** `ipe`, `juiza`, `cadastro` e `entrega` saem `pulada` até serem implementadas.
- **Carga histórica:** ainda não implementada (ver seção 3).
