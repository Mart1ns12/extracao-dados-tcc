# Histórico de mudanças: 2026-10-19 e 2026-10-20

Changelog do trabalho que transformou o scraper em duas pipelines: o dataset v1 congelado e a pipeline nova, com
executor no Railway, painel de acompanhamento e as 8 tabelas da etapa seguinte. Cobre o pedido, o desenho, cada
marco com seus commits, as correções, a verificação, a migração para o banco único, as decisões tomadas pelo agente
e as pendências do dono. Fonte principal: o ledger em `.superpowers/sdd/quero-fazer-o-seguinte-misty-mochi/progress.md`.
Base: commit `04f1711`; branch `feat/executor-painel`; 35 commits até `1d16cfa`.

## 1. O pedido

O painel de acompanhamento (`interface-acompanhamento`) já existia, com a API testada contra um simulador. Faltava o
lado do scraper: rodar a coleta diária como serviço, expor o progresso ao painel e gerar as tabelas que a etapa
seguinte vai consumir.

Decisões do dono, que não foram reabertas:

- Escopo completo: Fases A e B. O Jev entra como adaptador desligado até aprovação.
- Um ano agora (`DATA_START=2025-09-01`), com fim rolante e pronto para cinco anos.
- A carga histórica roda no próprio Railway, dentro do executor, grava no MySQL em sequência, é retomável e tem
  prioridade sobre o daily.
- O consumidor lê as 8 tabelas direto no MySQL.
- O FinBERT roda no Railway.
- O dataset v1 fica intacto.
- A arquitetura é pull: a API do painel nunca chama o scraper. Ela grava comandos numa fila (`v1-painel_comandos`), e
  o executor busca, aplica, confirma e grava o progresso.

## 2. Desenho

Documentos de referência:

- `docs/superpowers/specs/2026-10-19-entrega-etapa-seguinte-design.md`: o desenho de produto aprovado.
- `docs/spec-entrega-etapa-seguinte.md`: o contrato das 8 tabelas.
- `interface-acompanhamento/api/docs/contrato-api.md` e `integracao-scraper.md`: o contrato do painel.
- O plano de execução ficou fora do repositório, em `C:/Users/consu/.claude/plans/quero-fazer-o-seguinte-misty-mochi.md`.

Fluxo do executor:

```
painel -> API do painel -> v1-painel_comandos (fila)
                                 | POST /api/executor/comandos
                            executor -> daily / etapa / historico
                                 | grava
                            MySQL v1-painel_* -> lido pela API -> painel
```

O `daily` tem nove etapas: `precos`, `macro`, `noticias`, `ipe`, `enriquecimento`, `juiza`, `cadastro`, `entrega` e
`analise`. A carga histórica usa os mesmos coletores, ano a ano. Ver [Executor e painel](07-executor-e-painel.md).

O trabalho foi executado por agentes em tarefas numeradas (T1 a T30), com um commit por tarefa. As tarefas com arquivos
disjuntos rodaram em paralelo. Tarefas que editam o mesmo arquivo, como `cli.py` e `orquestrador/etapas.py`, rodaram em
sequência.

## 3. Marcos

O plano organizou o trabalho em seis marcos. Os commits são os do `git log --oneline 04f1711..HEAD`.

### M0: fundações (T1 a T7)

Configuração, prefixos de modelo e os contratos de coleta que os marcos seguintes usam.

- `config.py`: `Settings` com a janela rolante, o executor, o daily, a juíza e a entrega. Constantes do universo
  (`UNIVERSO_HISTORICO`, `MUDANCAS_TICKER`, `UNITS`, `NOMES_EMPRESA`, `APELIDOS`, `CONTAS_CVM`, entre outras).
- `db/models.py`: `TABLE_PREFIXES` ganha `painel_`. Módulos `models_fontes`, `models_entrega` e `models_painel`
  criados.
- `collectors/base.py`: `ObservadorColeta` para progresso, `reprocessar_erros`, requisição HEAD e throttle por processo.
- `sentiment/scoring.py` e `features/build.py`: o `score` e o `build-dataset` deixam o `cli.py` e viram funções
  chamáveis.
- Coletores existentes: fim rolante, símbolos novos, `--desde` e `--ate`, nomes do painel.
- `scripts/gerar_mapas_b3_cvm.py`: mapa de ticker para código CVM, pelos endpoints da B3.

Commits: `abb9208`, `0470732`, `2527576`, `93a571e`, `88b4e7c`, `249c990`.

### M1: tabelas do painel, orquestrador, executor e deploy (T8 a T11)

- Migration 0004: 8 tabelas `painel_*` e as colunas `origem` e `published_precision` em `raw_articles`.
- `orquestrador/`: o daily de nove etapas, com progresso e o repositório do painel.
- `executor`: worker com fila de comandos, heartbeat e agenda das 18h30. Ver [Executor e painel](07-executor-e-painel.md).
- `Dockerfile` e `railway.json`: o executor como worker no Railway, com torch CPU e os modelos do FinBERT no build.

Commits: `8633065`, `b4a6f5b`, `61d7563`, `f2d5105`.

### M2: coletores das fontes oficiais (T12 a T20)

- Migration 0005: 9 tabelas de fontes e juíza.
- Fixtures reais de CVM, B3, BCB e Yahoo, para testar sem rede. São 22 arquivos.
- ITR e DFP da CVM (`raw_cvm_balancos`, `raw_cvm_acoes`).
- Fatos relevantes do IPE, gravados como notícias.
- Cadastro da CVM (`cad_cia_aberta`) e da B3 (GetDetail por código CVM).
- Proventos e suplemento da B3.
- CDI e IPCA do SGS, em fatias anuais.
- T20: ligação no CLI dos comandos novos e do `validate-cadastro`, e etapas `ipe`, `cadastro` e `macro`.

Commits: `b900d63`, `ab09c70`, `2dde798`, `9bfa263`, `df1b591`, `045e2bc`, `9d2092f`, `7db6700`.

### M3: `build-entrega` (T21 e T22)

- Migration 0006: as 8 tabelas da entrega, com os nomes e a ordem de colunas da spec.
- Montagem das 8 tabelas, filtro de qualidade e exportação em CSV.

Commits: `e6deca9`, `59b9f11`.

### M4: juíza e regra barata (T23 a T27)

- `GeminiClient.classificar` sem busca na web (T23). Cliente do Jev criado, desligado por padrão.
- Schema de saída e prompt `juiza_v1` (T24).
- Regra barata, sem LLM: panorama de mercado, mídia sem texto e CVM (T25).
- Juíza com persistência e conjunto-ouro (T26). *O conjunto-ouro foi removido em 2026-10-23 (seção 15).*
- Regressão do dataset v1 e do filtro da juíza, contra banco (T27).

Commits: `b4e2ded`, `0661dae`, `463b6b3`, `b31f4f1`, `ed2d89d`.

### M5: carga histórica no executor (T28)

- `orquestrador/historico.py`: carga com pausa, retomada, cancelamento e `reprocessar_erros`.
- Transições entre `em_andamento`, `pausando`, `pausada`, `cancelando` e `cancelada`, conforme a seção 3.1 de
  [Executor e painel](07-executor-e-painel.md).
- O cron do daily só fica registrado como aplicado quando o disparo termina em `ok`.

Commit: `89c26b7`.

### Fechamento (T20b, T29, T30 e revisão)

- **T20b**: ligação final no CLI, com `build-entrega`, `judge-news`, `historico` e as etapas `entrega` e `juiza` no
  orquestrador. Commit `4ca4a95`.
- **Correções da revisão final**: `17c9435` (notícias reprovadas, id do histórico, heartbeat, entrega parcial) e
  `d3219b2` (a remoção de notícia vale só para artigo julgado e reprovado).
- **T29**: documentação do executor, ADRs da entrega e SQL até a 0006. Commit `af89bf1`.
- **T30**: ponta a ponta, sem commit próprio. Ver seção 5.
- **Revisão final do branch**: o agente revisor caiu por limite de sessão, antes de terminar. O único achado
  confirmado foi o das rotas do executor: são `POST`, com o id no corpo do ack. Corrigido em `3184716`.
- **Migration 0007**: larguras do IPE, em `cdc9938`.

## 4. Correções durante a execução

| Problema | Correção | Commit |
|---|---|---|
| `IntegrityError` na chave de `raw_collection_runs`: as sequências do PostgreSQL estavam atrás dos ids copiados | Sequências ressincronizadas com `setval` (banco de dev, reversível). Ponto obsoleto depois da migração para o MySQL | sem commit (dado) |
| `StringDataRightTruncation` em `raw_ipe_docs`: valores reais acima de `varchar(32)`, `varchar(64)` e `varchar(128)` | Migration 0007 com tamanhos medidos na fixture de 2026 | `cdc9938` |
| Rotas do executor descritas de forma desatualizada no contrato do painel. O código real da API usa `POST`, com id no corpo do ack | Ajuste no executor, conforme o código real da API | `3184716` |
| O cron do dia podia se perder por colisão com um daily manual | Comando `cron-<dia>` só é registrado como aplicado quando `ok`. Senão tenta de novo no próximo ciclo | `89c26b7` |
| Notícia reprovada continuava na tabela entregue, porque `build-entrega` só faz upsert | Remoção das linhas de `noticias` apenas para artigos julgados e reprovados | `17c9435`, `d3219b2` |
| Notícias sem julgamento poderiam sair sem critério | Sem julgamento, nada sai. O aviso "sem julgamento" conta o IPE até a juíza rodar | `d3219b2` |
| Heartbeat dizia `historico` com a carga pausada | Campo `ocupado_com` vira null quando a carga está pausada | `17c9435` |
| Id do histórico (`hist-AAAAMMDD-HHMM`) colidia no mesmo minuto | Id com segundos | `17c9435` |

Mudança de contrato sem migração: o hash de `raw_b3_proventos` mudou com a inclusão de `issuing_company`. As tabelas
`raw_b3_*` estavam vazias no dev, e a produção é do zero. A docstring avisa que mudar o hash exige recoleta.

## 5. Verificação

### 5.1 Testes e lint

| Momento | pytest (passed / skipped / xfailed) | Observação |
|---|---|---|
| Verificação integrada, antes da correção final | 408 / 37 / 1 | 0 falhas; ruff limpo; 20 comandos; `db-info` com 39 tabelas; 27 commits |
| Estado após a revisão final e o T30, com 31 commits | 420 / 39 / 1 | Suíte verde |
| Primeira 0008 (38 tabelas) | 423 / 40 / 1 | `alembic check` limpo; ida e volta no PostgreSQL |
| Após a refatoração para banco único | 385 / 39 / 1 | Sem banco; `ruff` limpo |
| Após a 0008 refeita (commit `1d16cfa`) | 386 / 39 / 1 | Sem banco |

Os números são os registrados no ledger, ao fim de cada etapa. Não foram reexecutados neste documento.

### 5.2 Ponta a ponta (T30)

O roteiro do T30, definido no plano, cobre: `alembic upgrade head`, `pytest -q`, API do painel em `MODO=live` com o
executor, daily disparado pelo painel, carga histórica de 2026 e 2025 em modo `pendentes`, pausa, retomada e
cancelamento, queda do executor no meio com retomada, e `build-entrega --csv`. O ledger registra o T30 como
`PASSOU (passos 1 a 7)`, sem detalhar cada passo. A execução foi feita no PostgreSQL local, antes da migração para o
banco único. O deploy no Railway e o heartbeat em `/api/componentes` não estão registrados como feitos.

Números da entrega e das fontes, conforme o ponta a ponta:

| Tabela | Linhas | Observação |
|---|---|---|
| `v1-precos` | 48.561 | Montagem da verificação de ponta a ponta (Postgres local, antes da migração para o banco único). `raw_prices` tem 48.564 linhas (coleta) |
| `v1-noticias` | 4.980 | |
| `v1-proventos` | 8.392 | |
| `v1-balancos` | 247 | |
| `v1-acoes_emitidas` | 170 | |
| `v1-indices` | 210 | |
| `v1-cadastro` | 88 | |
| `v1-mudancas_ticker` | 10 | |
| `v1-raw_ipe_docs` | 2.714 | Daily `ipe` concluído |

Outros números da rodada: macro com 906 itens; cadastro da coleta com 11.316 itens, entre CVM e B3 reais.

Passos da rodada:

1. A primeira rodada falhou no passo 3 (`IntegrityError` nas sequências do PostgreSQL). Corrigido com `setval`.
2. A segunda rodada falhou no passo 4, na etapa `ipe`, por `varchar(32)` em `raw_ipe_docs`. Corrigido pela 0007.
3. A rodada retomada nos passos 5 a 7 passou. Nela, o executor, iniciado às 23h19, disparou o daily `cron` do dia, o que
   era o comportamento desejado (recuperação da agenda). O heartbeat foi gravado mesmo com a API do painel fora do ar:
   o banco é a fonte que o painel lê.

## 6. Migração para o banco único

Até a refatoração de 2026-10-20, o projeto tinha dois alvos: o MySQL de produção e um PostgreSQL local. A seleção era feita por
variáveis do `.env` (`DB_TARGET` e `DATABASE_URL`), e `migrar_dados.py` copiava os dados entre os dois.

Sequência da mudança:

1. **`caf2deb`**: a primeira versão da 0008 renomeava 38 tabelas. Pelo número (38 = 25 + 13), ela incluía as 13 que já
   existiam antes da 0004, fora do critério adotado. A versão foi revertida.
2. **`42e9e28`**: um banco só, o MySQL do `.env`. Removidos `docker-compose`, `pg-start`, `pg-stop`, `pg-setup` e
   `migrar_dados.py`. O `db-info` passou a conectar só no MySQL.
3. **Reversão da 0008 no MySQL**: `alembic downgrade 0007`, a pedido do dono, com os dados preservados.
4. **`1d16cfa`**: a 0008 refeita. Renomeia só as 25 tabelas criadas a partir da 0004. O `sql/0008_prefixo_v1.mysql.sql`
   traz o `DELETE` da linha `0002`. Conferido contra o MySQL: as 25 origens existem e as 14 tabelas antigas estão
   presentes.

