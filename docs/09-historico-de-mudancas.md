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
- Juíza com persistência e conjunto-ouro (T26).
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
- **Aprovação do Jev** como segunda exceção paga do projeto. Enquanto isso, `JEV_ENABLED=false`.
- **Conjunto-ouro** para `judge-news --avaliar`. A rotulagem é do dono.
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
- **`judge-news --avaliar` sem chave** deveria avisar e sair com código 0. Não foi conferido.
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
