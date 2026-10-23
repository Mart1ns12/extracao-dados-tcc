# CLAUDE.md — Pipeline de Coleta do Dataset 1.0

Sistema de web scraping que produz um dataset diário de **notícias + mercado B3** para a etapa
seguinte do TCC (análise/interpretação — **fora deste escopo**).

Índice da documentação: [docs/README.md](docs/README.md).

---

## 1. O que este projeto entrega

Uma linha por `(data, ticker)` com as **14 colunas** de `docs/Dataset1.0.xlsx`:

| Coluna | Origem |
|---|---|
| `data` | Pregão (calendário do `^BVSP`) |
| `ticker` | Componente do IBOV (85 ativos) |
| `quant_noticias` | Notícias únicas nas 24 h anteriores |
| `positivo` / `neutro` / `negativo` | Média das probabilidades do FinBERT |
| `score_finbert` | `positivo − negativo` |
| `retorno_1d` / `retorno_5d` / `retorno_20d` | `adj_close` acumulado |
| `volatilidade_20d` | Desvio padrão dos retornos de 20 pregões |
| `volume_relativo` | Volume do dia ÷ média de 20 pregões |
| `retorno_ibovespa` | Variação do `^BVSP` |
| `target` | `1` se a ação superar o Ibov em **t+1**, senão `0` |

**Janela: 1 ano agora, 5 anos planejados** (`DATA_START` a `DATA_END` no `.env`; a janela do v1
é fixa, e o fim da coleta diária é rolante, `DATA_FIM_COLETA`). Os preços são coletados
`PRICE_WARMUP_DAYS` (400 dias) ANTES de `DATA_START`, para aquecer as janelas de 20
pregões — sem isso, os 20 primeiros dias do dataset sairiam sem `retorno_20d`,
`volatilidade_20d` e `volume_relativo`. Medido: com aquecimento, essas colunas saem com
**0% de nulos** (antes: 4%).

**O dataset é materializado em `features_dataset_v1`** pelo `build-dataset`, além de
exportado em Parquet/CSV. Upsert por `(data, ticker)`: remontar sobrescreve, não duplica.

**Restrição dominante do projeto: custo zero, exceto a LLM do Google (teto US$ 5/mês) e o Jev.**
A análise diária por LLM (Gemini) e a juíza de notícias são as dependências pagas. A juíza usa o mesmo Gemini, com teto próprio de US$ 3/mês e verba única de US$ 15 para o backlog, dentro do total de US$ 5/mês. O Jev (TypeSafe AI) é a segunda exceção paga, aprovada em 2026-10-23, a US$ 0,042 por milhão de tokens de entrada; o custo dele entra nas somas da juíza.
Qualquer outra proposta que exija API paga, cartão de crédito ou plano pago deve ser rejeitada.

---

## 2. Fontes — decisões já tomadas (NÃO reabrir sem motivo novo)

Estas conclusões vieram de **testes com chamadas reais**, não de documentação. Não reintroduza
uma fonte rejeitada sem antes provar, com uma chamada real, que a limitação mudou.

### Aprovadas

| Fonte | Papel | Por quê |
|---|---|---|
| **Yahoo Finance chart API** | Preço **primário** | 5 anos com `adjclose`, sem chave. Cobre ações `.SA`, `^BVSP`, `BZ=F` (Brent), `USDBRL=X` |
| **Google News RSS** | Notícias **históricas** | Único caminho grátis para 2 anos de histórico. Aceita `after:`/`before:` |
| **RSS nativos dos veículos** | Notícias **correntes** (sem coletor implementado) | InfoMoney/MoneyTimes expõem `content:encoded` = corpo completo do artigo |
| **BCB SGS série 432** | Selic | Oficial, sem chave, sem limite |
| **BCB PTAX** | Dólar | Oficial, sem chave. Usar `cotacaoVenda` |
| **Brapi** | *Fallback* de preço (sem coletor implementado) | Funciona sem token; free = 15.000 req/mês. Só 1 ano de histórico |

### Rejeitadas — e o motivo