A 0008 ainda não foi aplicada no MySQL de produção. A ordem de entrada está em [Operação e deploy](08-operacao-e-deploy.md),
seção 5. *(Nota de 2026-10-20: a pendência foi resolvida, e o schema já está aplicado no MySQL de produção. Ver 6.1.)*

### 6.1 Alembic descontinuado e faxina (2026-10-20)

Com o banco unificado e o schema já aplicado no MySQL de produção (estado equivalente à antiga 0008, aplicada pelo
dono), o Alembic deixou de ter função e foi descontinuado. O ADR "Alembic descontinuado (2026-10-20)" está em
[ADR](ADR.md).

- **Removidos**: `alembic.ini`, a pasta `alembic/`, a dependência no `pyproject.toml` e os `sql/*.mysql.sql` gerados
  pelo Alembic.
- **Adicionados**: `sql/schema.mysql.sql`, com o schema completo do estado atual, para banco vazio (não cria
  `alembic_version`). A regra de alteração passa a ser manual: muda o model em `src/tcc_scraper/db/`, atualiza
  `sql/schema.mysql.sql` e escreve o ALTER em `sql/alteracoes/AAAA-MM-DD_descricao.mysql.sql`, aplicado à mão com
  aprovação do dono.
- **Código morto removido**: `NATIVE_FEEDS`, `COLETORES_HISTORICO` e `Settings.brapi_token` (`BRAPI_TOKEN`). Não havia
  coletor Brapi nem de RSS nativo implementado. As duas fontes continuam aprovadas na decisão, sem implementação.
- **`db-info`** sugere `sql/schema.mysql.sql` no lugar de `alembic upgrade head`.
- **Documentação** atualizada: `CLAUDE.md`, `README.md`, e os docs 06, 07 e 08 e o ADR. O histórico anterior foi mantido.
- **`alembic_version`** continua no MySQL de produção, sem uso. A remoção aguarda aprovação do dono.
- **Reversão**: a reversão de uma alteração é escrita à mão. A da 0008, por exemplo, seria `RENAME TABLE` das 25 tabelas
  de volta. O `alembic downgrade 0007` citado acima deixou de existir.

## 7. Decisões tomadas pelo agente

As decisões abaixo estão no ledger como "Ruling". A coluna de custo traz o que o próprio ledger registra caso o ruling
esteja errado. Onde o ledger não registra custo, a célula tem `-`.

| Decisão | Motivo | Custo se errado |
|---|---|---|
| `DATA_FIM_COLETA` vazia conta como ausente (`env_ignore_empty`) | Vazio no `.env` não deve virar erro de data | Nenhum |
| `MUDANCAS_TICKER` só com trocas simples. Fusões (BRFS3 e MRFG3 para MBRF3) ficam fora, com comentário | A spec pede assim | Consumidor sem a fusão na tabela |
| Nomes de empresa para os removidos: CRFB3 (Carrefour Brasil), STBP3 (Santos Brasil) e SRNA3 (Serena Energia) | O nome é o termo principal da busca de notícias (`CLAUDE.md`) | - |
| `NOMES_EMPRESA` e `APELIDOS` em ASCII. O casamento de nomes normaliza acento | Convenção do arquivo | Nomes sem acento na interface |
| Apelidos de MBRF3 são as marcas da BRF (Sadia, Perdigão) | Aceito pelo agente: a notícia de marca da BRF conta para MBRF3 | - |
| Throttle compartilhado: reserva o próximo horário por domínio sob lock e dorme fora do lock | Não serializar domínios distintos | Taxa somada entre coletores do mesmo domínio |
| `score_pending` mantém o plano por bloco. A barra do CLI conta blocos | Plano por bloco, sem mudar a saída do `score` | Barra de progresso mais grossa |
| Renomeação dos coletores (`yahoo`, `bcb`, `enriquecimento` e outros) | Ids e nomes do painel são contrato | Checkpoints antigos deixam de valer (aceito: carga do zero) |
| CRFB3 fica no universo só para preço e notícia. O cadastro sai sem CNPJ | Companhia fechou capital e saiu da B3 | - ; teste marcado como `xfail` |
| Fixtures de 384 KB e 441 KB aceitas, acima do teto de 300 KB | Lidas em teste, sem custo de rede | - |
| `typeStock` aceito: ON, PN, PNA e PNB. UNIT é composto a partir de ON e PN. `RENDIMENTO` fica fora, com aviso | Valores reais da B3 (spike S1) | - |
| `CD_CVM` comparado como inteiro, porque vem com zeros à esquerda. Composição de capital filtrada por CNPJ | Formato do arquivo da CVM | - |
| `CONTAS_CVM` ganha DRE 3.09 e BPP 2.08. Bancos escolhem a linha pelo `ds_conta` | Bancos têm plano de contas próprio (spike S2) | PL e lucro de bancos nulos |
| `lucro_bruto` só sai para empresas que não são bancos ou seguradoras | A spec deixa esses campos em branco para elas | - |
| Bancos e seguradoras por lista explícita (`CD_CVM_BANCOS_SEGURADORAS`), não pela descrição. Lucro bruto, EBIT, D&A, empréstimos, caixa e aplicações ficam em branco para eles. Corrige o `lucro_bruto` de BBSE3, IRBR3 e PSSA3 | A descrição não separa bancos: BBSE3 e IRBR3 têm "Caixa e Equivalentes de Caixa", e PSSA3 usa o plano industrial (conferido no ITR 2025) | Lista precisa de manutenção quando entrar banco ou seguradora novo |
| Empréstimos somam `.01` e `.02` (empréstimos e debêntures), de curto e longo prazo, sem o arrendamento (`.03`) | Decisão D2 do dono: a spec pede dívida sem arrendamento | Dívida diverge de quem inclui o arrendamento |
| D&A do trimestre do ITR = diferença entre os acumulados da DFC (o ITR é acumulado no ano). Sem o trimestre anterior, ou com diferença negativa, fica em branco | Decisão D5 do dono (alterada): a CVM só publica a DFC acumulada, e o trimestre sai da diferença de dois documentos | D&A vazia nos trimestres sem o anterior coletado |
| Mudar `CONTAS_CVM` exige recoleta com `collect-cvm --no-resume` e depois `build-entrega --tabela balancos` | Sem `--no-resume`, o checkpoint pula os arquivos já lidos e as colunas novas ficam vazias | Colunas novas em branco até a recoleta |
| `factor` dos proventos: percentual para desdobramento e bonificação, razão para grupamento. **Em aberto**: validado só com um caso (PETR) | Convenção da B3 | Proventos antigos, depois de grupamento, mal ajustados |
| Nomes de até cinco letras só contam com inicial maiúscula no título original | Vale, Azul, Rumo, Vamos e Light viram ruído se a busca for livre | Perde "itau" em minúsculo (raro) |
| Domínio bloqueado casa também subdomínio (`m.youtube.com`) | Aceito pelo agente | - |
| Juíza: classificação sem ferramentas, com `temperature=0` | Reprodutibilidade | - |
| Jev desligado. Falha do Jev cai para o Gemini, com aviso | Aprovação pendente do dono para o Jev | Julgamento sem a segunda fonte (aceito) |
| Teto da juíza é `JUIZA_MAX_ITENS_DIA`, sem `limite_itens` | Protege o custo do Gemini (aceito) | - |
| `matriz_estrangeira` cobre matéria sobre a controladora estrangeira (AB InBev e Santander) | Desenho descrito na spec | - |
| Base64 padrão na URL da B3, verificado com chamadas reais | Chamadas reais em 2026-10-19 | - |
| SGS dos índices começa em `price_start`. Selic e PTAX seguem `data_start` | Índices precisam do aquecimento; Selic e PTAX são do v1 | - |
| `cancelar` é controle da carga. O daily não aceita cancelamento pelo painel | Contrato do painel (mantido) | - |
| `pausando` no reinício retoma a carga | Literal do contrato | - |
| Interrupção em sub-coletor leva a `falhou` | Consistente com `de_stats` | - |
| `--anos` é repetível, porque o Typer não aceita lista | Limitação da biblioteca | Documentado |
| Remoção de notícia reprovada só para artigos julgados | Sem julgamento, nada sai | Tabela entregue com lixo antigo |
| Hash de proventos mudou sem migração | Tabelas `raw_b3_*` vazias no dev. Produção é do zero | Recoleta necessária se já houver dado |
| `ctx.hoje` igual ao fim da coleta. `v1-painel_logs` sem `execucao_id`, como no contrato | Contrato do painel | - |
| Commits por tarefa no branch de feature, sem push | Pedido do dono de executar o plano | `git reset` se não quiser |
| Paralelismo só entre tarefas de arquivos disjuntos. `cli.py` e `etapas.py` em sequência | Evitar conflito de merge | Conflito em commit |
| ADR regravado depois de cada `index_repository` | Reindexação apaga o ADR do grafo | ADR perdido |

## 8. Pendências do dono

- **Remover `alembic_version`**: o schema já está aplicado no MySQL de produção. Falta só `DROP TABLE alembic_version`, quando o dono aprovar (ver [Operação e deploy](08-operacao-e-deploy.md), seção 5, e [Banco de dados](06-banco-de-dados.md), seção 4.4).
- **Conferir o `.env` local**: remover `DB_TARGET` e `DATABASE_URL`, variáveis do PostgreSQL.
- **Deploy no Railway**: serviço do executor, variáveis e `EXECUTOR_TOKEN` igual ao da API do painel.
- **Merge do `feat/executor-painel`** na `main`. Decisão do dono.
- **Revisão final do branch**: não concluída. Pode ser retomada com um revisor novo.
- **Aprovação do Jev** como segunda exceção paga do projeto. Enquanto isso, `JEV_ENABLED=false`. *Aprovada em 2026-10-23; o padrão segue `false` (seção 15).*
- **Conjunto-ouro** para `judge-news --avaliar`. A rotulagem é do dono. *Removido em 2026-10-23: a qualidade passou a ser medida por concordância Jev × Gemini (seção 15).*
- **Rubrica da juíza**: validada só com chamada real. Rodar `judge-news --limite 50`, com contagem de tokens.
- **Spike S7**: confirmar numa chamada real que a Interactions API aceita `generation_config`.
- **Fator de grupamento**: conferir a conversão com um grupamento conhecido (decisão em aberto na seção 7).
- **Nomes de empresa**: revisar `NOMES_EMPRESA` à mão. As sugestões do `tradingName` da B3 foram consideradas ruins.
- **Acentos na coluna `empresa`**: decisão de dado, não de código. Hoje sai sem acento.
- **`.cursor/rules/codebase-memory.mdc`, linha 56**: ainda cita PostgreSQL.
- **Docs fora deste trabalho** (feito na etapa final de documentação): os documentos absorvidos foram removidos, os links foram corrigidos, as docstrings citadas passaram a usar `v1-`, o `CLAUDE.md` diz que a juíza compartilha o teto de US$ 5/mês do Gemini, e a entrada do Jev no ADR diz que aguarda aprovação do dono.
- **Ajuste técnico**: `autoincrement=False` em `raw_b3_cadastro.cd_cvm`, no modelo e no ALTER correspondente em `sql/alteracoes/` (ver [Banco de dados](06-banco-de-dados.md), seção 7).

## 9. Lacunas

Estes pontos não foram confirmados no código ou no ambiente. Não há evidência neste documento de que funcionem:

- **`docker build` não foi executado.** A imagem do Railway está escrita e não foi construída.
- **Memória do FinBERT no Railway não foi medida.** O spike S4 previa medição e um plano B, sem resultado.
- **Paridade do dataset no MySQL não foi reexecutada.** O teste de paridade existia entre PostgreSQL e MySQL. Depois da
  migração para o banco único, não há servidor de teste.
- **Ponta a ponta no MySQL de produção não foi feito.** O T30 rodou no PostgreSQL local.
- **Número de preços: resolvido na etapa final de docs.** `raw_prices` tem 48.564 linhas (coleta); `v1-precos` tem 48.561, montadas na verificação de ponta a ponta.
- **Subida do executor no Railway não foi confirmada.** Depende do deploy da seção 8.
- **`judge-news --avaliar` sem chave** deveria avisar e sair com código 0. Não foi conferido. *O comando foi removido em 2026-10-23.*
- **Spike S3 (hora do IPE no RAD)** ficou fora do plano: a publicação é tratada como `precision='date'`, com
  `momento_pregao='desconhecido'`.

## 10. Comando coletar no executor (2026-10-20)

**O pedido.** As verificações do painel precisam recoletar coletores específicos para um recorte (por exemplo, ITR e
DFP de 2024 e 2025, sem checkpoint) e remontar uma tabela da entrega (`balancos`). A carga histórica faz isso, mas
roda inteira e só cabe uma por vez. O comando `coletar` faz a coleta pontual. Detalhes de uso e recusas estão em
[Executor e painel](07-executor-e-painel.md), seção 4.1.

**Arquivos novos:**

- `src/tcc_scraper/orquestrador/coletores.py`: catálogo único dos coletores (id, alvo, checkpoint padrão, parâmetros
  aceitos e janela de anos).
- `src/tcc_scraper/orquestrador/coletar.py`: validação do comando e montagem do plano de etapas.
- `tests/test_coletores.py`, `tests/test_daily_coletar.py` e `tests/test_coletar.py`.

**Arquivos alterados:**

- `orquestrador/historico.py`: `Passo` delega ao catálogo. As 13 constantes de alvo saem.
- `orquestrador/daily.py`: execução `tipo = coletar`, com plano em memória. `criar` passa a `_registrar`.
- `orquestrador/etapas.py`: `montar_entrega`, `resultado_juiza` e `resultado_finbert` extraídos para reuso.
- `collectors/prices_yahoo.py`: `tickers_padrao()`.
- `executor/comandos.py` e `executor/loop.py`: comando `coletar` e `ocupado_com`.
- `db/models_painel.py`: só comentários. Sem mudança de schema.
- `tests/test_orquestrador.py` e `tests/test_executor_loop.py`: casos novos.