| Fonte | Motivo da rejeição |
|---|---|
| **Marketaux** | Free = 100 req/dia × **3 artigos por request** e **sem histórico**. Impossível cobrir 85 ativos × 2 anos |
| **GDELT DOC 2.0 API** | Rate-limitado em 100% das tentativas de teste ("one request every 5 seconds") |
| **GDELT arquivos brutos** | Funcionam, mas 2 anos ≈ **300 GB** de download. Inviável localmente |
| **FRED** | Exige chave e o Yahoo já entrega Brent (`BZ=F`) sem chave |

### Limites operacionais que moldam o código

1. **Google News RSS satura em 100 itens por query.** Janela semanal não satura (~52 itens);
   mensal satura. Por isso a coleta é **semanal** e subdivide a janela ao bater 100.
2. **O link do Google News é um ID opaco** — não é URL. Não tente `curl -L` (fica em
   news.google.com) nem decodificar o base64 (não contém URL). A resolução é feita pelo
   endpoint interno `batchexecute`, em `enrich/link_resolver.py` — **medido em 42/42 = 100%**
   sobre links reais de 3 janelas temporais distintas. Como o endpoint não é documentado,
   `resolve()` devolve `None` em vez de levantar: o pipeline degrada para manchete-apenas.
3. **Nunca** coletar sem throttle. Toda requisição externa passa por `collectors/base.py`
   — inclusive o `resolve()`, que antes chamava o cliente HTTP cru e martelava o
   `news.google.com` sem limite (passava despercebido com 60 artigos; com 11 mil, não).
4. **Carteira do IBOV validada** contra o Yahoo: 88/88 tickers. 15 códigos estavam
   desatualizados por reestruturação societária e já foram corrigidos em `config.py`
   (ex.: Embraer `EMBR3→EMBJ3`, Eletrobras `ELET3→AXIA3`). Ao mexer na lista, rode
   `tcc-scraper validate-tickers`.

### Achados do piloto (2026-09-07) — já corrigidos

**1. Nomes ambíguos saturavam a busca.** `("Vale" OR VALE3)` devolveu **100 itens** (o teto),
cheios de panorama de mercado e do verbo "valer". Saturar significa que notícia real também
foi cortada. Com contexto setorial: 48 itens, zero panoramas. VALE3 caiu de 2.764 → 357
artigos/mês, agora proporcional ao ITUB4 (289). Ver `SEARCH_QUALIFIERS` em `config.py` —
só para nomes que são palavra comum; qualificar um nome distintivo reduziria a cobertura.

**2. Séries macro viravam linhas do dataset.** `assemble_dataset` inferia os ativos por
exclusão ("tudo menos o benchmark"), então Brent (`BZ=F`) e dólar (`USDBRL=X`) apareciam
como se fossem ações — e o dólar, sem volume por ser câmbio, inflava os nulos de
`volume_relativo` de 4% para 21%. Agora o universo é **explícito** (`universe=`).