**Decisões:**

1. **Catálogo único** em `orquestrador/coletores.py`, usado pela carga histórica e pelo `coletar`. Não muda o comportamento da carga.
2. **Concorrência como etapa manual.** Com execução `agendada` ou `em_andamento`, o `coletar` é recusado. Com carga ativa, nasce `agendada` e espera a carga. Custo: a recusa precisa ser reenviada pelo painel.
3. **Execução no `Daily`, com plano em memória e sem orçamento.** Falha de item não para os demais, e o final sai `parcial`. Custo: o plano se perde no reinício.
4. **Sem cancelamento pelo painel.** `cancelar` continua sendo da carga.
5. **Validação estrita.** Chaves de topo e de item são fechadas, a janela de anos é por coletor, e toda recusa vai com mensagem no ack.

**O que o time do painel precisa saber:**

- O heartbeat pode mandar `ocupado_com = "coletar"`.
- Em execuções `coletar`, as etapas de `v1-painel_etapas` usam ids de coletor (`cvm_itr_dfp`, `google_news`,
  `build_entrega`), e não as nove etapas do daily.
- Os logs dessas execuções saem com `origem = daily`.
- Um coletar recusado por execução ativa volta com `ja existe uma execucao em andamento ou agendada` e precisa ser
  reenviado.
- Depois de um reinício do executor, um coletar `agendada` ou `em_andamento` vira `falhou`, com a mensagem
  `reenvie o comando`.

## 11. Abertura do IBOV repetida pela fonte (2026-10-23)

**O problema.** Em ~32% dos dias, o Yahoo (`^BVSP`) devolve a abertura igual ao fechamento do pregão anterior. O pipeline recebe o valor como vem da fonte.

**Decisão.** A abertura repetida vira vazia (`NULL`) só na entrega, em `v1-precos`, ticker `IBOV`. A camada `raw_prices` não muda, então a regra pode ser revista sem recoleta. Só o IBOV é tratado: outras séries não foram medidas com o mesmo defeito.

**Arquivos alterados:**

- `src/tcc_scraper/entrega/builder.py`: helper `_anula_abertura_repetida` e aplicação em `montar_precos`.
- `tests/test_entrega_builder.py`: `_preco` aceita `abertura=`; 3 testes novos (repetida vira vazia; normal e primeiro dia mantêm; só o IBOV é afetado).
- `docs/05-entrega.md`: seção 3, regra da coluna `abertura` do IBOV.

## 12. Hora falsa das notícias do Google News (2026-10-23)

**O problema.** ~91% das notícias do Google News chegam com hora 00:00:00 do fuso de Los Angeles (04:00 ou 05:00 em SP). O pubDate do RSS traz só a data, e o servidor do Google preenche a hora com a meia-noite local. Como essa hora é falsa, a notícia parece publicada de madrugada, e o experimento vaza futuro: uma notícia da tarde do dia `t` entra como se já existisse de manhã.

**Decisão.**

1. **No parse do RSS** (`collectors/google_news.py`, `parse_rss`): se o pubDate, convertido para `America/Los_Angeles`, cair exatamente em 00:00:00, o artigo grava `published_precision='date'`, com `published_at` em 00:00 de São Paulo da data do Pacífico (em UTC) e `published_time_source` NULL. Horário real grava `'datetime'` e `published_time_source='rss'`.
2. **No enriquecimento** (`enrich/article_fetcher.py`, `_extrair_um`): quando o artigo está com `published_precision='date'` e a página da allowlist traz hora com fuso (meta `article:published_time`, `og:published_time`, `itemprop=datePublished`, JSON-LD `datePublished` ou `<time datetime>`, em `enrich/data_publicacao.py`), a hora só é aceita se a data da página, em SP, estiver a no máximo 1 dia da data gravada. Aceita, grava `published_at` (UTC), `published_precision='datetime'` e `published_time_source='pagina'`. Sem fuso, o valor é ignorado: a hora não é adivinhada.
3. **Comando `fix-news-times`** (`cli.py`, com `--dry-run` e `--limite`) para o histórico já gravado. Fase (a): remarca como `date` os artigos `google_news` em meia-noite de Los Angeles. Fase (b): para os `date` com `resolved_url` de veículo da allowlist, baixa a página (com o throttle do coletor) e aplica a regra do item 2. `--dry-run` conta tudo e não grava. A fase (b) baixa as páginas mesmo em dry-run, porque é leitura.

**Coluna nova.** `raw_articles.published_time_source VARCHAR(8)`, NULL. Modelo em `db/models.py`, `sql/schema.mysql.sql` e `sql/alteracoes/2026-10-23_articles_hora_origem.mysql.sql`. Aplicação manual, com usuário com DDL, após aprovação do dono. A migração depende de `published_precision`, que já existe no modelo.

**Limitações.**

- Artigos já enriquecidos (com corpo) não são baixados de novo pelo enrich; só a fase (b) do `fix-news-times` revisita os `date`.
- A fase (b) do `fix-news-times` é sequencial. Passa pelo mesmo throttle por domínio, por isso é lenta em carga grande.
- Página de veículo que não traz hora continua como `date`. Esse é o caso que a regra existe para não adivinhar.
- O `schema.mysql.sql` já não tinha `published_precision` nem `origem` antes desta mudança. Essa divergência não foi corrigida aqui e fica como pendência.

**Arquivos alterados:**

- `src/tcc_scraper/collectors/google_news.py`: `FUSO_PACIFICO`, `FUSO_BRASIL`, `data_de_meia_noite_pacifica`, `inicio_do_dia_brasil`, campos novos em `NewsItem` e em `parse_rss`, `_persist`.
- `src/tcc_scraper/enrich/data_publicacao.py` (novo): `extrair_data_publicacao` e `refinar_publicacao`.
- `src/tcc_scraper/enrich/article_fetcher.py`: `_pending`, `_extrair_um`, `_tentar_hora_da_pagina`, `_persist_horas`, `corrigir_horas_publicacao` e os helpers da fase (a) e (b).
- `src/tcc_scraper/cli.py`: comando `fix-news-times`.
- `src/tcc_scraper/db/models.py`, `sql/schema.mysql.sql`, `sql/alteracoes/2026-10-23_articles_hora_origem.mysql.sql`.
- `tests/test_data_publicacao.py`, `tests/test_google_news_precisao.py`, `tests/test_fix_news_times.py` (novos).
- `docs/05-entrega.md`: seção 2.3, nota sobre a precisão da hora.


## 12. IDIV, SMLL e IFIX pela B3 (2026-10-23)

**O problema.** O Yahoo devolve só 1 dia de histórico para `IDIV.SA`, `SMLL.SA` e `IFIX.SA` (`range=5y` trouxe 1 ponto, medido). Sem os 5 anos, os índices não cobriam a janela do dataset.

**Decisão.** Os três índices saem do Yahoo e vêm da B3, pelo endpoint `indexStatisticsProxy/IndexCall/GetPortfolioDay`, sem autenticação, um ano por chamada. Cada resposta traz 31 linhas (dia do mês) com `rateValue1..12`, o fechamento do dia em cada mês, em texto pt-BR (`"6.831,31"`). `null` quer dizer que não houve pregão ou que o dia não existe no mês. Os valores gravados em `raw_prices` usam `ticker` = código do índice (`IDIV`, sem `.SA`), que é a chave que a entrega usa em `INDICES_YAHOO`, e `source = 'b3'`. Esse endpoint só traz o fechamento: `open`, `high`, `low` e `volume` ficam nulos, e `adj_close` recebe o próprio `close`, como já era para os índices do Yahoo.

Na etapa `precos` do daily, o coletor refaz só o ano corrente. A carga histórica e o comando `coletar` usam `janela_anos`, como os demais coletores que têm anos.

**Arquivos alterados:**

- `src/tcc_scraper/collectors/b3_indices.py` (novo): `B3IndicesCollector`, `parse_portfolio_day` e `montar_raw_prices`.
- `src/tcc_scraper/collectors/b3_base.py`: `url_b3` e `B3Collector.get_json` aceitam `base=` para outro proxy da B3. O padrão não mudou.
- `src/tcc_scraper/collectors/prices_yahoo.py`: `tickers_padrao` não inclui mais `INDICES_YAHOO`.
- `src/tcc_scraper/orquestrador/coletores.py`: catálogo do coletor `b3_indices`, com `janela_anos` da faixa de preços.
- `src/tcc_scraper/orquestrador/etapas.py`: `etapa_precos` roda o Yahoo e o `b3_indices` por `_executa_partes`. Id, nome e coletor da etapa continuam os mesmos.
- `src/tcc_scraper/orquestrador/historico.py`: `_passos` inclui `b3_indices` logo após `yahoo`.
- `scripts/capturar_fixtures.py`: `_grava_b3_indice` e a seção de captura dos três índices.
- `tests/fixtures/b3_indices_idiv_2022.json`, `b3_indices_smll_2022.json` e `b3_indices_ifix_2022.json` (novos, respostas reais de 2022).
- `tests/test_b3_indices.py` (novo). `tests/test_coletores.py`, `tests/test_historico.py` e `tests/test_google_news_janelas.py` foram ajustados ao catálogo e ao Yahoo sem os índices.
- `docs/01-visao-geral.md`, `docs/02-pipeline-diaria.md` e `docs/05-entrega.md`.

**Efeitos.** A linha de 1 dia que o Yahoo gravou para cada índice é sobrescrita pelo upsert da B3 na mesma data, com as colunas que a B3 não traz zeradas. Para os 5 anos entrarem no banco, é preciso rodar a carga histórica (ou o `coletar` com o coletor `b3_indices`) e depois `build-entrega`.


## 13. Empréstimos de quem lança a dívida só no pai (2026-10-23)

**O problema.** No DFP 2025 consolidado (CSN, cd_cvm 4030), a dívida vem só na conta-pai: `2.01.04` "Empréstimos e Financiamentos" = 10.428.559 e `2.02.01` = 42.495.988 (em MIL). Todas as subcontas (`2.01.04.01`, `.01.01`, `.01.02`, `.02`, `.03` e as de `2.02.01`) vêm 0. Sabesp, Rede D'Or, Magalu e CMIN também. A entrega saía com `emprestimos_*` = 0 por dois motivos: o coletor só gravava as contas de `CONTAS_CVM`, então o pai nem chegava ao raw, e o builder somava `.01` + `.02` = 0.

**Decisão.**

- `config.py`: `CONTAS_CVM_AUXILIARES` com `2.01.04`, `2.02.01`, `2.01.04.03` e `2.02.01.03`. Só o filtro do coletor as usa. `CONTAS_CVM`, que define as colunas, não mudou.
- `collectors/cvm_demonstracoes.py`: o filtro do BPP aceita `CONTAS_CVM` ou `CONTAS_CVM_AUXILIARES`. O filtro de `ORDEM_EXERC = ÚLTIMO` e da versão continua igual.
- `entrega/builder.py`, `_emprestimos`: se a soma de `.01` + `.02` for > 0, vale a soma, como antes. Senão, se o pai (descrição "emprestimos e financiamentos") for diferente de 0, vale pai menos arrendamento (`.03`, None como 0). Sem pai e sem subcontas, fica None.
- Aplicações financeiras zeradas continuam valendo 0 (o pai também é 0 nesses casos). A regra não mudou.

**Limitação da fonte.** Se a empresa preenche só o pai e deixa o `.03` em 0, o valor pode incluir arrendamento, que a CVM não separa.

**Arquivos alterados:**

- `src/tcc_scraper/config.py`: `CONTAS_CVM_AUXILIARES`.
- `src/tcc_scraper/collectors/cvm_demonstracoes.py`: filtro do BPP e import.
- `src/tcc_scraper/entrega/builder.py`: `_PAI_EMPRESTIMOS` e `_emprestimos`, usados em `montar_balancos`.
- `tests/test_entrega_builder.py`: casos CSN, pai menos arrendamento, subcontas ignoram pai e pai zerado sem subcontas.
- `tests/test_cvm_demonstracoes.py`: `test_dfp_2024_traz_ebit_dividas_pai_e_arrendamento` (antes `..._sem_arrendamento`), que agora confere que pai e `.03` são gravados.
- `docs/05-entrega.md` (seção 6) e este arquivo.

**Efeitos.** O raw atual não tem as contas-pai. Para o caso CSN e os demais, é preciso recoletar `cvm_itr_dfp` (ITR e DFP) e só então rodar `build-entrega`. Sem a recoleta, a regra nova não tem o que ler e os valores seguem em 0.

## 14. Composição do IBOV por data (2026-10-23)

**O problema.** A entrega precisa saber quais ações estavam no IBOV em cada data (`v1-composicao_ibov`). Não há fonte única: as carteiras quadrimestrais oficiais (jan/2021 a set/2024) estão no repositório MIT paccafelipe/ibov-data-from-b3; o que vem depois são comunicados de rebalanceamento; e a carteira atual vem da API da B3.

**Decisão.**

- Carteiras de jan/2021 a set/2024 copiadas para `data/composicao_ibov/` (12 CSVs, com `LEIAME.md` de atribuição). `data/` estava no `.gitignore`: liberada só essa pasta (`!data/composicao_ibov/`).
- Rebalanceamentos de 2025 a 2026-09-08 em `REBALANCEAMENTOS_IBOV` e eventos extraordinários de 2024-09 a 2026-08 em `EVENTOS_IBOV_EXTRAORDINARIOS` (`config.py`), cada um com a data do primeiro pregão em que vale e a fonte em comentário. As trocas de `MUDANCAS_TICKER` entram na mesma linha do tempo; uma troca só vale se o código antigo estava na carteira e se nenhum evento explícito já trata o par.
- Módulo puro `entrega/composicao_ibov.py`: `carregar_carteiras_csv`, `aplicar_eventos`, `linha_do_tempo_ibov` e `montar_vigencias`. `builder.py` tem `montar_composicao`, que aplica os fechamentos já gravados pelo coletor.
- Coletor `collectors/b3_carteira_ibov.py` (`B3CarteiraIbovCollector`, fonte `b3_api`): compara a carteira de hoje com as vigências abertas e grava só a diferença, com a data do header. Entra na etapa `precos` do daily e no catálogo, com `resume=False`.
- Schema: model `ComposicaoIbov` (PK `ticker`, `vigencia_inicio`), bloco na etapa 0010 de `sql/schema.mysql.sql` e `sql/alteracoes/2026-10-23_composicao_ibov.mysql.sql`, para aplicar à mão.