**3. Limitação conhecida, não corrigida:** panoramas de mercado ("Ibovespa recua com Vale e
Petrobras") continuam entrando. O Google News não distingue "notícia sobre X" de "notícia
que menciona X". É defensável (menção em panorama é sinal de co-movimento), mas precisa
constar como limitação no TCC.

---

## 3. Protocolo de descoberta de código (OBRIGATÓRIO)

Este projeto usa **codebase-memory-mcp**. Para explorar código, use o grafo **antes** de Grep/Glob —
retorna resultados estruturados em ~500 tokens contra ~80k de um grep amplo.

**Ordem obrigatória ao procurar código:**

1. `search_graph(name_pattern=".*Collector.*", label="Class")` — localizar símbolos por padrão
2. `trace_path(function_name="build_dataset", direction="both", depth=3)` — **antes de editar**,
   para saber quem chama e o que é chamado
3. `get_code_snippet(qualified_name="tcc_scraper.features.dataset.build_dataset")` — ler o símbolo exato
4. `get_architecture()` — visão de estrutura ao entrar em uma área nova
5. `search_code(pattern=...)` — busca textual aumentada pelo grafo

**Grep / Glob / Read continuam livres** (e preferíveis) para: `.env`,
`pyproject.toml`, arquivos SQL de `sql/`, Markdown, fixtures de teste e qualquer arquivo não-Python.

**Sempre `Read` um arquivo antes de editá-lo** — o grafo diz onde está, não substitui a leitura.

---

## 4. Protocolo de ATUALIZAÇÃO da memória (OBRIGATÓRIO)

O grafo só é útil se estiver atualizado. Estas são as regras de manutenção:

| Quando | Ação |
|---|---|
| Projeto ainda não indexado (`list_projects` não mostra) | `index_repository` — **antes** de qualquer exploração |
| Criou/renomeou/moveu **módulo ou pacote** | `index_repository` novamente |
| Antes de commitar | `detect_changes()` — mapeia o diff para os símbolos afetados |
| Em dúvida se o índice envelheceu | `index_status` |
| Tomou uma **decisão arquitetural** | `manage_adr` — registra o *porquê* no grafo |

**Editar o corpo de uma função existente NÃO exige reindexar.** Só mudanças estruturais
(novos arquivos, novos símbolos, imports novos) invalidam o grafo.

### ARMADILHA VERIFICADA: `index_repository` apaga o ADR

Confirmado por teste nesta sessão: depois de reindexar, `manage_adr(mode="get")` passou a
retornar `no_adr`. O grafo é reconstruído do zero e leva o ADR junto.

**Regra:** toda vez que rodar `index_repository`, **regrave o ADR logo em seguida** com
`manage_adr(mode="update")` e confirme com `manage_adr(mode="get")`. A cópia de referência
fica versionada em `docs/ADR.md` — restaure a partir dela.

### O que registrar como ADR (`manage_adr`)

Decisões cujo *motivo* se perde se ficar só no código:

- Rejeição de uma fonte de dados e o limite concreto que a inviabilizou
- Resultado do spike de resolução de link do Google News (a taxa medida e a rota escolhida)
- Escolha de MySQL como banco único (sem banco local)
- Qualquer mudança na regra do `target` ou na definição de uma feature

Um ADR bem escrito responde: **qual era o problema, quais opções existiam, o que foi escolhido e
o que se perdeu com isso.**

---

## 5. Convenções de código

- **Todo coletor herda de `collectors/base.Collector`.** Ele já resolve throttle por domínio,
  retry exponencial e checkpoint. Nunca chamar `requests`/`httpx` cru em um coletor.
- **Persistência sempre por `db/upsert.bulk_upsert`.** Nunca `mysql_insert`
  direto, e nunca um `execute` por linha dentro de laço — foi assim que o `_persist` do
  Google News chegou a ~450 mil idas ao banco na carga completa (hoje: ~8,6 mil).
- **Tipo de ponto flutuante é `Double`, nunca `Float`.** `sa.Float()` vira 4 bytes no
  MySQL e truncaria as probabilidades do FinBERT em ~7 dígitos, mudando o dataset
  conforme o banco. Há teste de regressão.
- **Timestamps usam `UTCDateTime`**: o MySQL não guarda fuso e, sem o tipo, o valor voltaria
  naive, deslocando a agregação diária.
- **A camada `raw` é append-only, com exceções de UPDATE.** Reprocessamento sempre parte dela — nunca da rede. As exceções reais são upserts que atualizam `raw_prices`, `raw_macro`, `raw_articles.resolved_url`, `v1-raw_cvm_cadastro` e `v1-raw_b3_cadastro` (ver [docs/06-banco-de-dados.md](docs/06-banco-de-dados.md), seção 3.3).
- **Deduplicação por `url_hash`**, nunca pela URL crua (querystrings de tracking variam).
- **Alteração de schema é manual:** muda o model em `src/tcc_scraper/db/`, atualiza `sql/schema.mysql.sql` e escreve o ALTER em `sql/alteracoes/AAAA-MM-DD_descricao.mysql.sql`. Quem aplica é o dono, com usuário com DDL, após aprovação. Nada de `CREATE TABLE` solto.
- **Prefixo `v1-` só nas tabelas criadas pela pipeline nova** (`PREFIXO_TABELA` em `db/models.py`):
  as que nasceram nas antigas migrations 0004–0007 e nas alterações depois delas (27 tabelas: `painel_*`,
  fontes/juíza e as 9 de entrega, sendo a 9ª `v1-composicao_ibov`, de 2026-10-23). O nome real é `v1-painel_logs`, `v1-raw_ipe_docs`, `v1-noticias` … `v1-indices` (o CSV
  continua com o nome da spec). As 14 antigas mantêm o nome, inclusive `raw_articles`, `raw_prices` e
  `raw_llm_runs`; ficam sem prefixo também `features_dataset_v1`. O hífen exige
  crase em SQL escrito à mão: `` `v1-raw_ipe_docs` ``.
  No código, prefira `Model.__table__`.
- **Segredos só em `.env`** (nunca commitado). `.env.example` documenta as chaves.
- **Vazamento temporal é bug de corretude**, não detalhe: features em `t` só usam dados com
  timestamp `<= t`. O `target` é o único campo que olha `t+1`. Há teste dedicado para isso.
- Timestamps sempre em **UTC** no banco; conversão para `America/Sao_Paulo` só na agregação diária.

---

## 6. Comandos

```bash
# Banco: MySQL 8 do .env, o UNICO do projeto (nao ha banco local). Schema e alteracoes: docs/06-banco-de-dados.md, secao 4.
# banco vazio: aplicar sql/schema.mysql.sql (usuario com DDL)
python -m tcc_scraper.cli db-info              # confere o banco e as tabelas (so leitura)
python -m tcc_scraper.cli validate-tickers     # tickers do IBOV respondem no Yahoo
python -m tcc_scraper.cli validate-cadastro    # ticker -> codigo CVM -> B3
python -m tcc_scraper.cli collect-prices       # Yahoo: acoes, Ibovespa, Brent, dolar
python -m tcc_scraper.cli collect-macro        # Selic e PTAX (BCB)
python -m tcc_scraper.cli collect-news         # Google News (retomavel; 6-9 h na carga completa)
python -m tcc_scraper.cli enrich               # corpo dos artigos (resolve o link)
python -m tcc_scraper.cli score                # FinBERT pt/en (exige o extra sentiment)
python -m tcc_scraper.cli build-dataset        # 14 colunas -> features_dataset_v1, Parquet e CSV
python -m tcc_scraper.cli analyze-llm [--data YYYY-MM-DD] [--dry-run] [--modelo X]   # analise diaria (Gemini)
python -m tcc_scraper.cli collect-ipe          # fatos relevantes (CVM IPE)
python -m tcc_scraper.cli collect-cvm          # ITR e DFP (CVM)
python -m tcc_scraper.cli collect-cadastro     # cadastro das companhias (CVM e B3)
python -m tcc_scraper.cli collect-proventos    # proventos (B3)
python -m tcc_scraper.cli collect-indices      # CDI e IPCA (BCB)
python -m tcc_scraper.cli judge-news [--limite N] [--backlog [--anos A] [--orcamento-usd X]] [--status] [--concordancia N [--semente S]]   # juiza de noticias
python -m tcc_scraper.cli build-entrega [--csv] [--tabela X]    # 9 tabelas da entrega
python -m tcc_scraper.cli executor             # worker do painel (Railway): comandos, daily das 08:00 BRT, carga historica
python -m tcc_scraper.cli daily [--etapa ID]   # daily de 9 etapas, sem painel (bloqueante)
python -m tcc_scraper.cli historico --anos 2025 --anos 2026 [--modo pendentes]  # carga historica (bloqueante)

pytest                                          # testes (sem rede, usam fixtures)
TCC_TESTES_COM_BANCO=1 pytest                   # testes com banco: gravam no MySQL do .env (opt-in)
```

A coleta de notícias leva **6–9 h** na primeira execução (9.010 janelas + subdivisões).
É **retomável**: se interromper, rodar o mesmo comando continua de onde parou.

Para rodar tudo em sequência no servidor: `./scripts/coleta-completa.sh` (ver
[docs/08-operacao-e-deploy.md](docs/08-operacao-e-deploy.md), seção 1).

**Executor e painel:** ver [docs/07-executor-e-painel.md](docs/07-executor-e-painel.md).

---

## 7. Configuração do codebase-memory-mcp

O servidor está registrado **globalmente** em `~/.claude.json` desta máquina
(`C:/Users/consu/.local/bin/codebase-memory-mcp.exe`), por isso o `.mcp.json` do projeto
fica vazio: um caminho absoluto de Windows commitado no repositório quebraria em qualquer
outra máquina e entraria em conflito com a configuração global que já funciona.

**Para usar em outra máquina**, instale o servidor e registre-o no escopo do usuário:

```bash
claude mcp add --scope user codebase-memory-mcp <caminho-do-executavel>
```

Confirme com `claude mcp list` e, dentro do projeto, com `list_projects`.
Se o projeto não aparecer, rode `index_repository` antes de qualquer exploração.

---

## 8. Banco: MySQL 8, único

O projeto tem **um banco só**: o MySQL do `.env` (`IP_BANCO`, `USER_BANCO`, `SENHA_BANCO`,
`PORTA_BANCO`, `NOME_BANCO`). Não existe PostgreSQL nem banco local; a URL é montada sempre pelo
`Settings.effective_url`. Decisão e custo em `docs/ADR.md` ("Um banco só").

Decisões de tipo e de SQL que continuam valendo:

| Ponto | Por quê |
|---|---|
| **Camadas viram prefixo** (`raw_articles`), não schema | MySQL não tem schemas — lá "schema" e "database" são a mesma coisa |
| **`Double`, nunca `Float`** | `sa.Float()` vira 4 bytes no MySQL: `0.7766114473342896` voltava `0.776611` |
| **`UTCDateTime`** | MySQL não guarda fuso; sem isso, matéria das 22h cairia em dias diferentes |

E o `RETURNING` **não existe no MySQL** — por isso `google_news._persist` grava em lote,
relê os ids pelo `url_hash` e só então grava os vínculos: três queries por janela,
independentemente do número de matérias.

Schema e acesso: **[docs/06-banco-de-dados.md](docs/06-banco-de-dados.md)**.

**Pegadinha do MySQL:** a collation `utf8mb4_0900_ai_ci` ignora acentos em `LIKE` —
`LIKE '%ã%'` devolve linhas a mais do que o esperado. Não afeta o pipeline (nenhuma query dele
usa `LIKE`), mas afeta consultas de análise.

**Testes com banco são opt-in.** Gravam no banco real, por isso só rodam com
`TCC_TESTES_COM_BANCO=1`; sem a variável são pulados. Cada um limpa só as linhas que criou.

---

## 9. Análise diária por LLM (Gemini)

Camada **independente do scraper**: 1 chamada/dia ao Gemini com grounding do Google Search e
JSON schema **na mesma chamada** (só Gemini 3+ aceita os dois juntos). Modelo em
`GEMINI_MODEL` (padrão `gemini-3.8-flash`). Tabelas: `raw_llm_prompts` (prompt versionado),
`raw_llm_runs` (append-only, uma linha por tentativa, mesmo em erro) e `staging_llm_*`
(substituídas por dia). O `features_dataset_v1` **fica intacto**.

**Exceção aprovada à regra de custo:** é a dependência paga principal do projeto, com teto de
US$ 5/mês (~30 chamadas), compartilhado com a juíza, que tem teto próprio de US$ 3/mês e verba de US$ 15 para o backlog (ver [docs/04-juiza-e-llm.md](docs/04-juiza-e-llm.md), seção 3.6). Roda como etapa `analise` do daily, agendada pelo executor às 08:00 de Brasília (ver [docs/07-executor-e-painel.md](docs/07-executor-e-painel.md)).

**Citações:** o prompt exige `fonte` e `url` dentro de cada item do JSON. As anotações
`url_citation` da API vão para `citacoes_json` só como auditoria, nunca como fonte de dados.

**Mudou o template → sobe `VERSAO` em `llm/prompt.py`** e cria `analise_diaria_v{N+1}.txt` (hoje a versão é `v3`);
nunca edite a versão antiga no lugar.

Variáveis e custo: [docs/04-juiza-e-llm.md](docs/04-juiza-e-llm.md), seção 3. Deploy e banco remoto: [docs/08-operacao-e-deploy.md](docs/08-operacao-e-deploy.md), seção 4.