**Arquivos alterados.** `data/composicao_ibov/` (novo), `.gitignore`, `config.py`, `db/models.py`, `db/models_entrega.py`, `entrega/composicao_ibov.py` (novo), `entrega/builder.py`, `collectors/b3_carteira_ibov.py` (novo), `orquestrador/coletores.py`, `orquestrador/etapas.py`, `sql/schema.mysql.sql`, `sql/alteracoes/2026-10-23_composicao_ibov.mysql.sql` (novo), `tests/test_composicao_ibov.py` (novo), `tests/fixtures/b3_carteira_ibov_2026.json` (novo, resposta real de 2026-10-23), `tests/test_batch_regression.py` (27 tabelas v1-), `tests/test_entrega_schema.py` (9 tabelas), `tests/test_coletores.py` (catálogo com 15 ids).

**Verificação.** Suíte inteira: 634 passed, 39 skipped, 1 xfailed. Ruff nos arquivos tocados: limpo. Nada foi gravado no banco nem aplicado SQL. O teste `test_reconstrucao_ate_2026_10_23_bate_com_a_carteira_da_api` confere que a reconstrução até 2026-10-23 tem o mesmo conjunto de 76 papéis da carteira da API.

**Validação contra a B3: fechada.** A primeira reconstrução dava 84 papéis em 2026-10-23, contra 76 da B3. A diferença era de eventos fora dos rebalanceamentos: fusões, cisões, recuperações, OPAs, deslistagens e trocas de código com mais de um papel. Eles foram para `EVENTOS_IBOV_EXTRAORDINARIOS`, com a data do primeiro pregão em que a mudança vale. A diferença final é zero.

Contagem de papéis depois de cada data da linha do tempo:

| Data | Papéis | O que muda |
|---|---|---|
| 2024-09-09 | 86 | BRAV3 entra, RRRP3 sai |
| 2024-11-18 | 86 | ISAE4 entra, TRPL4 sai |
| 2024-12-16 | 87 | AMOB3 entra (cisão da Vamos) |
| 2025-01-06 | 87 | Rebalanceamento |
| 2025-05-02 | 87 | CCRO3 vira MOTV3 |
| 2025-05-05 | 87 | Rebalanceamento |
| 2025-05-30 | 86 | AZUL4 sai (recuperação judicial) |
| 2025-06-02 | 85 | CRFB3 sai (deslistagem) |
| 2025-06-09 | 84 | JBSS3 sai (vira BDR, fora do índice) |
| 2025-07-02 | 84 | NTCO3 vira NATU3 |
| 2025-09-01 | 84 | Rebalanceamento |
| 2025-09-23 | 83 | BRFS3 e MRFG3 saem, MBRF3 entra (fusão) |
| 2025-10-06 | 82 | STBP3 sai (OPA) |
| 2025-11-03 | 82 | EMBR3 vira EMBJ3 |
| 2025-11-10 | 82 | ELET3, ELET6 e CPLE6 saem; AXIA3, AXIA6 e CPLE5 entram |
| 2025-12-22 | 82 | CPLE5 sai, CPLE3 entra |
| 2026-01-05 | 85 | Rebalanceamento |
| 2026-03-12 | 84 | PCAR3 sai (recuperação extrajudicial) |
| 2026-03-13 | 83 | RAIZ4 sai |
| 2026-05-04 | 79 | Rebalanceamento |
| 2026-05-05 | 79 | ODPV3 vira SAUD3, mas ODPV3 não estava na carteira: sem efeito |
| 2026-06-08 | 78 | AXIA6 sai (convertida em AXIA3) |
| 2026-08-26 | 77 | BRKM5 sai (recuperação extrajudicial) |
| 2026-09-08 | 76 | Rebalanceamento |

**Simplificações.** As trocas intermediárias não têm linha própria. ELET6 → AXIA6 (2025-11-10) → AXIA3 (conversão de 2026-06-08) aparece como AXIA6 até 2026-06-07, e AXIA3 já entra em 2025-11-10 pela troca de ELET3. AZUL4 → AZUL54/AZUL53 → AZUL3 não entra: AZUL4 sai antes, por recuperação judicial. CPLE6 → CPLE5 → CPLE3 aparece como CPLE5 de 2025-11-10 a 2025-12-21.

**Gravação.** Com a reconstrução igual à API, a primeira rodada do coletor `b3_carteira_ibov` não deve gravar mudanças em 2026-10-23. A gravação no banco continua sendo decisão do dono; nada foi gravado nesta etapa.

## 15. Juíza: 100% dos pares, Jev por concordância e teto em US$ (2026-10-23)

**O problema.** A juíza julgava até `JUIZA_MAX_ITENS_DIA` pares por execução, sem teto em US$ e sem contar tentativas. Um par sem julgamento ficava pendente sem limite. O Jev estava desligado porque faltava uma medida de qualidade. O conjunto-ouro previsto (cerca de 300 pares rotulados pelo dono) nunca foi feito.

**Decisão.**

- **Julgar 100% dos pares, com tentativas.** Até `JUIZA_MAX_TENTATIVAS` (3) por par, em `v1-staging_juiza_tentativas`. Esgotado, o par sai da fila e, com o filtro da entrega ligado, fica de fora.
- **Teto em US$.** Calculado sobre `custo_usd` e `finalidade` em `v1-raw_llm_juiza_runs`. Juíza diária: US$ 3 por mês (`JUIZA_ORCAMENTO_MENSAL_USD`), dentro do total de US$ 5 por mês do Gemini, que segue compartilhado com a análise diária. Backlog: verba única de US$ 15 (`JUIZA_BACKLOG_VERBA_USD`), cumulativa. Concordância não tem teto.
- **Jev como segunda exceção paga.** US$ 0,042 por milhão de tokens de entrada, saída grátis, uma requisição por par. O custo entra nas somas da juíza.
- **Qualidade medida por concordância, sem conjunto-ouro.** `judge-news --concordancia N --semente S` compara Jev e Gemini numa amostra reprodutível. Se o mínimo entre a concordância de `papel` e a de `tipo_conteudo` for pelo menos 85%, o dono pode ligar `JUIZA_JEV_DECIDE`. Senão, o Jev fica em triagem, só com descartes de confiança pelo menos 0,9. Concordância não é acurácia: o TCC não terá precisão nem revocação por rótulo humano, e o número a reportar é a concordância, com essa ressalva.
- **Gemini Batch API rejeitada por ora.** O desconto é pequeno em valor absoluto, e o job assíncrono aumenta a complexidade.
- **Abertura do IBOV sem fonte alternativa.** A abertura repetida fica vazia na entrega (seção 11). Testado em 2026-10-23: o Stooq bloqueia com desafio JavaScript, a brapi exige token (HTTP 401) e a B3 (GetPortfolioDay e indexStatistics) só tem fechamento.

**Medição do Jev.** Chamada real de teste em 2026-10-23: HTTP 200, 507 ms, 453 tokens de entrada.

**Schema.** `sql/alteracoes/2026-10-23_juiza_custo_tentativas.mysql.sql`: `custo_usd` e `finalidade` em `v1-raw_llm_juiza_runs` (com o índice `ix_raw_llm_juiza_runs_finalidade`) e a tabela `v1-staging_juiza_tentativas`. Aplicar à mão, com usuário com DDL e aprovação do dono, antes de subir o código novo. Ordem completa em [08](08-operacao-e-deploy.md), seção 5.1. Com esta alteração, o schema tem 41 tabelas (14 sem prefixo e 27 com `v1-`).

**Código.** Já estava no working tree quando esta entrada foi escrita: `llm/juiza.py`, `llm/juiza_custo.py`, `llm/jev_client.py`, `llm/juiza_concordancia.py`, `cli.py` (`judge-news`, sem `--avaliar`) e `config.py`. `ENTREGA_FILTRO_JUIZA` segue `false` no código. A troca do padrão para `true` é a última fase (F9), feita só depois de o backlog zerar.

**Documentação.** `docs/04-juiza-e-llm.md` (seções 2.1, 2.4 a 2.7, 3.5, 3.6 e 4), `docs/08-operacao-e-deploy.md` (comandos, variáveis e passo a passo, seção 5.1), `docs/01`, `02`, `03` e `06` (contagens de tabelas e custo), `docs/ADR.md` (três ADRs novos e o do Jev marcado como substituído), `README.md`, `CLAUDE.md`, `docs/README.md` e notas de revisão na spec em `docs/superpowers/specs/`. O conjunto-ouro aparece nas seções antigas com a nota de remoção.

**Verificação.** `pytest -q`: 665 passed, 39 skipped, 1 xfailed. Nenhum teste lê os documentos. Ruff não foi rodado, porque a mudança é só de documentação.

**Não verificado.** A concordância ainda não foi medida, nem o backlog julgado. O custo real da juíza em produção também não foi medido.

## 16. Ações avulsas no painel: corrigir horas e concordância (2026-10-23)

**O problema.** Duas verificações que o dono rodava só pelo CLI (`fix-news-times` e `judge-news --concordancia`) não podiam ser disparadas pelo painel. A tela **Qualidade** precisa delas.

**Decisão.**

- **Dois coletores novos no catálogo** (`orquestrador/coletores.py`), disparados pelo comando `coletar` que já existe:
  - `corrigir_horas` chama `enrich.article_fetcher.corrigir_horas_publicacao`, a mesma função do CLI. Aceita `aplicar` (bool, obrigatório; `false` é a simulação, que não grava) e `limite` (int ≥ 1, opcional; `null` = sem limite).
  - `juiza_concordancia` chama `llm.juiza_concordancia.medir_concordancia`, a mesma do CLI. Aceita `n` (int de 10 a 1000, obrigatório) e `semente` (int, opcional, padrão 42).
- **Fora do daily e da carga histórica.** Não estão em `ETAPAS` nem em `historico._passos`. Não têm checkpoint: `refazer` não se aplica.
- **Função sem `observador`.** `ColetorDef` ganhou `recebe_observador` (falso para os dois). As funções existentes não mudam.
- **Resultado no log.** Cada contagem sai numa linha, com origem `daily`. Na concordância, as linhas são as do resumo, incluindo a decisão. Ela continua gravando a run-resumo, como já fazia.
- **Falta de chave** (Jev ou Gemini) vira erro do item, com mensagem clara, antes de qualquer chamada.
- **`montar_entrega`** segue como antes: vale para qualquer plano e é anexado no fim. Não recusei a combinação com ações avulsas, porque o padrão atual não recusa e o contrato pedia o mínimo.

**Código.** `orquestrador/coletores.py` (catálogo e `executar`), `orquestrador/coletar.py` (validação e log), `orquestrador/etapas.py` (`resultado_corrigir_horas` e `resultado_concordancia`).

**Testes.** `tests/test_coletores.py` (catálogo, alvos, observador, fora do daily e do histórico) e `tests/test_coletar.py` (aceitação, recusas, plano, chamada das funções por monkeypatch, log e falta de Jev).

**Documentação.** `docs/07-executor-e-painel.md` (seção 4.1: catálogo, regras e recusas), `docs/08-operacao-e-deploy.md` (seção 5.1, agora pelo painel, com o lote `sql/lotes/2026-10-23_lote.mysql.sql` aplicado à mão).

**Verificação.** Suíte inteira: 700 passed, 39 skipped, 1 xfailed. Ruff: sem erros em `src` e `tests`. Nenhum teste com banco foi habilitado.

**Não verificado.** A execução ponta a ponta pelo painel (o painel é outro repositório, editado em paralelo) e a medição real da concordância, que gasta API.

## 17. Escala de ações e balanços na entrega (2026-10-26)

**O problema.** Medido no banco real: a CVM publica a composição do capital sem coluna de escala. Cerca de 30% das linhas vêm em milhares e o resto em unidades. VALE3 saía sempre em milhares (4.255.763 em vez de ~4,26 bi). PSSA3 alternava trimestre a trimestre (646.586 → 646.586.060 → 646.586). Nos balanços, alguns documentos declaram a escala errada: SAUD3 (ITR 2021-06-30, UNIDADE com valores em milhares, enquanto os outros 21 documentos são MIL) e JBSS32 (DFP 2023-12-31, UNIDADE, com PL 1000x menor que o ITR seguinte em MIL).

**Decisão.**

1. **Ações.** O raw continua como a CVM publica. Na entrega, a série de cada CNPJ é quebrada onde o total salta ~1000x. Um segmento está em milhares quando a mediana do giro passa de 10% ao dia. O giro é a mediana do volume dos últimos 60 pregões, somado de ON, PN e unit, dividida pelo total de ações. Segmento sem giro (trimestres de antes da listagem, como AURE3 até 2022-03 e NATU3 até 2025-07) herda pela cadeia: a escala alterna a cada troca de 1000x a partir do segmento decidido pelo giro mais próximo. Valor isolado que não é troca de 1000x não é corrigido: só gera aviso (CMIN3 em 2025-12-31, 10x; AZUL3 em 2026-03-31, 18.000x).
2. **Balanços.** Documento cuja escala declarada difere da majoritária do CNPJ passa a usar a majoritária quando a magnitude fica 100x abaixo (UNIDADE em CNPJ MIL) ou acima (MIL em CNPJ UNIDADE) da mediana dos documentos na majoritária. A Vivara, que declara UNIDADE em tudo e está correta, não muda.

**Arquivos alterados:**

- `src/tcc_scraper/entrega/builder.py`: `volumes_por_cnpj`, `_corrigir_escala_acoes` e helpers do giro e dos segmentos, `montar_acoes_emitidas` (parâmetro `volumes`), `_corrigir_escala_balancos` e helpers, chamada em `montar_balancos`, e `_montar` para `acoes_emitidas`. Constantes `JANELA_GIRO_PREGOES` e `MIN_PREGOES_GIRO`.
- `tests/test_entrega_builder.py`: 12 testes novos (8 de ações, 4 de balanços).
- `docs/05-entrega.md`: seção 6 (regra de escala declarada errada) e seção 7 (regra de escala das ações).

**Verificação.** Suíte inteira: 712 passed, 39 skipped, 1 xfailed. Simulação em seco sobre o banco real, sem gravar: 503 de 1.847 linhas de ações mudam; depois da correção, nenhuma empresa fica com valor de mercado implícito abaixo de R$ 500 mi (antes, 16 tickers ficavam abaixo de R$ 10 mi). Nos balanços, mudam 3 linhas (SAUD3 e JBSS32).

**Aplicação (2026-10-27).** `build-entrega` rodado junto com a correção da AZUL3 (entrada 19): `v1-acoes_emitidas` com 1.850 linhas e `v1-balancos` com 2.694. Conferido no banco: VALE3 com 4.255.763.000 ações; SAUD3 no ITR 2021-06-30 com receita de R$ 906.007.000 e PL de R$ 1,09 bi.

**Saltos que continuam, sem ser escala:** SRNA3 com 10 e 100 ações antes do IPO (2021), IGTI11 com 87.889 antes da reorganização com a Jereissati (2021), AUAU3 com salto de 111x na fusão (2025-03) e AZUL3 na reestruturação (2026-03, com aviso).

**Limitações.**

- CNPJ sem nenhum volume em `raw_prices` não é corrigido. É o caso da STBP3: o Yahoo não devolve preço, e as 22 linhas seguem em milhares (851.018 a 864.170 ações, contra cerca de 863 milhões reais).
- Valor isolado na ponta da série (sem os dois vizinhos) não gera aviso.
- O giro soma o volume de unit com o de ON e PN do mesmo CNPJ, como pedido. Isso pode contar a mesma negociação duas vezes. A decisão não muda, porque o limiar de 10% tem mais de uma ordem de grandeza de folga.
- Sem `volumes`, `montar_acoes_emitidas` mantém o comportamento antigo.

## 18. Juíza refeita, medições e análise desligada (2026-10-26)

**O problema.** Em 2026-10-24, a carga histórica gastou US$ 2,24 em 13.144 pares julgados só pelo Gemini `gemini-3.1-flash-lite`, e parou com 402 (crédito esgotado). O Jev nunca tinha rodado: `JEV_ENABLED=true` sem `JEV_API_KEY` era ignorado em silêncio. A análise diária também depende do Gemini e ficou sem crédito. Faltavam medições de custo por par, do limiar do Jev e da LLM de fallback.

**Decisão.**

- **Juíza em três passos.** Regra barata, depois o Jev em `JUIZA_JEV_MODO=entrega` (ele decide "entra" ou "não entra" quando tem segurança), depois a LLM de fallback `gpt-5.6-luna` na OpenAI, com `reasoning.effort = none`, schema estrito e `store = false`. Fluxo em [04](04-juiza-e-llm.md), seção 2. Decisão no ADR de 2026-10-26.
- **Paradas explícitas** `jev_sem_chave`, `erro_jev` e `sem_credito`, no lugar da falha silenciosa.
- **Análise diária desligada** por `ANALISE_ENABLED=false`, padrão do código. A etapa `analise` sai como `pulada`, e o `analyze-llm` manual continua. A migração para a OpenAI está pendente.
- **Medições de 2026-10-26**, que substituem as estimativas e o limiar provisório:
  - Spike com 50 pares já julgados pelo Gemini, sem gravar nada: o Jev respondeu 50 de 50 em 3,7 s, com cerca de 1.155 tokens por par (US$ 0,000049). Com só papel e tipo, caiu para 725 tokens (−37%), com rótulos iguais em 47 de 50 e decisão de entrega diferente em 1 de 50. O dono decidiu manter as 4 perguntas.
  - Luna (`effort none`): 50 de 50 itens válidos, cerca de US$ 0,00009 por par sem cache, e cerca de US$ 0,00007 numa repetição com cache.
  - `judge-news --concordancia 500` (US$ 0,0245): limiar recomendado 0,9. Cobertura de 68,8%, concordância de 95,9%, precisão de 96,7% (entra) e 95,0% (não entra), kappa de 0,918 em 344 decididos. O modo `decide` segue reprovado. Em 28% dos pares decididos, o Gemini tinha tickers adicionais que se perdem.
  - Projeção: Jev em todos os pares e Luna nos cerca de 31% restantes dá cerca de US$ 0,00008 por par. O backlog de 13.144 pares sairia por cerca de US$ 0,97, contra US$ 2,24 pagos ao Gemini.
- **Documentação.** `docs/04` (seções 2 a 4), `docs/08` (seção 5.2), `docs/ADR.md` (frase de medição na decisão da juíza e ADR "Análise diária desligada até migrar para a OpenAI") e `CLAUDE.md` (seções 6 e 9).

**Arquivos alterados** (código, ainda sem commit):

- `src/tcc_scraper/config.py`: `ANALISE_ENABLED` (padrão `false`) e as variáveis da juíza, do Jev e da OpenAI.
- `.env.example`: variáveis novas da juíza, do Jev e da OpenAI.
- `src/tcc_scraper/entrega/filtros.py`: `entra_por_rotulos`.
- `src/tcc_scraper/llm/jev_client.py`: modos `entrega`, `triagem` e `decide`, com probabilidades.
- `src/tcc_scraper/llm/openai_client.py` (novo): cliente da Responses API.
- `src/tcc_scraper/llm/schema.py`: `schema_para_openai`.
- `src/tcc_scraper/llm/juiza_schema.py`, `llm/juiza_prompt.py` (prompt `juiza_openai`), `llm/juiza_custo.py` (`custo_llm` e `custo_resposta`; `custo_gemini` removido), `llm/juiza.py` e `llm/juiza_concordancia.py`.
- `src/tcc_scraper/orquestrador/etapas.py` (`PARADAS_FALHA` e análise desligável) e `orquestrador/coletores.py`.
- `src/tcc_scraper/cli.py`: `judge-news --referencia`, e `--status` ampliado.
- Testes: `test_config`, `test_jev_client`, `test_openai_client` (novo), `test_juiza_prompt`, `test_juiza_custo`, `test_juiza`, `test_juiza_concordancia`, `test_orquestrador`, `test_coletar`, `test_cli_coletores` e `test_cli_juiza` (novo).

**Verificação.** Suíte inteira: 797 passed, 39 skipped (testes com banco, opt-in), 1 xfailed. Ruff limpo.

**Não verificado.**

- O custo real do backlog em produção. Os números são de amostra e de projeção.
- A implantação no executor: chave nova do Jev e chave da OpenAI com crédito.
- A análise diária continua sem migração. Enquanto isso, as `staging_llm_*` não são atualizadas.

**Limitações para o TCC.**

- Concordância não é acurácia. Dois modelos que erram do mesmo jeito concordam.
- Perda de `tickers_adicionais` em 28% dos pares decididos pelo Jev.
- Assimetria de contexto: a referência viu 300 caracteres e o Jev vê 1.500. Na amostra, o trecho médio tinha 88 caracteres, então o Jev julgou quase só pela manchete.
- A chave atual do Jev vazou em uma transcrição e precisa ser substituída antes da implantação.

## 19. Preços da AZUL3 pela B3 (2026-10-26)

**O problema.** O Yahoo (`AZUL3.SA`) emenda papéis diferentes entre 2026-01-13 e 2026-04-17: fechamentos de 41,25 → 7,42 → 341 → 28 milhões, com volume alternando entre dezenas e centenas de milhões. Na COTAHIST da B3, nessa janela a ON da Azul negociou como AZUL53 (ISIN BRAZULA01OR8), cotada por lote de 1.000.000 de ações (FATCOT = 1000000). A partir de 2026-04-20 negocia como AZUL3 por ação, e ali o Yahoo bate com a B3. O Yahoo não registra os grupamentos de 75:1 (lastDatePrior 2026-02-18) e 150.000:1 (lastDatePrior 2026-04-17) que explicam a série.

**Decisão.**

- Na janela de 2026-01-13 a 2026-04-17, o preço da AZUL3 vem da COTAHIST. Preço por ação = preço ÷ FATCOT, dividido pelo produto dos grupamentos do suplemento com lastDatePrior ≥ data. Volume = QUATOT × o mesmo fator, arredondado. Fonte gravada: `b3_cotahist`.
- O CSV `data/correcoes_precos/AZUL3.csv` é versionado. Em cada execução, o coletor do Yahoo troca a janela pelo CSV (`aplicar_correcoes`). Como ele rebaixa o histórico inteiro e faz upsert, sem isso o Yahoo sobrescreveria a correção. Fora da janela, o Yahoo fica.
- A correção é uma lista explícita, `CORRECOES_PRECO_B3` em `config.py`. O comando `gerar-correcao-precos` baixa o ZIP anual da B3 (~96 MB) e lê o suplemento do banco, só em leitura.

**Arquivos alterados:**

- `src/tcc_scraper/collectors/b3_cotahist.py` (novo): `parse_linha`, `ler_cotahist`, `grupamentos_do_suplemento`, `serie_corrigida`, `gerar_correcoes`, `carregar_correcoes` e `CotahistCollector`.
- `src/tcc_scraper/collectors/prices_yahoo.py`: `PriceRow.source` (padrão `yahoo`), `aplicar_correcoes`, chamada em `process`, e `_persist` grava `r.source`.
- `src/tcc_scraper/config.py`: `CorrecaoPrecoB3` e `CORRECOES_PRECO_B3`.
- `src/tcc_scraper/cli.py`: comando `gerar-correcao-precos`.
- `data/correcoes_precos/AZUL3.csv` (novo, 66 linhas) e `.gitignore` (`!data/correcoes_precos/`).
- `tests/test_b3_cotahist.py` e `tests/test_prices_yahoo_correcoes.py` (novos, 36 testes).
- `docs/01-visao-geral.md`, `docs/05-entrega.md` e `docs/08-operacao-e-deploy.md`.

**Verificação.** Suíte inteira: 817 passed, 39 skipped, 1 xfailed. Ruff limpo nos arquivos alterados. CSV gerado pelo comando, 66 linhas de 2026-01-13 a 2026-04-17:

- Fechamento de 2026-01-13: 464,0602. O nominal (41,25 ÷ 1e6 × 75 × 150.000) dá 464,0625. A diferença de 5e-6 vem do fator publicado pela B3, 0,00000666670, que tem 6 algarismos significativos.
- Fechamento de 2026-02-18: 38,2498. Fechamento de 2026-04-17: 32,2498.
- Volume de 2026-01-13: 24.310.
- Continuidade: 2026-04-17 (32,25) → 2026-04-20 (30,54, igual no Yahoo e na B3).
- QUATOT em ações: em 2026-04-20 o volume do Yahoo (74.200) é igual ao QUATOT da B3. Preço × QUATOT ÷ FATCOT ÷ VOLTOT tem mediana 0,989 na AZUL53, entre 0,68 e 1,32. A razão foge de 1 porque VOLTOT usa o preço médio dos negócios, e PREULT é o último negócio.

**Aplicação (2026-10-27).** `collect-prices --no-resume` (92 de 92; sem dados no Yahoo: CRFB3, SRNA3 e STBP3), `build-dataset` (114.224 linhas em `features_dataset_v1`) e `build-entrega` (`v1-precos` com 143.686 linhas). Conferido no banco: a AZUL3 tem 66 pregões com `source = b3_cotahist` (2026-01-13 a 2026-04-17) e 131 do Yahoo a partir de 2026-04-20; o fechamento de 2026-04-14 passou de 29.852.850 para 29,85; `retorno_1d` e `volume_relativo` da AZUL3 voltaram a valores normais. Registrado no `docs/ADR.md` e no `CLAUDE.md` (seções 2 e 6).

**Limitações.**

- A correção é por lista explícita em `CORRECOES_PRECO_B3`. Outro papel com o mesmo problema precisa entrar lá e ter o CSV gerado.
- O fator publicado pela B3 é arredondado (erro de 5e-6 na escala). Nos retornos o efeito é desprezível, porque a escala é constante na janela.
- Os preços de 2026-01-13 a 2026-04-17 não são o Yahoo. Se a B3 republicar o COTAHIST, o CSV precisa ser regenerado.

## 20. Coleta híbrida de notícias: API WordPress e classificador por código (2026-10-27)

Decisão e opções: [ADR](ADR.md), seção "Coleta de notícias dos 3 veículos pela API do WordPress". Aqui ficam o que foi feito e o que foi medido.

**Medições.** Teste real de março de 2026: 11.306 posts no mês, em 122 requisições, sem erro. A API cobre 862 de 865 (99,7%) do que o Google trouxe desses sites, e a normalização de URL acertou 100%. Republicação entre veículos: 106 pares por mês, contados como 1 notícia. Throttle de 1,0 s: 216 requisições sem erro. Posts por ano nos 3 sites: 89,5 mil (2021), 93,8 mil (2022), 83,6 mil (2023), 84,6 mil (2024) e 106,1 mil (2025). Classificador contra 28.279 julgamentos do Gemini, com critério de n ≥ 30 e precisão ≥ 0,90: K3 com 91,5% no dev, 91,9% no teste e 92,4% em 2026-10 (aprovado); K6 com 100% (n = 31); eliminação de E5 e de "sem ocorrência" com 90,1% e 90,8% em 2026-10; K5 de 24% a 52% (reprovado, vai para a juíza); tipo pelo título de 52% a 65% (só marca o par como duvidoso).

**Módulos novos.** `collectors/wordpress.py` (download por site e mês), `enrich/wp_html.py` (limpeza do HTML em blocos), `enrich/casador.py` (casador v3, com os motivos de invalidação E1 a E6), `enrich/boilerplate.py` (índice de shingles causal), `enrich/historias.py` (republicações e duplicatas do Google), `enrich/processar_wp.py` (núcleo puro e gravação) e `llm/juiza_codigo.py` (classificador por camadas, com `REGRAS_VERSAO = 1`).

**Alterações de banco e de execução.**

- Tabelas novas: `v1-raw_wp_posts`, `v1-staging_historias` e `v1-staging_wp_processamento`. O ALTER está em `sql/alteracoes/2026-10-27_wordpress_historias.mysql.sql` e foi aplicado em 2026-10-27. O total de tabelas `v1-` passa de 27 para 30.
- A etapa `noticias` do daily tem 3 partes: `google_news`, `wordpress` e `processar_wp`. A etapa só sai `falhou` se as três falharem. Novos ids de coletor: `wordpress` e `processar_wp`.
- Novos comandos: `collect-wp` e `processar-wp`. Novas settings: `WP_ENABLED` (padrão `true`) e `THROTTLE_WORDPRESS` (padrão `1.0`), ambas em `.env.example`.
- Com `WP_ENABLED=true`, o Google News descarta os itens dos 3 domínios.
- Na entrega, artigo que é duplicata de história sai de `v1-noticias`, e `origem = 'wordpress'` é um valor válido.

**Limitações.**

- Os 3 itens que o Google trouxe e a API não cobre (2 de conteúdo de marca e 1 renomeado) não são recuperados.
- A coleta depende de um site público sem contrato. Se a API mudar, a coleta falha e precisa de ajuste.
- Panoramas de mercado com ticker no título continuam entrando. É a limitação já registrada na seção 2 do CLAUDE.md.
- O classificador por código é heurística. Se as listas de regra mudarem, é preciso medir de novo e subir `REGRAS_VERSAO`.
- A juíza fica com cerca de 26% dos pares do WordPress, a US$ 0,000077 por par (Jev mais Luna): cerca de US$ 0,11 por mês e US$ 5,5 no histórico.

## 21. Unidade em curso e último erro no progresso da carga (2026-10-27)

Motivo: com as duas trilhas paralelas da carga, a posição única de `v1-painel_historico` não mostra o que cada (ano, coletor) está fazendo. Além disso, uma unidade lenta parecia travada, e um erro de unidade ou de passo não aparecia no painel.

O que foi feito, sem commit:

- `v1-painel_historico_progresso` ganha `unidade_atual`, `ultimo_erro_unidade` e `ultimo_erro_mensagem`. O ALTER está em `sql/alteracoes/2026-10-27_historico_progresso_unidade_erro.mysql.sql`, aplicado pelo dono em 2026-10-27. O schema recebe a Etapa 0012 no fim. Como o dono aplica a DDL antes de publicar, o `ProgressoHistorico` novo depende dela.
- `ProgressoHistorico.inicio_unidade(key)` grava a unidade antes de ela rodar. `Collector.run` chama esse gancho por `getattr`, para não mudar o Protocol do observador.
- `unidade(ok=False)` grava o último erro na hora. O flush seguinte não apaga um erro anterior, mesmo num objeto novo depois de reinício.
- Exceção do passo: `_executar` grava `falhou` com `Tipo: mensagem` em `ultimo_erro_mensagem`. Passo concluído zera `unidade_atual`. Passo pausado mantém a unidade.

A DDL foi aplicada e o commit saiu junto com as demais tarefas do plano de trilhas (7f518e2).

## 22. Carga histórica em duas trilhas (2026-10-27)

Motivo: o Google News é o passo mais lento (6 a 9 horas por ano). Em sequência, o WordPress, a IPE e a juíza esperavam o Google terminar cada ano, e a fila inteira ficava parada atrás dele.

O que foi feito, sem commit:

- A carga roda em duas trilhas simultâneas. A trilha Google tem `google_news` e `enriquecimento`. A principal tem o resto, incluindo `wordpress`, `processar_wp`, `cvm_ipe` e a juíza de WordPress e IPE (id no painel `juiza_wp`, com `origens` = `wordpress` e `cvm_ipe`).
- Portão do ano: `finbert`, a juíza do ano e `build_entrega` só começam quando a trilha Google termina o último passo do ano (`enriquecimento`). O FinBERT e a juíza recebem `anos`, para pontuar e julgar só o ano que chegou.
- `ProgressoHistorico` aceita `abortar` e grava o progresso com o id do painel. `_Ritmo` tem lock próprio, porque as duas trilhas registram nele.
- Pausa: as duas trilhas estacionam numa `Condition` sobre o lock da carga. `pausada` só é gravada quando todas as trilhas ainda ativas estão paradas. Retomar acorda as duas.
- Falha de passo em uma trilha aborta a outra e leva a carga a `falhou`.
- Retomada depois de reinício: os passos com `concluido` em `v1-painel_historico_progresso` não se refazem. Os demais rodam de novo. `_indice_inicial` saiu.
- Testes em `tests/test_historico.py`: ordem por trilha, portão, pausa nas duas trilhas, falha cruzada, retomada pelo status e `_Ritmo` concorrente.

Pendente: a verificação com carga real (ver os riscos da seção 10 de [03](03-carga-historica.md)), a DDL de 2026-10-27 aplicada pelo dono e o commit junto com as demais tarefas do plano de trilhas.

## 23. Provedor Anthropic (`claude-haiku-5-5`) como padrão da juíza (2026-10-28)

**O que mudou.** A LLM de fallback padrão da juíza passa de OpenAI `gpt-5.6-luna` para a Anthropic `claude-haiku-5-5`, pelo crédito de US$ 100/mês da conta Anthropic. OpenAI e Gemini continuam como alternativas, escolhidas por `JUIZA_PROVEDOR`. Fluxo e variáveis em [04](04-juiza-e-llm.md), seções 2.3 e 3.

**Arquivos:**

- `src/tcc_scraper/llm/anthropic_client.py` (novo): `AnthropicClient` pela Messages API. Saída JSON estrita com `output_config` (`effort` e `format` `json_schema`). Não envia `thinking`, `temperature`, `top_p`, `top_k`, prefill nem `fallbacks`. Recusa vira texto vazio; `max_tokens` vira `incomplete`. 402, ou 400 com `credit balance`, vira `SemCreditoLLM`. A repetição de 408, 409, 429, 5xx e falha de conexão fica com o SDK (`max_retries`), sem tenacity.
- `src/tcc_scraper/config.py`: `juiza_provedor` com padrão `anthropic`; `MODELO_PADRAO_JUIZA["anthropic"]`; bloco `ANTHROPIC_*`; `juiza_chave_llm` cobre os três provedores.
- `src/tcc_scraper/llm/juiza.py`: despacho para o cliente Anthropic. `avisos_config` exige modelo `claude*` com `anthropic`, e recusa `claude*` com `openai` e `gemini`.
- `src/tcc_scraper/llm/juiza_custo.py`: tabela `anthropic` = US$ 0,10 de entrada, 0,01 de cache e 0,50 de saída por milhão de tokens.
- `src/tcc_scraper/llm/juiza_prompt.py` e `juiza_schema.py`: o provedor Anthropic usa o nome `juiza_anthropic` e o schema estrito da OpenAI, sem envelope. Os hashes de openai e gemini não mudaram.
- `pyproject.toml`: `anthropic>=2.3` nas dependências do core.
- `.env.example`: `JUIZA_PROVEDOR=anthropic`, bloco `ANTHROPIC_*` e OpenAI marcada como alternativa.
- Testes: `tests/test_anthropic_client.py` (novo, sem rede) e casos em `test_config.py`, `test_juiza_custo.py`, `test_juiza_prompt.py` e `test_juiza.py`.

**Decisões.**

- O padrão muda, mas `JUIZA_PROVEDOR=openai` continua funcionando. Quem já tinha `JUIZA_PROVEDOR=openai` no `.env` não muda de provedor.
- Os tetos não mudam: US$ 3/mês para a juíza diária e verba de US$ 15 para o backlog. Eles somam o Jev e a LLM, seja qual for.
- Custo medido com chamada real (300 pares, effort `medium`): US$ 0,000119 por par, todos os itens válidos, 34,7 s por lote de 50. Concordância de entrega com o Gemini: 86,7% (kappa 0,735). Detalhes em [04](04-juiza-e-llm.md), seção 3.6.
- A primeira chamada real deu 400: a Anthropic recusa `minimum`/`maximum` em campos `number`. O schema `anthropic` passou a sair sem esses limites (`juiza_schema._sem_limites`); a faixa continua validada pelo Pydantic.
- Sem `cache_control`, o cache do Claude não é usado. O pensamento já está em `output_tokens`, então não é cobrado duas vezes.

**Verificação.** `pytest` sem banco e `ruff check src tests` passam. Os testes do cliente usam um falso injetado e erros montados com `httpx2`, que é a biblioteca de HTTP do SDK 2.3.0.

**Pendências.**

- Colocar `ANTHROPIC_API_KEY` no `.env` do executor (máquina Processamento-Venturo).
- `docs/01-visao-geral.md`, `02-pipeline-diaria.md` e `08-operacao-e-deploy.md` ainda descrevem a OpenAI como padrão. Ficaram para a próxima revisão de docs.
- Sem commit: a mudança está no working tree do branch `feat/executor-painel`.

## 24. Google News: bloqueio por IP (503), ritmo de 5 s, só pt-BR, pausa progressiva; trilha Google não derruba a principal (2026-10-28)

**Diagnóstico.** A carga `hist-20261028-192314` falhou. O Google News respondeu HTTP 503 ("tráfego incomum") a todas as consultas do IP do executor. No log do executor, foram 181 consultas boas em 6 minutos, a 1 a cada 2 s. Depois vieram 10 falhas seguidas. Na mesma manhã, o IP já levava 503 desde a primeira consulta, por pelo menos 40 min. O código antigo perdeu 54 unidades sem avisar. Desta máquina, a mesma URL respondia 200. Conclusão: bloqueio por IP e por ritmo. O 503 não traz `Retry-After`, então não há espera indicada pelo servidor.

**Decisões do dono.**

- Ritmo mais lento, com pausa em vez de só aborto: `THROTTLE_GOOGLE_NEWS` de 2,0 para 5,0 s, mais `THROTTLE_GOOGLE_NEWS_JITTER_S=2.0` (sorteio de 0 a 2 s a cada espera, média ~6 s). O jitter evita um ritmo regular, que o Google reconhece.
- en-US desligado: `GOOGLE_NEWS_IDIOMAS=pt`. Medido em `raw_collection_runs`: o en-US era 50% das consultas e 12% das notícias (46.781 contra 334.417 do pt-BR). A carga tem metade das unidades, ~4.500 por ano.
- Pausa progressiva no lugar do aborto: ao atingir `GOOGLE_NEWS_MAX_FALHAS_SEGUIDAS` (10), a coleta pausa `GOOGLE_NEWS_PAUSAS_MIN=30,60,120` minutos. Só aborta depois das 3 pausas sem sucesso.
- Trilha Google não derruba a principal: falha da Google para só a trilha Google. A carga termina `falhou`, mas o restante da principal roda até o fim.

**Mudanças.** Código e testes no working tree; detalhes de comportamento em [03](03-carga-historica.md), seção 3.1.

- `src/tcc_scraper/config.py`: `throttle_google_news` = 5,0; `throttle_google_news_jitter_s`; `google_news_idiomas` (`pt`); `google_news_pausas_min` (`30,60,120`), com validadores e as funções `parse_idiomas` e `parse_pausas`.
- `src/tcc_scraper/collectors/base.py`: `DomainThrottle` aceita jitter em `configure`, com padrão 0 (os outros domínios não mudam). `FalhaBusca` guarda o `motivo` (`HTTP 503` ou o nome da exceção), que vai à mensagem. `ParadaSolicitada`: a unidade interrompida pela parada não é gravada e segue pendente.
- `src/tcc_scraper/collectors/google_news.py`: `process` chama `_pausar_por_bloqueio` ao atingir o limite. `_dormir_pausa` dorme em passos de 5 s e consulta `deve_parar`. A pausa aparece no painel como log `aviso` e como unidade atual. Os locales vêm de `google_news_locales_ativos`.
- `src/tcc_scraper/orquestrador/historico.py`: `_falha_google` e `_google_encerrada`. A falha da trilha Google só para ela. Passos com portão de ano que a Google não concluiu são pulados, com `aviso`. A carga termina `falhou` com erro `trilha google: ...`. As esperas (`join`, `_esperar_portao`, `_aguardar`) viraram laços com timeout.
- `.env.example`: os valores novos e os comentários.
- Testes: `tests/test_historico.py` (+143 linhas), `tests/test_google_news_robusto.py` e `tests/test_google_news_janelas.py` (ajustes). Novos: `tests/test_collector_motivo_jitter.py`, `tests/test_google_news_idiomas.py`, `tests/test_google_news_pausa.py` e `tests/relogio_falso.py` (relógio e sono falsos, para não esperar 30 min reais).
- Docs: `docs/03-carga-historica.md`, `docs/08-operacao-e-deploy.md` e esta seção.

**Verificação.** `pytest`: 1066 passed. `ruff check`: ok.

**Pendências.**

- Medir uma carga real no ritmo novo e confirmar que o bloqueio não volta.
- Sem commit: a mudança está no working tree do branch `feat/executor-painel`.
- `CLAUDE.md` (seção 6, "6–9 h") e `docs/ADR.md` ainda citam os números antigos de tempo e de idiomas. A revisão fica com o dono.

## 25. Escala e base das ações emitidas; eventos de ações na entrega (2026-11-02)

**O problema.** A quantidade de ações da CVM vem em milhares ou em unidades, e não diz qual base de ações é a de hoje. Um grupamento depois da data do documento deixa a série numa base antiga: AZUL3 em 2026-03 tem 54,7 trilhões de ações antes do grupamento 150.000:1 de 2026-04-17, e ~364,9 milhões na base atual, coerente com 2026-06 (368,6 milhões). Por isso AZUL3 não é erro de escala. Há também erros de dígito (CMIN3 em 2025-12, GMAT3 em 2022-06) e documentos anulados por retificação. O caso do IRBR3 mostrou que um DFP entregue depois de um grupamento já vem na base nova, e converter só pelos eventos aplicaria o grupamento duas vezes.

**Decisão.**

1. **Camadas de correção da fonte, só na entrega** (o raw não muda). Escala por giro (`corrigido_escala`, a regra de 2026-10-26); erro de dígito isolado, dividido por 10^k quando os dois vizinhos concordam (`corrigido_digito`, CMIN3 e GMAT3), com a última linha da série só marcada `suspeito`; anulação por lista explícita, `ACOES_CVM_ANULADAS` em `config.py` (`anulado`, quantidade NULL: GMAT3, SRNA3, IGTI11 e AUAU3).
2. **Base atual.** `qtd_on_base_atual` e `qtd_pn_base_atual` aplicam os eventos da classe com data-com a partir da data de referência, na mesma base do fechamento ajustado e de `valor_por_acao`. Um evento entre a data de referência e a data de entrega só é aplicado se a quantidade ainda não o refletir (comparação com a linha anterior, tolerância de 5%).
3. **`data_entrega`** (`DT_RECEB`) em `v1-acoes_emitidas`, para mostrar quando o dado chegou.
4. **Tabela nova `v1-eventos_acoes`**, 10ª tabela da entrega: desdobramentos, bonificações e grupamentos do suplemento da B3. Chave (`ticker`, `data_com`, `tipo`, `isin_emitido`). Fica de fora incorporação, cisão, resgate e redução ou restituição de capital, porque o sentido do fator não foi verificado e eles não mudam a contagem da própria classe. A troca de código continua em `v1-mudancas_ticker`.

**Arquivos alterados (documentação e esquema).**

- `sql/alteracoes/2026-11-02_acoes_base_atual_eventos.mysql.sql` (novo): as 4 colunas em `v1-acoes_emitidas` e a tabela `v1-eventos_acoes`. Aplicado à mão pelo dono.
- `docs/spec-entrega-etapa-seguinte.md`: adendo de 2026-11-02 no fim, sem alterar o texto original.
- `docs/05-entrega.md`: 10 tabelas; seção 7 com as camadas, a base atual e a regra do IRBR3; seção 11 nova para `v1-eventos_acoes`; seções seguintes renumeradas (12 e 13).
- `docs/06-banco-de-dados.md`, `docs/ADR.md` (seção "Escala e base das ações emitidas") e `CLAUDE.md` (contagem de tabelas com prefixo `v1-`).
- Contagens de tabelas de entrega em `docs/01`, `docs/02`, `docs/03`, `docs/08` e `docs/README.md`.
- `src/tcc_scraper/db/models_entrega.py`: só a docstring do módulo.

**Verificação.** Esta entrada cobre a documentação. Pendente: rodar `build-entrega` depois da DDL e conferir os casos citados (CMIN3 em 2025-12, GMAT3 em 2022-06, IRBR3 em 2022-12, AZUL3 em 2026-03 e RENT em 2025-12).

**Pendências.**

- Confirmar com a CVM ou com o dono se AUAU3 (2023-12 a 2024-12) e IGTI11 (2021-03 a 2021-09) são anulados ou têm valor real. Hoje é hipótese.
- A base atual não é point-in-time. A análise que precisar da quantidade da época usa `qtd_on` e `qtd_pn`.
- CNPJ antigo fora do cadastro atual da B3 não tem eventos; bonificação paga em outra classe não ajusta a quantidade da classe de `isin`; CNPJ com mais de uma classe PN com fatores conflitantes fica com `qtd_pn_base_atual` NULL.
- O registro no grafo (`manage_adr`) não foi feito nesta entrada; a cópia de referência é `docs/ADR.md`.

## 26. Emissor por período, balanços (IFRS 17 e reapresentação) e hora das notícias do Google News (2026-11-03)

**O problema.** Três problemas independentes, tratados no mesmo dia. **(A)** Troca de código que veio com troca de companhia: JBSS3 virou JBSS32 com a JBS N.V. e NTCO3 virou NATU3 com a Natura Cosméticos. Um mesmo ticker passou a ter dois CNPJs, e a série de preço ganhou uma alta falsa de +100% do JBSS32 em 2025-06-09 (o Yahoo emendava JBSS3 sem ajuste) e quatro dias falsos de NATU3 a R$ 36,86 (placeholder do Yahoo). **(B)** Balanços: a HAPV3 publica a despesa de seguros (IFRS 17) dentro da conta 3.01, então a receita de 1T25 saía em R$ 1,47 bi, contra R$ 7,92 bi no 3T25. A JBS N.V. publicou o 1T25 com receita 0 e reapresentou depois; a regra de "só o ÚLTIMO" mantinha o documento zerado. SAUD3 tem salto de escopo no próprio dado da CVM. **(C)** Datas do Google News: o `fix-news-times` nunca selecionava as matérias com hora falsa. No MySQL, `TIME(x) IN ('07:00:00','08:00:00')` compara como número e não casa nada.

**Decisão.**

**A. Emissor por período (itens 3 JBS e 4 Natura do consumidor).** `EMISSORES_POR_PERIODO` em `config.py` lista o emissor de cada ticker por período (`EmissorPeriodo`): JBSS32 tem JBS S.A. (CVM 20575, JBSS3 até 2025-06-08, fator 0,5 = 1 BDR para 2 ações) e JBS N.V. (CVM 80233, desde 2025-06-09). NATU3 tem Natura Cosméticos (CVM 19550) até 2019-12-17, Natura &Co Holding (CVM 24783, NTCO3, de 2019-12-18 a 2025-07-01, troca 1:1) e Natura Cosméticos de novo desde 2025-07-02. Qual emissor vale é decidido pela **data de entrega à CVM** (`dt_receb`, `data_entrega` do IPE, data-com dos proventos), não pela data de referência, que deixaria o 2T25 da Natura sem dono. A data de entrega não gera vazamento. O ticker das tabelas é sempre o atual. Os coletores CVM (demonstrações, cadastro, IPE) passam a coletar os códigos antigos (`codigos_cvm_universo`); o coletor de proventos da B3 ganha as unidades `prov|JBSS|20575` e `prov|NTCO|24783`. Na entrega: balanços e ações filtram pelo emissor válido na data; a quantidade da base atual é multiplicada pelo `fator_acoes`; proventos são convertidos ao ticker atual (JBSS3 para JBSS32, com valor ÷ 0,5; NTCO3 para NATU3); proventos de BDR que só existem no suplemento entram; notícias do IPE só ficam no ticker se o emissor valia na data (as 7 notas da Natura Cosméticos ligadas a NATU3 saem de `v1-noticias`). Tabela nova `v1-emissores_ticker` (ticker, ordem, ticker_negociado, cd_cvm, cnpj, empresa, vigencia_inicio, vigencia_fim, fator_acoes), a 11ª tabela da entrega e a 32ª com prefixo `v1-`. A gravação da entrega passa a apagar linhas obsoletas de `balancos`, `acoes_emitidas`, `proventos` e `emissores_ticker`, só quando a montagem produz linhas. **Preço:** `CORRECOES_PRECO_B3` ganha `JBSS32` (série JBSS3 de 2019-12-02 a 2025-06-06, `fator_troca` 0,5: preço ×2, volume ×0,5) e `NATU3` (série NTCO3 de 2019-12-18 a 2025-07-01). O `adj_close` da correção passa a ser ajustado por proventos em dinheiro da B3 (fator = 1 − provento ÷ fechamento na data-com) e emendado na base do Yahoo pelo primeiro pregão depois da janela. O CSV ganha as colunas `fator_troca` e `fator_dividendos`.

**B. Balanços (item 7).** O coletor de demonstrações guarda também `ORDEM_EXERC` PENÚLTIMO (comparativo reapresentado) e as subcontas `3.01.NN` no raw. Sem DDL; o raw cerca de dobra. `receita_liquida` subtrai da `3.01` a despesa de seguros publicada dentro dela (subconta `3.01.NN` negativa cuja descrição começa com "despesa" e contém "seguro"; IFRS 17). Só a HAPV3 casa (1T25, 2T25, 1T26 e 2T26): o 1T25 passa de R$ 1,47 bi para R$ 7,80 bi. **Reapresentação:** se a receita do ÚLTIMO vem 0 ou ausente e uma remessa posterior traz o mesmo período como PENÚLTIMO com receita diferente de zero, a linha usa o comparativo dessa primeira remessa, e `data_entrega` passa a ser a dela (sem vazamento). O documento original fica inválido (PL e posições em branco; sai da D&A acumulada). Caso real: JBS N.V. 1T25, publicado com receita 0 em 2025-05-27 e reapresentado com R$ 114,1 bi em 2026-05-12. Coluna nova `situacao` em `v1-balancos` (`ok`, `ajuste_ifrs17`, `reapresentado`; `reapresentado` tem precedência). **SAUD3** (BRADSAÚDE) salta de ~R$ 0,6 bi para ~R$ 8 bi por trimestre entre 1T26 e 2T26 no próprio dado da CVM (mudança de escopo): não corrigido, só documentado. **TIMS3** e **BBSE3** publicam `3.01` = 0 na DFP 2025: fora do escopo.

**C. Datas do Google News (item 8).** Corrigido o filtro de `buscar_candidatos_hora` em `enrich/article_fetcher.py`: passa a usar `HOUR IN (7, 8) AND MINUTE = 0 AND SECOND = 0`. A fase (a) do `fix-news-times` remarca as 131.659 matérias com 00:00 do Pacífico gravado como hora exata para precisão `date`, com `published_at` às 00:00 de São Paulo do mesmo dia. O dia não muda, então `features_dataset_v1` não muda. Em `v1-noticias`, essas linhas passam a ter `data_hora_publicacao` vazia e `momento_pregao` = `desconhecido`. A fase (b), que busca a hora na página (~15.900 páginas), é opcional e pode rodar depois. O `UPDATE` de `raw_articles.published_at`, `published_precision` e `published_time_source` pelo `fix-news-times` é uma exceção à regra de raw append-only, registrada no `CLAUDE.md` (seção 5) e na seção 3.3 de `docs/06-banco-de-dados.md`. O caso "matérias de 2022 datadas em 02/06/2026" não foi reproduzido no banco.

**D. Dividendos do Yahoo para o JBSS32 (2026-11-03).** A B3 não devolve os proventos da JBS S.A. depois de 2019-04-30, e o Yahoo tem os de 2020 a 2025 (medido). `CorrecaoPrecoB3.proventos_yahoo` (`JBSS32.SA`) faz `gerar_correcoes` buscar esses eventos na janela da correção, com data-com = pregão anterior à data ex, e gravar `data/proventos_yahoo/JBSS32.csv`. O fator de dividendos da série e a entrega (`v1-proventos`, `montar_proventos`) leem esse CSV. Sem DDL. Limite: o Yahoo não distingue JCP de dividendo, então tudo entra como `DIVIDENDO`.

**Arquivos alterados.**

- Código (conforme o `git status` do início da sessão): `src/tcc_scraper/config.py`; `collectors/cvm_demonstracoes.py`, `cvm_cadastro.py`, `cvm_ipe.py`, `b3_proventos.py`, `b3_cotahist.py`, `prices_yahoo.py`; `enrich/article_fetcher.py`; `entrega/builder.py`; `db/models.py`; `db/models_entrega.py`; `sql/schema.mysql.sql`; testes em `tests/` (incluindo `test_emissores_periodo.py`, novo).
- SQL: `sql/alteracoes/2026-11-03_emissores_ticker.mysql.sql` (novo) e `sql/alteracoes/2026-11-03_balancos_situacao.mysql.sql` (novo). Aplicação no banco é do dono.
- Documentação: `docs/spec-entrega-etapa-seguinte.md` (adendo de 2026-11-03, sem alterar o texto anterior); `docs/05-entrega.md` (11 tabelas, seção 12 nova para `v1-emissores_ticker`, seções seguintes renumeradas para 13 e 14, balanços, proventos, ações, IPE e preço); `docs/06-banco-de-dados.md`; `docs/08-operacao-e-deploy.md`; `docs/01`, `docs/02`, `docs/03`, `docs/README.md` (contagens); `docs/ADR.md` (duas decisões novas); `CLAUDE.md` (seções 2 e 5).

**Verificação (2026-11-03, banco real).** DDLs aplicadas; recoletados cadastro, proventos, IPE 2021-2025, ITR/DFP 2020-2026 e preços; `gerar-correcao-precos`, `build-dataset` e `build-entrega --csv` rodados. JBSS32: retorno de 2025-06-09 = +0,18% (era +100%); na data ex de 2024-08-20 o fechamento cai 5,3% e o `adj_close` 0,08%. NATU3: preços reais de NTCO3 de 2019-12-30 a 2025-07-01, sem o R$ 36,86. HAPV3: 1T25 = R$ 7,80 bi e 2T25 = R$ 7,96 bi (12 linhas `ajuste_ifrs17`). JBS 1T25 = R$ 114,1 bi, pelo ITR da JBS S.A. (o da N.V. entregue antes da listagem não entra; nenhuma linha `reapresentado`). JBS S.A.: 31 balanços (1T20 a 1T25); proventos do JBSS32 de 2020 a 2025 pelo Yahoo. Natura &Co: 31 balanços e 3 proventos (B3 sob "GRUPO NATURA"). `fix-news-times` fase (a): 131.659 matérias remarcadas para `date`.

**Pendências.**

- A B3 não devolve proventos da JBS S.A. depois de 2019 nem os da Natura &Co sob "NATURA &CO": o primeiro vem do Yahoo (item D) e o segundo, de "GRUPO NATURA".
- `volume_relativo` do JBSS32 tem quebra de regime: o BDR negocia cerca de 1% do volume da JBSS3.
- SAUD3: salto de escopo sem correção.
- Fase (b) do `fix-news-times` (hora pela página, ~15.900 páginas): rodada em 2026-11-03 (ver entrada 27).
- A regra de IFRS 17 casa só a HAPV3. Outras companhias com a mesma conta, se aparecerem, exigem revisão.
- Os ADRs desta entrada foram gravados em `docs/ADR.md`; o registro no grafo (`manage_adr`) não foi feito.

## 27. Host do executor na Processamento-Venturo, `build_dataset` no executor, painel com novas tabelas, fase (b) em lotes (2026-11-03)

**O problema.** Os docs ainda tratavam o Railway como host do executor. Na prática, o executor roda como serviço Windows NSSM `tcc-executor` (conta LocalSystem) na máquina de processamento do dono, "Processamento-Venturo", e no Railway fica só a API do painel. Além disso, o dataset de 14 colunas (`features_dataset_v1`) só era montado à mão pela CLI `build-dataset`, fora do daily e da carga histórica.

**O que mudou.**

- **Documentação do host.** A máquina de processamento é o host real do executor. A alternativa Railway (`Dockerfile` e `railway.json`) ficou marcada como não usada, com o aviso de que a imagem exclui `data/`. O procedimento de atualização está em [08](08-operacao-e-deploy.md), seção 4.1. Arquivos: `CLAUDE.md` (seção 6), `docs/02`, `docs/03`, `docs/06`, `docs/07` (seção 12), `docs/08` (seções 2.6, 3.5, 3.6, 4, 5, 5.1, 5.2 e 6.1), `docs/README.md`, `docs/ADR.md` (decisão nova) e o comentário de cabeçalho de `scripts/coleta-completa.sh`.
- **`build_dataset`, novo coletor.** Monta `features_dataset_v1`, igual à CLI `build-dataset`. Na etapa `entrega` do daily, roda antes das 11 tabelas da entrega; se falhar, a entrega roda mesmo assim e a etapa sai `falhou`. Na carga histórica, roda em cada ano logo depois de `build_entrega`, na trilha principal, com o mesmo portão do ano. É aceito no comando `coletar`.
- **Painel.** Aceita as tabelas `v1-eventos_acoes` e `v1-emissores_ticker` e o coletor `build_dataset`.
- **Fase (b) do `fix-news-times`.** Grava em lotes de 200 páginas (commit 9e6894f). Rodada no banco real em 2026-11-03, com 15.896 páginas baixadas e 0 falhas:
  - 15.318 horas corrigidas e gravadas;
  - 515 com a hora da página fora da janela, que continuam `date`;
  - 63 páginas sem hora.

  Em seguida, a `v1-noticias` foi remontada (408.605 linhas).

**Pendências.**

- A ordem difere entre os dois caminhos (daily: dataset antes da entrega; carga: depois), sem efeito: as tabelas da entrega não leem `features_dataset_v1`.
- Na carga, uma falha do `build_dataset` deixa a carga `falhou`, como qualquer passo; no daily, a entrega roda mesmo assim. Retomar a carga no modo pendentes.
- As DDLs de 2026-10-27 já estavam aplicadas (conferido no banco em 2026-11-03).

## 28. Bancos e seguradoras no banco (`v1-ref_instituicoes_financeiras`) e indicador de balanços do painel (2026-11-04)

**O problema.** O indicador "Contas novas" do painel contava Itaú (19348), BTG (22616) e Santander até 2025-03 (20532) como faltando. Recoletar não mudava nada, porque esses bancos não publicam 1.01.01/1.01.02 no BPA consolidado da CVM (conferido no ITR 2025), e a spec já deixa essas colunas em branco para bancos e seguradoras.

**O que mudou.**

- Tabela `v1-ref_instituicoes_financeiras` (camada nova `ref_`) com os 8 códigos. Ela substitui `CD_CVM_BANCOS_SEGURADORAS`, que saiu de `config.py`. Quem grava é o dono, por `sql/alteracoes/2026-11-04_instituicoes_financeiras.mysql.sql`.
- O builder lê a tabela no `build-entrega` e falha a montagem de `balancos` se ela estiver vazia. O log avisa quando um banco ou seguradora do IBOV (`IBOV_SECTORS`, fora B3SA3 e ITSA4) não está na tabela.
- A SQL e o texto do card no painel excluem esses códigos do total e do faltando.

**Pendências.**

- Resolvido em 2026-11-04: `sql/alteracoes/2026-11-04_instituicoes_financeiras.mysql.sql` aplicado (8 linhas) e ADR regravado no grafo. No banco real, a consulta nova do indicador deu `faltando = 0` em todos os anos (2020 a 2026).
- Falta atualizar o executor na Processamento-Venturo. Sem o código novo, ele continua usando a lista antiga, com o mesmo resultado.

## 29. Notícias órfãs, Raízen em lote, lacunas de preço, balanços individuais e D&A na entrega (2026-11-09)

**O problema.** Pedido do consumidor da entrega, em cinco frentes independentes. **(A)** `v1-noticias` tinha 18.136 linhas órfãs: republicações gravadas antes do agrupamento de histórias, que a remontagem não apagava. **(B)** Copel (CPLE): o desdobramento 10:1 de 2021-03-11 só gerava evento para CPLE3, e a base dava 5,77 vezes a quantidade real. **(C)** Raízen (RAIZ4): desde 2026-11-03 a B3 cota RAIZ55 por lote de 100, e o Yahoo repete o preço do lote em RAIZ4. Além disso, 76 pregões de 2020 a 2026 não existem no Yahoo. **(D)** Balanços: a CVM não traz a consolidada em alguns documentos, e a entrega ficava com dado zerado ou ausente; a regra de PL, lucro, dívida e D&A também tinha lacunas. **(E)** Proventos: `data_pagamento` só existe desde 2025, e a unidade do JBSS32 depois de 2025-06-09 não está confirmada.

**O que mudou.**

- **A. Notícias.** `_Fontes.fora_da_montagem` → `_Poda.sem_linha` → `_ids_a_apagar`: a remontagem apaga as linhas de artigos que viraram republicação (`article_id` ≠ `historia_article_id` em `v1-staging_historias`) e as de artigos sem título, inclusive fora da janela. `montar_noticias` não gera linha sem título. O coletor do Google News descarta o item cujo título fica vazio depois de tirar o sufixo da fonte (3 casos "Página 3"). As horas corrigidas pelo `fix-news-times` chegam a `v1-noticias` só na remontagem.
- **B. Ações e eventos.** `SUFIXO_ISIN` ganha PA (PNA, classe 5) e PB (PNB, classe 6); o desdobramento da Copel gera eventos de CPLE3, CPLE5 e CPLE6. `qtd_pn_base_atual` fica NULL nas linhas anteriores à troca PN → ON de `MUDANCAS_TICKER` (AZUL4 → AZUL3, CPLE6 → CPLE3, ELET6 → AXIA3). O fator do grupamento da Azul (0,0000066667) é gravado sem arredondar.
- **C. Preços.** `FATORES_COTACAO_YAHOO` (RAIZ4, fator 100, a partir de 2026-11-03), aplicado por `aplicar_fator_cotacao`, com trava de salto de 50%. `MUDANCAS_TICKER` ganha RAIZ4 → RAIZ55. Lacunas do Yahoo: `gerar-lacunas-precos` gera `data/lacunas_precos/lacunas.csv` (COTAHIST para papéis, IBOV da B3 para `^BVSP`), e `completar_lacunas` aplica o CSV na coleta, com escala pela razão mediana dos vizinhos.
- **D. Balanços (CVM).** Coleta das demonstrações individuais (`_ind_`), com a coluna `consolidacao` na PK de `v1-raw_cvm_balancos` (`sql/alteracoes/2026-11-09_raw_cvm_balancos_consolidacao.mysql.sql`). Escolha por documento em `_escolher_consolidacao`, com `situacao` = `individual`. Lucro aceita o rótulo de banco e a conta 3.13 das seguradoras. PL pela conta de nível 2 com rótulo "Patrimônio Líquido". Dívida em "Outros" da Allos (`conta_divida_em_outros`). Sai do filtro o período de 1 mês (LREN3 e RECV3 no ITR 1T21). `_DA_EXCLUI` passa a excluir "equivalência patrimonial" (falso positivo da EQTL3). Acumulado anterior de outro documento só por `CNPJ_ACUMULADO_ANTERIOR`.
- **E. Proventos.** Só documentação; sem mudança de código.

**Decisões do dono.**

- Base PN de classe extinta fica NULL nas trocas PN → ON (AZUL4, CPLE6 e ELET6). A conversão não é modelada como evento, e a empresa é a do ticker ON novo. É limitação para o TCC.
- D&A sem comparativo de documento posterior (PENÚLTIMO), porque o valor só ficou público depois da `data_entrega` da linha. Por isso ficam em branco GMAT3 (2020 e 2021), AURE3 (2021 e 2022), BRAV3 no 2T20, TIM no 2T22 e SBSP3 no 3T23.
- Sem ajuste do ex-direito de subscrição da Raízen (09/11/2026). O efeito máximo é de cerca de 11%.

**Respostas sem mudança.** BBSE3 tem receita 0 porque a CVM publica a `3.01` zerada em todos os documentos. NATU3 no 1T25 está no CNPJ da holding 32785497000197, emissora de NATU3 até 2025-07-01.

**Arquivos alterados.** Código e testes, conforme o `git status` de 2026-11-09: `src/tcc_scraper/config.py`, `collectors/prices_yahoo.py`, `collectors/b3_cotahist.py`, `collectors/cvm_demonstracoes.py`, `collectors/google_news.py`, `cli.py`, `db/models_fontes.py`, `entrega/builder.py`, `orquestrador/historico.py`, `sql/schema.mysql.sql` e os testes em `tests/` (novos: `test_b3_cotahist_lacunas.py`, `test_prices_yahoo_fator_cotacao.py`, `test_prices_yahoo_lacunas.py`). SQL: `sql/alteracoes/2026-11-09_raw_cvm_balancos_consolidacao.mysql.sql` (novo, aplicado pelo dono). Documentação: `docs/01`, `docs/05` (seções 2.3, 3, 5, 6, 7, 8, 11 e 14), `docs/06` (seções 3.2 e 3.3), `docs/08` (seções 2.2, 5 e 5.3), `docs/ADR.md` (cinco decisões) e `CLAUDE.md` (seções 2 e 6).

**Verificação.** Esta entrada só documenta. Os testes novos e a suíte completa não foram rodados ao escrevê-la. Medições registradas: 18.136 linhas órfãs; 76 lacunas (75 em 2020-11-20 e IVVB11 em 2025-10-24); 5,77 vezes na Copel; BTG 4T20, Vivo 1T25 e WEG 1T21 como casos em que o documento já traz a base nova.

**Pendências.**

- Rodar `pytest` com os testes novos antes do commit.
- Recoletar a CVM (`collect-cvm --no-resume`) e remontar a entrega no banco real, na ordem da [08-operacao-e-deploy.md](08-operacao-e-deploy.md), seção 5.3.
- Gerar e commitar `data/lacunas_precos/lacunas.csv`. O diretório ainda não existe no repositório.
- Encerrar a janela da Raízen quando a B3 voltar a cotar RAIZ4 por ação.
- Confirmar a unidade do JBSS32 depois de 2025-06-09 pelo aviso oficial.
- O registro das decisões no grafo (`manage_adr`) não foi feito. A cópia de referência é `docs/ADR.md`, e o `manage_adr update` envia o arquivo inteiro.

## 30. Raízen fora de `v1-mudancas_ticker` (2026-11-09)

**O problema.** O consumidor lê `ticker_novo` de `v1-mudancas_ticker` como a chave de todas as tabelas da entrega. Na troca ELET3 → AXIA3, por exemplo, cadastro, preços e notícias só têm AXIA3. A troca RAIZ4 → RAIZ55, que entrou na entrada 29, quebrava essa leitura: a entrega inteira segue RAIZ4, e o cadastro não tem RAIZ55. Com isso a Raízen sumia do histórico do consumidor, cerca de 250 linhas por ano de 2023 a 2025.

**O que mudou.** `MUDANCAS_TICKER` perde RAIZ4 → RAIZ55. O comentário passa a dizer que código de cotação por lote fica fora da tabela, como os códigos intermediários (AZUL54/AZUL53). `mudancas_ticker` entra em `TABELAS_COMPLETAS`, para que a troca retirada do config saia da tabela na remontagem. Teste novo: `test_codigo_de_cotacao_por_lote_nao_vira_troca_de_ticker`.

**Decisão do dono.** Desfazer a troca, em vez de adotar RAIZ55 como chave. O RAIZ55 é código de cotação por lote de 100 (Ofício Circular B3 022/2026-VPE), provavelmente temporário. Se a Raízen trocar de código de vez (grupamento ou units), a troca entra com a data dessa troca.

**Respostas sem mudança de código.**

- Aumento de capital privado da Raízen (ex-direito em 09/11/2026): não entra em `v1-eventos_acoes`, que só tem desdobramento, grupamento e bonificação. A subscrição não multiplica as ações de quem já é acionista. As ações novas aparecem em `v1-acoes_emitidas` no primeiro ITR ou DFP depois da homologação.
- MGLU3 3T20 e IRB 4T22: o documento já traz a quantidade depois do evento (desdobramento 4:1 de 2020-10-13 e grupamento 30:1 de 2023-01-24). `_fator_pendente` reconhece o caso pela razão com o trimestre anterior, e `qtd_*_base_atual` sai certa.
- NATU3 3T24: a D&A acumulada de 9 meses de 2024 (682,0 mi) é menor que a do primeiro semestre (799,2 mi), e o comparativo de 2023 encolheu do mesmo jeito. A subtração dá negativa e a linha fica NULL. O semestre reapresentado só sai em documento posterior, que a regra de D&A não usa.

**Verificação.** `pytest`: 1269 passed, 40 skipped, 1 xfailed. `ruff` sem achados novos.
