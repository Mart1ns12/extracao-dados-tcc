# ADR — cópia de referência

> **Este arquivo é a cópia versionada do ADR armazenado no codebase-memory-mcp.**
>
> `index_repository` **apaga** o ADR do grafo (verificado por teste). Depois de reindexar,
> restaure com `manage_adr(mode="update")` usando o conteúdo abaixo (a partir de `## PURPOSE`)
> e confirme com `manage_adr(mode="get")`.
>
> Ao alterar o ADR, atualize **os dois**: o grafo e este arquivo.

---

## PURPOSE

Pipeline de web scraping que produz o **Dataset 1.0** (`docs/Dataset1.0.xlsx`): uma linha
por (data, ticker) com 14 colunas — contagem de notícias, sentimento FinBERT
(pos/neu/neg/score), retornos 1/5/20d, volatilidade 20d, volume relativo, retorno do
Ibovespa e um target binário (1 se a ação superar o Ibov em t+1).

Cobre os 85 ativos do IBOV, ~2 anos de histórico, notícias em pt e en. Restrição
dominante: **custo financeiro zero**. Exceção única e aprovada: a análise diária por LLM do Google, com teto de US$ 5/mês. A etapa de análise/modelagem está fora deste escopo.

## STACK

Python 3.11+ · MySQL 8 (único banco; camadas por prefixo `raw_`, `staging_`, `painel_`) · SQLAlchemy 2.0 (schema em `sql/schema.mysql.sql`) ·
httpx · lxml · trafilatura · pandas + pyarrow · transformers/torch (extra opcional) ·
typer + rich · pytest + ruff · google-genai (extra opcional llm).

## ARCHITECTURE

Quatro camadas, cada uma persistindo seu resultado:

1. **Coleta** (`collectors/`) — Google News RSS (notícias), Yahoo chart API (preços),
   BCB SGS/PTAX (macro). Tudo herda de `collectors/base.Collector`, que resolve throttle
   por domínio, retry exponencial e checkpoint retomável.
2. **Enriquecimento** (`enrich/`) — resolve o link opaco do Google e extrai o corpo do
   artigo com trafilatura, restrito à allowlist de veículos.
3. **Sentimento** (`sentiment/`) — roteador por idioma sobre dois checkpoints FinBERT.
4. **Features** (`features/`) — retornos, volatilidade, agregação de notícias, target.

`raw` é imutável e append-only: é o único lugar onde a rede escreve. Trocar o modelo de
sentimento ou a regra do target é reprocessamento local, sem repetir as ~17 mil
requisições (~10 h) da coleta.

## PATTERNS

- **Todo coletor herda de `Collector`.** Nunca `httpx` cru: throttle e checkpoint são
  obrigatórios, não opcionais.
- **Checkpoint por unidade de trabalho** (`collection_runs`, chave estável tipo
  `PETR4|2024-06-03|pt`). Sem isso, uma queda na hora 9 de 10 perderia tudo.
- **Upsert em toda persistência.** Conflito é o caso normal: a mesma notícia aparece nas
  consultas de vários tickers.
- **Deduplicação por `url_hash`** (SHA-256 da URL canonicalizada, sem parâmetros de
  tracking) — nunca pela URL crua.
- **Vínculo N:N** `article_tickers`: uma matéria cita vários ativos sem ser duplicada.
- **Ausência de dado é NULL, não zero.** Dia sem notícia tem `quant_noticias = 0` e
  sentimento nulo; preencher com 0 afirmaria "neutro medido", que é falso.
- **Falha de enriquecimento degrada, não interrompe.** `link_resolver.resolve()` devolve
  `None` e o artigo entra com a manchete.

## TRADEOFFS

**Google News RSS sobre Marketaux/GDELT.** Marketaux: free = 3 artigos por requisição,
sem histórico — inviável para 85 ativos × 104 semanas. GDELT DOC API: rate-limitado em
100% das tentativas. GDELT bruto: ~300 GB para 2 anos. O Google News aceita
`after:`/`before:` e entrega histórico de graça. Custo: não é API oficial, pode mudar.

**Janela semanal.** Imposição da fonte, não preferência: o RSS satura em 100 itens por
consulta (mensal = 100, semanal = 52). Janelas maiores causariam perda silenciosa — o
código subdivide em dias ao atingir o teto.

**Yahoo sobre Brapi para preços.** Yahoo entrega 5 anos com `adjclose` sem chave; Brapi
free entrega 1 ano. Brapi permanece como fallback. Efeito colateral: o FRED tornou-se
desnecessário (Brent vem de `BZ=F`).

**Dois modelos de sentimento.** ProsusAI/finbert é treinado em inglês e metade do corpus
é pt. Traduzir ~130 mil manchetes seria caro e ruidoso; `lucas-leme/FinBERT-PT-BR` é
nativo. Detalhe crítico: a ordem dos rótulos difere entre checkpoints — o código lê
`model.config.id2label`, porque assumir posições fixas inverteria o sinal do dataset
inteiro sem gerar erro.

**PostgreSQL sobre SQLite.** Decisão do usuário; SQLite foi oferecido (zero infra) e
recusado. Custo: exige servidor rodando. *Superada pela ADR "Um banco só": o PostgreSQL saiu do projeto.*

**Carteira do IBOV congelada em código.** Uma lista dinâmica tornaria o dataset
irreprodutível meses depois. Validada contra o Yahoo: 88/88. 15 códigos corrigidos por
reestruturação societária (Embraer `EMBR3→EMBJ3`, Eletrobras `ELET3→AXIA3`, JBS
`JBSS3→JBSS32`, Copel `CPLE6→CPLE3`, Natura `NTCO3→NATU3`, Odontoprev `ODPV3→SAUD3`,
Azul `AZUL4→AZUL3`, Petz `PETZ3→AUAU3`, CCR `CCRO3→MOTV3`, BRF+Marfrig→`MBRF3`).
Removidos por deslistagem: `CRFB3`, `STBP3`, `SRNA3`.

**`url_hash` sobre a URL de descoberta.** A URL real vai para `resolved_url`, coluna
separada. Sobrescrever `url` dessincronizaria o hash e quebraria a dedup.


**Qualificadores de busca para nomes ambíguos.** Medido no piloto: `("Vale" OR VALE3)`
saturou o teto de 100 itens com ruído (panoramas e o verbo "valer") — e saturar corta
notícia real. Com contexto setorial, 48 itens e zero panoramas; VALE3 caiu de 2.764 para
357 artigos/mês. `SEARCH_QUALIFIERS` cobre só os nomes que são palavra comum.

**Universo explícito no dataset.** Inferir os ativos por exclusão fez Brent e dólar virarem
linhas do dataset no piloto, inflando os nulos de `volume_relativo` de 4% para 21%. Agora
`assemble_dataset` recebe `universe`.


**Um banco só: MySQL do `.env` (2026-10-20).** Supera a ADR de portabilidade logo abaixo. Problema:
o projeto mantinha dois bancos com o mesmo código (PostgreSQL local para desenvolvimento e MySQL de
produção), duas suítes de paridade e dois caminhos de configuração (`DB_TARGET`, `DATABASE_URL`). Isso
dobrava a superfície de teste e de manutenção, e a paridade virou um teste que só comparava o
dataset entre dois bancos. Opções: (a) manter PostgreSQL + MySQL; (b) um MySQL local em Docker;
(c) um banco só, o MySQL do `.env`, que já tem os dados reais (88 mil artigos, preços, dataset v1).
Escolhido (c): `Settings.effective_url` monta sempre `mysql+pymysql` a partir de `IP_BANCO`,
`USER_BANCO`, `SENHA_BANCO`, `PORTA_BANCO` e `NOME_BANCO`; `db/session.py`, `db/upsert.py` e
`UTCDateTime` ficam só com o caminho MySQL; `docker-compose.yml`, os scripts `pg-*` e a cópia
`migrar_dados.py` saem do repositório. As decisões de tipo e SQL (`Double`, `UTCDateTime`, sem
`RETURNING`, o `ON DUPLICATE KEY UPDATE` com deduplicação do lote) continuam valendo.
Custo que se perde: (1) não há banco de desenvolvimento. O executor local e os testes com banco
gravam no banco real; por isso os testes com banco são **opt-in** (`TCC_TESTES_COM_BANCO=1`) e cada
um limpa só as linhas que criou; (2) a suíte padrão não exercita SQL de verdade, só o que roda sem
banco; (3) DDL não tem rollback no MySQL, então um ALTER ruim atinge produção. Por isso cada alteração é
revisada antes e aplicada à mão da máquina local, com usuário com DDL e aprovação do dono, seguindo
[Banco de dados](06-banco-de-dados.md), seção 4. As antigas migrations 0001–0007 não foram editadas; a 0008 foi reescrita antes de
ser aplicada em qualquer banco (ver a ADR do prefixo), e as que têm ramo `with_variant` seguem como histórico.

**Portabilidade PostgreSQL + MySQL 8** *(superada pela ADR acima; mantida como registro).* O banco de produção do TCC é MySQL, e o
desenvolvimento segue em PostgreSQL. Três incompatibilidades reais foram encontradas
testando contra um MySQL 8.4 de verdade, não lendo documentação:

1. `ON CONFLICT` é exclusivo do PostgreSQL → camada `db/upsert.bulk_upsert`.
2. `sa.Float()` vira 4 bytes no MySQL → probabilidades truncadas de
   `0.7766114473342896` para `0.776611`; o dataset **mudava conforme o banco**. Corrigido
   com `Double`, com teste validado por mutação.
3. `RETURNING` não existe no MySQL → `google_news._persist` relê os ids pelo `url_hash`.

Diferença que não dá para abstrair, então está documentada: o PostgreSQL **aborta** um
`ON CONFLICT DO UPDATE` cujo lote traga a mesma chave duas vezes ("cannot affect row a
second time"), enquanto o MySQL aceita em silêncio. Como a resposta do Google News
repete matéria dentro da janela, `bulk_upsert` deduplica antes de enviar — sem isso a
coleta quebraria no PostgreSQL e produziria resultado dependente do banco.

**Inserts em lote.** Os seis `_persist` gravavam uma linha por `execute` dentro de laço.
Medido com dados reais: uma janela de 52 matérias custava 156 statements, hoje custa 3
(52× menos). Na carga completa: ~450 mil → ~8,6 mil idas ao banco.

**Janela de 1 ano com aquecimento de preço.** O escopo caiu de 2 para 1 ano, o que
reduz a coleta de notícias de ~17.850 para 9.010 janelas (de 13–18 h para 6–9 h). O
recorte é aplicado DEPOIS do cálculo das features: o preço anterior a `data_start` vira
aquecimento das janelas de 20 pregões. Efeito medido: `retorno_20d`, `volatilidade_20d`
e `volume_relativo` passaram de 4% de nulos para **0%**. Como os preços de 2 anos já
estavam coletados, o aquecimento saiu de graça.

**Dataset materializado em `features_dataset_v1`.** Antes o `build-dataset` só exportava
Parquet/CSV e a tabela ficava vazia. Agora grava com upsert por `(data, ticker)`:
remontar com outra regra de feature sobrescreve as linhas em vez de acumular. `NaN` vira
`NULL` — gravar 0 afirmaria "sentimento neutro medido", que é diferente de "não medido".

**Enriquecimento paralelo, com a fila intercalada por veículo.** O `enrich` baixava
um artigo por vez. Paralelizar é seguro porque o `DomainThrottle` é por domínio e tem
lock: os workers se serializam sozinhos no `news.google.com` e correm em paralelo nos
veículos.

Só que paralelizar sozinho **piorou** o tempo (38 s contra 32 s numa amostra de 24
artigos reais). A causa: os artigos chegavam agrupados por veículo — 17 dos 24 eram do
mesmo domínio — e os 6 workers ficavam todos na mesma fila do throttle, pagando só o
custo das threads. Com a fila intercalada (round-robin por domínio), o paralelo passou a
ganhar. Medições da mesma amostra:

| | 24 artigos | 11 mil |
|---|---|---|
| sequencial, fila agrupada | 32,2 s | 4,1 h |
| paralelo, fila agrupada | 38,2 s | 4,9 h |
| sequencial, fila intercalada | 23,5 s | 3,0 h |
| paralelo, fila intercalada | 19,5 s | 2,5 h |

O gargalo então **mudou de lugar**: passou a ser o resolve, que serializa no Google.
Com `THROTTLE_LINK_RESOLVE=0.05` o tempo cai para 1,1 h (24/24 de sucesso na amostra),
mas o padrão fica em 0,4 s — um bloqueio no meio de uma execução de horas custa mais que
as 2 h economizadas. Fica configurável, com a medição documentada no `.env.example`.

Dois bugs corrigidos no caminho: o `resolve()` chamava o cliente HTTP cru, sem throttle
nenhum; e `self.default_delay = ...` era atribuído DEPOIS do `super().__init__()`, que já
havia criado o `DomainThrottle` — o `throttle_article_fetch` do `.env` nunca era aplicado.

> **Análise diária por Gemini com grounding, 1 chamada/dia.** Problema: leitura qualitativa diária
> sem reabrir o scraping. Opções: (a) Gemini 3 Flash com `google_search` + JSON schema na mesma
> chamada; (b) 2 chamadas nos 2.5; (c) LLM sobre `raw_articles`. Escolhido (a): spike de
> 2026-10-15 confirmou que a Interactions API aceita tools + `response_format` juntos no
> `gemini-3.8-flash` (prompt v3: ~3,2 mil tokens de entrada, ~8,5 mil de saída, ~13,7 mil de
> raciocínio, 14 buscas, 85-133 s, ~US$ 0,086 por chamada). Dois achados do teste real: o
> schema enviado precisa marcar TODOS os campos como obrigatórios (senão o modelo omite seções
> inteiras), e as URLs vêm como redirect do Google que expira e às vezes aponta para imagem. Custo: única dependência paga — exceção explícita à
> regra de custo zero, **teto US$ 5/mês**. Perde-se reprodutibilidade (a busca muda; por isso JSON
> bruto, prompt renderizado, queries e citações ficam em `raw_llm_runs`), e a chamada não passa
> por `collectors/base.Collector` (não é scraping).
>
> **raw + staging para o LLM.** `raw_llm_runs` append-only (uma linha por tentativa, mesmo em erro);
> `staging_llm_*` substituída por dia (DELETE + upsert). Reprocessar com outro parser é local.
>
> *Superada: o executor roda como worker sempre ligado (ver as decisões do executor abaixo).*
>
> **Cron em vez de loop.** Railway cobra por tempo de processo; loop com `sleep` custaria o mês
> inteiro por 1 min de trabalho. `get_engine().dispose()` no `finally` é o que deixa o processo sair.
>
> **Citações no JSON, não nas anotações.** A Interactions API devolve `url_citation` por offset
> sobre o texto; mapear sobre JSON é frágil. O prompt exige `fonte`/`url` em cada item; as
> anotações vão para `citacoes_json` só como auditoria.

## DECISÕES DO EXECUTOR E DA ENTREGA

**Executor puxando comandos (pull) em vez de a API chamar o scraper (push).** Problema: o painel
precisa disparar coleta sem que o scraper exponha uma porta de entrada no Railway. Opções: (a) a API
chama o scraper por HTTP, o que exige URL pública, healthcheck e deixa o painel dependente da
disponibilidade do scraper; (b) fila no próprio banco (`painel_comandos`), que o executor consulta por
HTTP a cada `EXECUTOR_POLL_S`. Escolhida (b). Custo: latência de até `EXECUTOR_POLL_S` (5 s); entrega
"pelo menos uma vez", tratada com `painel_comandos_aplicados` (um id já aplicado é só reconfirmado);
heartbeat em `painel_heartbeat` para o painel saber se o executor está vivo. Substitui o cron da
ADR "Cron em vez de loop" acima.

**Carga histórica dentro do executor, no Railway, com prioridade sobre o daily.** Problema: a carga de
anos leva horas e disputa o mesmo banco, o mesmo throttle e as mesmas fontes que o daily. Opções: (a)
serviço separado no Railway, com dois processos escrevendo sem controle de exclusão; (b) rodar a carga
à mão, fora do painel; (c) uma thread própria dentro do executor, uma execução por vez. Escolhida (c),
com pausa e cancelamento checados entre unidades (coletor × ano), não no meio de uma requisição. Um
daily que chega durante a carga nasce `agendada` com `aguardando = historico`. Custo: o executor é um
processo só; ao reiniciar, a carga retoma sozinha pelos checkpoints de `raw_collection_runs`, mas um
daily em andamento vira `falhou` e não é refeito.

**Tabelas de entrega sem prefixo.** Problema: as 8 tabelas são o contrato com a etapa seguinte, não uma
camada de dados. Opções: prefixo de camada (`entrega_*`) ou nome limpo. Escolhido o nome limpo
(`noticias`, `precos`, `cadastro`, `mudancas_ticker`, `balancos`, `acoes_emitidas`, `proventos`,
`indices`), listado em `ENTREGA_TABLES`. Custo: é exceção à regra de prefixo do CLAUDE.md, e a lista
precisa ser mantida à mão.

**Fim rolante da coleta × janela fixa do v1.** Problema: a coleta diária tem de avançar até hoje, mas o
dataset v1 do TCC precisa de uma janela fixa e reproduzível. Opções: (a) trocar `data_end` por hoje, o
que muda o v1 silenciosamente; (b) separar dois conceitos: `fim_coleta` rolante (`data_fim_coleta`
vazio = hoje) e a janela do dataset, fixa em `data_start`/`data_end`. Escolhida (b). Para distinguir a
origem das linhas de `articles`, a coluna `origem` (default `google_news`) recebe também os outros
coletores de notícia (IPE), e o v1 filtra por `google_news`. Custo: há duas definições de "fim" no
código; mudar a janela do v1 exige ADR novo, não basta trocar o `.env`.

**Juíza sem busca na web, com regra barata primeiro.** Problema: decidir se uma notícia é sobre a
empresa. Busca na web por par é cara, instável e reintroduz uma fonte que não se reproduz. Opções: (a)
Gemini com grounding por par; (b) regra local (nome, apelido, contexto) decide o que puder, a fonte
opcional Jev vem depois e só o Gemini recebe o que sobrar; (c) só regra local. Escolhido (b), sem busca.
Ordem por par pendente: regra (sem custo) -> Jev (desligado) -> Gemini. Custo: o que sobra vai ao Gemini,
então o consumo precisa ser medido contra o teto de US$ 5/mês (ver `JUIZA_MAX_ITENS_DIA`). Toda chamada
fica em `raw_llm_juiza_runs`, inclusive as que falharam.

**Fonte JEV implementada, mas desligada.** *Substituído em 2026-10-23 pelo ADR "Jev na juíza medido por concordância, sem conjunto-ouro".* Problema: a fonte JEV tem chave própria (`JEV_API_KEY`) e custo
que não foi decidido. Opções: remover o adaptador ou mantê-lo desligado. Escolhido manter o adaptador com
`JEV_ENABLED=false` por padrão. Motivo: a religação aguarda aprovação do dono, porque seria a segunda exceção paga à regra de custo zero (o teto de US$ 5/mês é compartilhado com o Gemini), e a fonte precisa de um teste em português no conjunto-ouro. Nunca substitui o FinBERT, que a spec e a pergunta do TCC nomeiam: se religada, entra só como colunas extras.
Custo: código sem uso enquanto estiver desligado.

**Nomes de coletor são os do painel.** Problema: o painel mostra cada etapa e coletor pelo nome. Opções:
renomear no painel ou mapear no scraper. Escolhido usar os ids de `EtapaDef` (`precos`, `macro`, `noticias`,
`ipe`, `enriquecimento`, `juiza`, `cadastro`, `entrega`, `analise`) e os coletores `yahoo`, `bcb`,
`google_news`, `cvm_ipe`, `finbert`, `cvm_b3`, `build_entrega`, `gemini` como contrato. Custo: renomear
exige combinar com o painel antes.

**Plano de contas de bancos decidido por `ds_conta`.** Problema: bancos usam outro plano da CVM (lucro em
3.09, pois não existe 3.11; patrimônio líquido em 2.08, pois 2.03 são passivos). Opções: código fixo por
tipo de empresa ou também a descrição. Escolhido aceitar a conta pelo código e pela descrição
(`ds_conta`, em `entrega/builder.py`, `_aceita_conta`), com o mapa de bancos em `config.py`. Custo: a
descrição vem da CVM em texto livre e pode mudar de redação entre demonstrações.

**Bancos e seguradoras por lista explícita, não pela descrição (2026-10-20).** Complementa a decisão
acima: a descrição continua escolhendo a conta, mas não decide quem é banco. Problema: EBIT, D&A,
empréstimos, caixa, aplicações e lucro bruto não se aplicam a bancos e seguradoras, e a descrição não
separa essas empresas: BBSE3 e IRBR3 têm "Caixa e Equivalentes de Caixa" como a indústria, e PSSA3 usa o
plano industrial inteiro. Opções: (a) inferir pela descrição; (b) lista explícita de códigos CVM em
`CD_CVM_BANCOS_SEGURADORAS`, em `config.py`. Escolhida (b), conferida no ITR 2025 da CVM. Custo: a lista
precisa de manutenção quando entrar um banco ou seguradora novo. ITSA4 (holding) e B3SA3 (bolsa) ficam de
fora porque usam o plano industrial.

**D&A trimestral do ITR pela diferença de acumulados.** Problema: a DFC do ITR é acumulada no ano, então o
Q2 e o Q3 não têm D&A publicada. Opções: (a) só o período igual ao de uma DFC publicada (acumulado,
primeiro trimestre e DFP), deixando o Q2 e o Q3 em branco; (b) derivar o trimestre pela diferença entre o
acumulado até o fim do período e o acumulado até a véspera do início, usando o documento anterior do mesmo
CNPJ. Escolhida (b), por decisão do dono (o plano inicial previa só (a)). Custo: a linha mistura dois
documentos, cada um com a própria versão. Se falta o documento anterior, ou a diferença sai negativa
(reapresentação), a coluna fica em branco.

**Provento de UNIT composto de ON + PN.** Problema: a B3 não publica proventos de UNIT como um papel
próprio; a UNIT é composta de ações ON e PN. Escolhido compor o provento da UNIT a partir de ON + PN. Custo:
depende da composição vigente da UNIT; mudança na composição não é detectada sozinha.

**`registro_hash` inclui `issuing_company`.** Problema: o mesmo registro de provento, em empresas
diferentes, colidia na chave de upsert. Escolhido incluir a empresa emissora no conteúdo hasheado.
Custo: mudar o conteúdo hasheado muda todos os hashes, e isso exige recoleta completa de proventos.

**Notícias reprovadas pela juíza saem de `noticias`, só para artigos julgados.** Problema: a entrega não
pode mostrar notícia que a juíza reprovou. Opções: filtrar só na montagem ou apagar da tabela de entrega.
Escolhido apagar de `noticias` (entrega) o que foi reprovado, e só artigos que já têm julgamento
(`entrega/builder.py`, `_apagar_reprovadas`). A camada `raw` não muda. Custo: a entrega muda quando a
juíza reprova, e um novo julgamento pode devolver o artigo.

**Prefixo `v1-` só nas tabelas criadas pela pipeline nova.** Problema: a pipeline nova compartilha o banco
com o dataset v1 congelado (`features_dataset_v1`), e o dono pediu que as tabelas da pipeline nova tenham
`v1-` na frente do nome. A primeira versão (caf2deb) prefixou as 38 tabelas que a pipeline usa, inclusive
as que já existiam (`raw_articles`, `raw_prices`, `raw_llm_runs`, `staging_llm_*`). Opções: (a) prefixar
as 38; (b) prefixar só as 25 criadas a partir da 0004, e as antigas mantêm o nome; (c) schema separado,
rejeitado porque o MySQL não tem schemas (ver a ADR de camadas); (d) renomear só no código, sem tocar o
banco, rejeitado porque o nome real precisa bater com o que a API lê. Escolhido (b): critério "só tabelas
criadas pela pipeline nova; as antigas, inclusive a análise da LLM v3, mantêm o nome". `PREFIXO_TABELA = "v1-"`
em `db/models.py`; migration 0008 com 25 `rename_table` (MySQL). As 25 são 8 `painel_*`, 9 de fontes e juíza
(0005, 0007) e as 8 de entrega (0006). As 14 antigas (`raw_articles`, `raw_article_bodies`, `raw_prices`,
`raw_macro`, `raw_collection_runs`, `staging_article_tickers`, `staging_article_sentiment`, `raw_llm_prompts`,
`raw_llm_runs`, `staging_llm_*` x4, mais `features_dataset_v1`) mantêm o nome. `tabela_do_projeto` aceita o
prefixo de camada com ou sem `v1-`, e as 8 de entrega só com `v1-`. Custo: o hífen obriga crase em SQL escrito
à mão (`v1-raw_ipe_docs`); quem lê uma tabela precisa saber se ela é antiga ou nova (o critério é a migration
de origem); e as constraints, índices e sequências existentes mantêm o nome antigo. `uq_raw_articles_url_hash`
tem nome explícito no modelo, igual ao da 0001. A naming_convention gera `v1-` só nas constraints das tabelas
novas. O CSV de entrega continua sem prefixo.

**Alembic descontinuado (2026-10-20).** Problema: com o banco unificado no MySQL do `.env`, já com todo o schema
aplicado (estado equivalente à antiga migration 0008), o Alembic só custava: uma dependência, o `env.py`, a tabela de
controle `alembic_version`, o SQL gerado duplicado em `sql/` e um executor sem DDL, que nunca roda migration. Opções:
(a) manter o Alembic; (b) `create_all` a partir dos models, que cria banco vazio mas não altera banco existente;
(c) SQL versionado à mão: `sql/schema.mysql.sql` com o schema completo do estado atual, para banco vazio, e cada
alteração em `sql/alteracoes/AAAA-MM-DD_descricao.mysql.sql`, aplicada à mão com usuário com DDL e aprovação do dono.
Escolhido (c). Removidos: `alembic.ini`, a pasta `alembic/`, a dependência no `pyproject.toml` e os `sql/*.mysql.sql`
gerados. Custo que se perde: (1) downgrade automático: a reversão vira outra alteração escrita à mão, por exemplo um
`RENAME TABLE` de volta; (2) autogenerate e `alembic check`, que detectavam divergência entre model e banco: hoje a
conferência é revisão manual; (3) o histórico registrado na própria tabela de controle: passa a ser disciplina manual,
em `sql/alteracoes/` e no [histórico de mudanças](09-historico-de-mudancas.md); (4) `db-info` só confere a existência
das tabelas, não as colunas. A tabela `alembic_version` continua no MySQL de produção, sem uso, até o dono aprovar a
remoção.

**Comando `coletar`: coleta avulsa como variante do daily, com catálogo único de coletores (2026-10-20).** Problema: o painel precisa pedir a recoleta de coletores específicos (ex.: `cvm_itr_dfp` de 2024–2025 sem checkpoint, seguida da remontagem de `balancos`) sem disparar a carga histórica inteira. Opções: (a) filtro de coletores na carga histórica, que tem estado próprio (`v1-painel_historico`, posição por ano e coletor) e é única por vez; (b) uma terceira máquina de execução; (c) reaproveitar o `Daily`: a execução (`v1-painel_execucoes`, tipo `coletar`) recebe um plano de etapas montado pelo comando, uma por item e uma para a montagem. Escolhido (c), com um catálogo único em `orquestrador/coletores.py` (id → alvo, checkpoint padrão, parâmetros aceitos, janela de anos), usado também pela carga histórica. Mesma exclusão do daily: recusa com execução agendada ou em andamento, e nasce `agendada` com `aguardando = historico` durante a carga. Sem orçamento. Falha de item resulta em `parcial`. Sem cancelamento pelo painel. Custo: o plano vive só na memória do processo (não há coluna de parâmetros), então um coletar agendado ou em andamento num reinício vira `falhou` e precisa ser reenviado. O heartbeat ganha o valor `coletar`, e as etapas ganham ids de coletor que o painel precisa exibir.

**Jev na juíza medido por concordância, sem conjunto-ouro (2026-10-23).** *Atualizado em 2026-10-26 pelo ADR "Juíza: Jev decide por entrega segura; fallback OpenAI gpt-5.6-luna (effort none)": `JUIZA_JEV_DECIDE` ficou obsoleta em favor de `JUIZA_JEV_MODO`, e a concordância não chama mais a LLM (compara com a referência já gravada).* Problema: o Jev é a segunda exceção paga do projeto, e a juíza precisa saber se ele pode decidir sem o Gemini. O plano anterior dependia de um conjunto-ouro de cerca de 300 pares rotulados à mão pelo dono, para medir precisão e revocação em português. Esse conjunto nunca foi feito. Opções: (a) manter o conjunto-ouro e esperar a rotulagem; (b) medir o Jev contra o Gemini, com concordância e kappa numa amostra reprodutível; (c) não usar o Jev. Escolhido (b), com `judge-news --concordancia N --semente S`. O comando julga N pares no Jev e no Gemini, sem gravar julgamento e sem teto. Regra de decisão: se o mínimo entre a concordância de `papel` e a de `tipo_conteudo` for pelo menos 85%, o dono pode ligar `JUIZA_JEV_DECIDE=true` (o Jev decide, e o Gemini só entra abaixo de `JUIZA_CONFIANCA_MINIMA`). Abaixo disso, o Jev fica em triagem: só aceita descarte com confiança de pelo menos 0,9. O resto vai ao Gemini. Custo: concordância não é acurácia. Dois modelos que erram do mesmo jeito concordam. Sem rótulo humano, não há precisão nem revocação, e o número para o TCC é a concordância, com essa ressalva. O conjunto-ouro (`tests/fixtures/juiza_gold.csv` e `judge-news --avaliar`) foi removido. Este ADR substitui "Fonte JEV implementada, mas desligada".

**Teto em US$ e verba do backlog; Batch API rejeitada (2026-10-23).** *Atualizado em 2026-10-26 pelo ADR "Juíza: Jev decide por entrega segura; fallback OpenAI gpt-5.6-luna (effort none)": os tetos (US$ 3 mensal da juíza e US$ 15 do backlog) continuam, e passam a cobrir Jev e OpenAI. A Batch API da OpenAI também foi rejeitada.* Problema: `JUIZA_MAX_ITENS_DIA` limita itens, não dinheiro. Os lotes têm tamanhos diferentes, e o total mensal não era conferido. A juíza precisa julgar o passado (cerca de 2 anos de pares) sem estourar o teto de US$ 5/mês que divide com a análise diária. Opções: (a) só teto de itens; (b) teto em US$ por mês para a juíza diária (US$ 3) e verba única para o backlog (US$ 15, cumulativa); (c) Gemini Batch API para o backlog, com desconto. Escolhido (b). Cada chamada grava `custo_usd` e `finalidade` (`producao`, `backlog` ou `concordancia`), e os tetos são somas dessas colunas. Assim, o teto não depende de memória do processo. A checagem é feita antes de cada lote, e o estouro máximo é de um lote. A Batch API foi rejeitada por ora: o desconto é pequeno em valor absoluto para este volume, e o job assíncrono (envio, espera e coleta) aumenta a complexidade do executor. Custo: o teto só vale para o que a juíza grava, e a análise diária não passa por essa checagem. O estouro é de no máximo um lote.

**Abertura do IBOV vazia quando repete o fechamento anterior; sem fonte alternativa (2026-10-23).** Problema: em cerca de 32% dos dias, o Yahoo (`^BVSP`) devolve a abertura igual ao fechamento do pregão anterior. Esse valor não é uma abertura real. Opções: (a) trocar a fonte do IBOV; (b) anular a abertura repetida só na entrega (`v1-precos`, ticker `IBOV`), mantendo `raw_prices` como veio; (c) não tratar. Testado em 2026-10-23 sem fonte alternativa: o Stooq bloqueia com desafio JavaScript; a brapi exige token (HTTP 401 sem token); a B3 (GetPortfolioDay e indexStatistics) só traz fechamento. Escolhido (b). Custo: só o IBOV é tratado, porque as outras séries não foram medidas com o mesmo defeito. A regra é uma heurística: uma abertura igual ao fechamento anterior pode ser real, e isso é raro. A regra pode ser revista sem recoleta, porque `raw_prices` não muda.

**Juíza: Jev decide por entrega segura; fallback OpenAI gpt-5.6-luna (effort none) (2026-10-26).** Problema: em 2026-10-24, a carga histórica gastou US$ 2,24 (cerca de R$ 13,24) em 13.144 pares julgados só pelo Gemini `gemini-3.1-flash-lite`. Cada par teve cerca de 139 tokens de entrada e 91 de saída, e cerca de 80% do custo foi saída. O crédito pré-pago do Gemini acabou (402) e a carga parou. O Jev nunca rodou: `JEV_ENABLED=true` sem `JEV_API_KEY` era ignorado em silêncio, e no modo triagem ele só descartava. Opções: (a) manter o Gemini `gemini-3.1-flash-lite`; (b) trocar a LLM de fallback pela OpenAI `gpt-5.6-luna`, com preço oficial de US$ 0,20 de entrada, US$ 0,02 de entrada em cache e US$ 1,20 de saída por milhão de tokens; (c) `gpt-5-nano`, mais barato, mas desligado em 11/12/2026; (d) Batch API, com 50% de desconto, mas execução assíncrona de até 24 h, que não cabe no daily; (e) modelo local: o DeepSeek V4.1 Flash precisa de 128 a 145 GB de memória, e modelos pequenos via Ollama ficaram para depois. Escolhido: (1) fluxo regra barata, depois Jev, e a LLM só para o que sobra; (2) `JUIZA_JEV_MODO=entrega` como padrão, em que o Jev decide "entra" ou "não entra" quando a probabilidade da regra de entrega (`tipo_conteudo == reportagem` e `papel` em {protagonista, citada_com_fato}) passa de `JUIZA_JEV_LIMIAR_ENTREGA`, padrão 0,9 e provisório até a concordância medir o limiar; (3) fallback `openai` com `gpt-5.6-luna`, Responses API, `reasoning.effort = none`, Structured Outputs estrito e `store = false`, com o prompt `juiza_openai` na mesma versão 1 (os julgamentos do Gemini continuam válidos); (4) paradas explícitas `jev_sem_chave`, `erro_jev` e `sem_credito`, no lugar da falha silenciosa; (5) tetos inalterados (US$ 3 mensal da juíza e US$ 15 do backlog), agora valendo para Jev e OpenAI. O Gemini fica só na análise diária, com teto de US$ 5/mês. O que se perde: o Jev não devolve `tickers_adicionais`, então os pares que ele decide ficam sem os tickers extras, e a concordância mede essa perda. A referência viu 300 caracteres do corpo e o Jev vê 1.500, então parte da discordância pode ser acerto do Jev. Pares sem segurança seguem para a LLM, que continua pagando parte do backlog. O custo real da Luna ainda não foi medido. Concordância continua não sendo acurácia. O sistema passa a depender de duas chaves pagas (Jev e OpenAI) e de um limiar ainda não medido. Medido em 2026-10-26: o limiar 0,9 foi confirmado (cobertura 69%, concordância 95,9%, kappa 0,918, n = 344), o custo ficou em cerca de US$ 0,00008 por par (Jev mais Luna), e 28% dos pares decididos pelo Jev perdem tickers adicionais.

**Análise diária desligada até migrar para a OpenAI (2026-10-26).** Problema: o crédito pré-pago do Gemini acabou em 2026-10-24 (402), e a análise diária depende do `google_search` embutido no Gemini. Opções: (a) recarregar o Gemini e manter a análise como está; (b) tirar `GEMINI_API_KEY` do `.env`, o que deixa a etapa `analise` como `pulada`; (c) desligar por flag, `ANALISE_ENABLED`, e migrar a análise para a OpenAI, que exige outra forma de pesquisa na web. Escolhido: (c). `ANALISE_ENABLED=false` é o padrão do código; a etapa `analise` sai como `pulada`, com a mensagem "análise diária desativada (ANALISE_ENABLED=false)", e o `analyze-llm` manual continua valendo. O que se perde: sem análise diária até a migração, as tabelas `staging_llm_*` não são atualizadas pelo daily, e a migração é trabalho separado, que troca a busca do Google.

**Escala da composição do capital inferida pelo giro, só na entrega (2026-10-26).** Problema: a CVM publica a composição do capital (`QT_ACAO_ORDIN`, `QT_ACAO_PREF`, `QT_ACAO_TOTAL_CAP_INTEGR`) sem coluna de escala. Medido no banco real: cerca de 30% das linhas vêm em milhares; VALE3 sai sempre em milhares (4.255.763 em vez de ~4,26 bi) e PSSA3 alterna trimestre a trimestre. Nos balanços, alguns documentos declaram `ESCALA_MOEDA` errada (SAUD3 no ITR 2021-06-30, JBSS32 na DFP 2023-12-31). Opções: (a) corrigir o raw; (b) lista manual por CNPJ e data; (c) inferir na entrega. Escolhido (c), com o raw intacto. Ações: a série de cada CNPJ é quebrada nos saltos de ~1000x, e um trecho está em milhares quando a mediana do giro (volume de 60 pregões, ON + PN + unit, dividido pelo total de ações) passa de 10% ao dia, o que é impossível em unidades. Trecho sem volume (antes da listagem) herda em cadeia, alternando a cada troca de 1000x a partir do trecho decidido mais próximo. Balanços: documento com escala diferente da majoritária do CNPJ e magnitude 100x fora da mediana passa a usar a majoritária. Custo: depende de volume em `raw_prices`, então CNPJ sem nenhum preço não é corrigido (STBP3, que o Yahoo não devolve, continua em milhares: 851.018 a 864.170 ações); valor isolado que não é troca de 1000x só gera aviso (CMIN3, AZUL3 em 2026-03-31); é heurística, revisável sem recoleta. Aplicado em 2026-10-27: 503 linhas de ações e 3 de balanços mudaram.

**Preço da AZUL3 pela COTAHIST da B3 numa janela fixa: exceção à fonte primária (2026-10-26).** Problema: entre 2026-01-13 e 2026-04-17, o Yahoo (`AZUL3.SA`) emenda papéis diferentes (fechamentos de 41,25 → 7,42 → 341 → 28 milhões). Nessa janela a ON da Azul negociou como AZUL53, cotada por lote de 1.000.000 de ações (FATCOT), e o Yahoo não registra os grupamentos de 75:1 e 150.000:1. Opções: (a) deixar a AZUL3 sem preço na janela; (b) Brapi, que exige token e cobre só 1 ano; (c) COTAHIST da B3, arquivo oficial, sem chave, ~96 MB por ano, dividindo o preço pelo FATCOT e pelo fator acumulado dos grupamentos do suplemento. Escolhido (c), por lista explícita (`CORRECOES_PRECO_B3` em `config.py`) e CSV versionado (`data/correcoes_precos/AZUL3.csv`), gerado pelo comando `gerar-correcao-precos`, fora do daily. Como o coletor do Yahoo rebaixa o histórico inteiro e faz upsert, ele troca a janela pelo CSV a cada execução (`aplicar_correcoes`); as linhas ficam com `source = b3_cotahist`. Custo: duas fontes na mesma série; o fator publicado pela B3 tem 6 algarismos significativos (erro de 5e-6, constante na janela, desprezível nos retornos); outro papel com o mesmo defeito precisa entrar na lista à mão; se a B3 republicar o COTAHIST, o CSV precisa ser regenerado. Aplicado em 2026-10-27: 66 pregões da B3 e continuidade de 32,25 (2026-04-17, B3) para 30,54 (2026-04-20, Yahoo e B3 iguais).

**Coleta de notícias dos 3 veículos pela API do WordPress, com classificador por código antes da juíza (2026-10-27).** Problema: o Google News levava mais de 30 h para 5 anos de notícias (throttle de 2,0 s, sequencial, ~9.010 consultas/ano) e não trazia o corpo completo nem todas as matérias de InfoMoney, Money Times e Exame. Opções: (a) manter só o Google News; (b) RSS nativos dos veículos, que cobrem só o presente; (c) API REST do WordPress, pública e sem chave, com todos os posts do período, `date_gmt` exato e o HTML do corpo; (d) mandar todos os pares para a juíza. Escolhido (c) para os 3 veículos, com o casamento de tickers feito localmente, e um classificador por código (`llm/juiza_codigo.py`) que decide o que a evidência do casador já resolve; o resíduo vai para a juíza. Medido em 2026-03: 11.306 posts no mês em 122 requisições, sem erro. A API cobre 862 de 865 (99,7%) do que o Google trouxe desses sites (faltaram 2 de conteúdo de marca e 1 renomeado), e a normalização de URL acertou 100%. Republicação entre veículos (106 pares/mês) conta como 1 notícia, a história, com o representante no `date_gmt` mais antigo (decisão do dono). Posts por ano nos 3 sites: 89,5 mil (2021), 93,8 mil (2022), 83,6 mil (2023), 84,6 mil (2024) e 106,1 mil (2025). Classificador medido contra 28.279 julgamentos do Gemini, com o critério de n ≥ 30 e precisão ≥ 0,90: K3 (ticker no título, matéria com corpo) aprovado, com 91,5% no dev, 91,9% no teste e 92,4% em 2026-10; K5 (corpo forte) reprovado, de 24% a 52%, e vai para a juíza; tipo pelo título, de 52% a 65%, só marca o par como duvidoso; K6 aprovado (100%, n = 31), e a eliminação de E5 e de "sem ocorrência" aprovada, com 90,1% e 90,8% em 2026-10. Com só o título, como nos pares do Google, o código acerta ~78%, por isso não decide esses pares. A juíza fica com ~26% dos pares do WordPress, a US$ 0,000077 por par (Jev mais Luna), ~US$ 0,11/mês e ~US$ 5,5 no histórico. A regra de entrega fica igual: só `reportagem` com papel `protagonista` ou `citada_com_fato`. O que se perde: a cobertura passa a depender de um site WordPress público, sem contrato, que pode mudar a API (a coleta acusa divergência de total nos meses fechados, e o processamento é refeito do banco). Os 3 domínios saem do Google News quando `WP_ENABLED=true`, e a origem `google_news` deixa de cobrir esses veículos. Os 3 itens do Google que a API não cobre não são recuperados. O classificador por código é heurística: depende das listas de regras (`REGRAS_VERSAO = 1`) e precisa ser medido de novo se elas mudarem. O panorama de mercado com ticker no título continua entrando (limitação já registrada), e o custo passa a ter três tabelas novas (`v1-raw_wp_posts`, `v1-staging_historias` e `v1-staging_wp_processamento`).

**Carga histórica em duas trilhas paralelas; Google News falha alto em vez de calar (2026-10-27).** *Atualizado em 2026-10-28 pelo ADR "Google News: bloqueio por IP (503)": o disjuntor pausa antes de abortar, e a falha da trilha Google não derruba mais a principal.* Problema: a carga rodava um passo por vez, e o `google_news` (6 a 9 h por ano) segurava todos os outros: WordPress, IPE e juíza só começavam depois dele. Além disso, a coleta "parecia parada": o painel mostrava a última unidade concluída, não a em curso; o retry ignorava o `Retry-After` (2, 4 e 8 s e desistia); uma resposta 200 que não era RSS (captcha ou consentimento) gravava a semana como `ok` com 0 itens, perda silenciosa nunca refeita; e uma falha num dia da subdivisão perdia a semana inteira. Opções: (a) segundo coletor do Google em paralelo, que não acelera nada, porque o throttle de 2,0 s é por domínio e mais requisições no mesmo IP só aumentam o risco de bloqueio; (b) só reordenar os passos (WordPress antes do Google); (c) duas trilhas: Google (`google_news` e `enriquecimento`, que dividem o `news.google.com`) e principal (globais e, por ano, `wordpress`, `processar_wp`, `cvm_ipe` e `juiza_wp`, a juíza só das origens `wordpress` e `cvm_ipe`), com um portão por ano: `finbert(anos=[Y])`, `juiza` e `build_entrega` só depois que a trilha Google concluir Y. Escolhido (c), com a retomada pelo status de cada `(ano, coletor)` em `v1-painel_historico_progresso` (sai a posição única de `v1-painel_historico`) e pausa que só vira `pausada` quando as duas trilhas estacionam. No Google News: resposta sem `<rss>/<channel>` vira erro da unidade (refeita no modo `pendentes`); `Retry-After` respeitado com teto (`HTTP_ESPERA_MAX_S`, 120 s); subdivisão para no primeiro dia que falha, grava o que veio e sai com erro; disjuntor aborta a coleta após `GOOGLE_NEWS_MAX_FALHAS_SEGUIDAS` (10) unidades seguidas com falha. O painel ganha a unidade em curso e o último erro por `(ano, coletor)` (alteração `2026-10-27_historico_progresso_unidade_erro`). O que se perde: o tempo total continua sendo o do Google; a ordem global dos passos deixa de ser determinística; `google_news._persist` e `processar_wp` gravam em `raw_articles` ao mesmo tempo (um deadlock 1213 vira erro de unidade, ou derruba a carga se ocorrer fora de unidade, e um novo disparo `pendentes` retoma); artigos com `published_at` em 01/jan UTC do segmento anterior ficam para o daily; a verba do backlog é gasta primeiro com WordPress e IPE de todos os anos; o disjuntor derruba as duas trilhas de propósito; e o retry pode dormir até 3 × 120 s sem atender a pausa.

**Juíza: fallback Claude Haiku 5.5 no lugar da OpenAI gpt-5.6-luna (2026-10-28).** Problema: o dono passou a ter US$ 100/mês de crédito gratuito na API da Anthropic, e a LLM de fallback da juíza (OpenAI `gpt-5.6-luna`, ~US$ 0,00009 por par, medido) era paga em dinheiro. Opções, com a estimativa por par feita sobre o lote medido da Luna (~5.976 tokens de entrada e ~2.750 de saída por 50 pares, tokenizer ~1,3x): (a) manter a Luna; (b) Claude Opus 5.5, ~US$ 0,0025 por par; (c) Claude Sonnet 5.5, ~US$ 0,001; (d) Claude Haiku 5.5, ~US$ 0,00006, a US$ 0,10 de entrada e US$ 0,50 de saída por milhão de tokens (prompts de até 100K tokens). Escolhido (d), por decisão do dono: o objetivo continua sendo o mais econômico possível, e classificação em lote é o uso para o qual o Haiku foi feito. Provedor `anthropic` (`llm/anthropic_client.py`, SDK oficial `anthropic`), padrão de `JUIZA_PROVEDOR`, com `claude-haiku-5-5`, effort `medium` (o padrão do modelo), pensamento adaptativo, JSON schema estrito por `output_config.format` e o prompt `juiza_anthropic` na mesma versão 1. OpenAI e Gemini continuam como alternativas. Tetos inalterados (US$ 3/mês e verba de US$ 15), agora valendo para Jev e Claude. Medido em 2026-10-28 com chamada real, em 300 pares que o Gemini já julgou (6 lotes de 50, sem gravar): 300/300 itens válidos, sem recusa, 34,7 s por lote, **US$ 0,000119 por par** (o dobro da estimativa, por causa do pensamento no effort `medium`; perto da Luna, mas pago pelo crédito). Concordância com o Gemini: entrega 86,7% (kappa 0,735), papel 68,0%, tipo_conteudo 76,7%; `tickers_adicionais` em 27 pares contra 82 do Gemini. A primeira chamada deu 400 porque a Anthropic recusa `minimum`/`maximum` em `number`: o schema `anthropic` sai sem esses limites, e a faixa segue validada pelo Pydantic. O Jev continua na frente: custa US$ 0,000049 por par e concorda 95,9% com o Gemini nos 69% de pares que decide. Julgar tudo pelo Haiku sairia mais caro e com menos concordância, então `JEV_ENABLED` segue `true`. O que se perde: o Haiku 5.5 não tem fallback no servidor para recusas de segurança (o par recusado volta vazio e conta como falha do item); o crédito de US$ 100 é condição externa, e sem ele o custo volta a ser dinheiro, no nível da Luna; acima de 100K tokens de prompt o preço sobe 5x (um lote de 50 pares fica muito abaixo); o Haiku traz menos `tickers_adicionais` que o Gemini e recusa mais entregas que ele (33 contra 7 nas discordâncias). O effort `low` cortaria parte da saída, mas o dono escolheu `medium`.

**Google News: bloqueio por IP (503); ritmo de 5 s com jitter, só pt-BR e pausa progressiva; a trilha Google não derruba a principal (2026-10-28).** Problema: a carga `hist-20261028-192314` falhou com `AbortarColeta`. O log do executor mostra 181 consultas boas em 6 min, a 1 a cada 2 s, e depois só **HTTP 503** ("tráfego incomum", sem Retry-After) até o disjuntor abortar. Na mesma manhã, o IP já levava 503 desde a 1ª consulta, por pelo menos 40 min (o código antigo perdeu 54 unidades sem avisar). Desta outra máquina, a mesma URL respondia 200. É bloqueio por IP e por ritmo, não bug. Além disso, o abort derrubava a trilha principal: o WordPress parou no meio de dez/2021 por causa do Google. Opções: (a) manter os 2 s e só retomar à mão, o que faz a carga parar a cada ~6 min; (b) mesmo ritmo, com pausa ao bloquear; (c) ritmo mais lento, com variação aleatória e pausa progressiva; (d) ritmo muito lento (8 a 10 s), sem pausa; (e) outro IP ou proxy, que não está disponível. Para o en-US: manter, desligar, ou só no daily. Escolhido, por decisão do dono: (c), com throttle de 5 s mais 0 a 2 s aleatórios (`THROTTLE_GOOGLE_NEWS_JITTER_S`) e pausas de 30, 60 e 120 min (`GOOGLE_NEWS_PAUSAS_MIN`) depois de 10 falhas seguidas, interrompíveis pelo painel e visíveis nele (log `aviso` e "unidade atual"). Só aborta depois das 3 pausas sem nenhum sucesso entre elas. O **en-US foi desligado** (`GOOGLE_NEWS_IDIOMAS=pt`): era 50% das consultas e 12% das notícias (46.781 contra 334.417). A mensagem de erro passou a trazer a causa (`HTTP 503`, `ReadTimeout`). Na carga, uma falha da trilha Google para só ela: a principal vai até o fim, os passos do portão dos anos que o Google não concluiu ficam pendentes, e a carga termina `falhou` ("trilha google: ..."). Uma falha da principal continua derrubando as duas. As esperas viraram laços com timeout. O que se perde: cerca de 12% das notícias do Google (cobertura estrangeira em inglês, que pode ser religada pela variável); o tempo por unidade sobe de 2 para ~6 s, mas a carga tem metade das unidades (~7,5 h por ano de notícias, mais as pausas); as unidades que falharam numa sequência não são refeitas na mesma execução, só no modo `pendentes`; 3,5 h de pausas sem sucesso ainda abortam; e o limite do Google não é conhecido: 5 s é escolha, não medida.

---

## PHILOSOPHY

**Validar com chamada real, nunca com documentação.** Toda decisão de fonte acima veio de
um teste executado. A documentação da Marketaux não diz "3 artigos"; a medição diz.

**Vazamento temporal é bug de corretude.** Toda janela em `features/returns.py` olha só
para trás. O `shift(-1)` existe em **um único ponto** do código — `compute_target` —
isolado para que qualquer auditoria encontre um só lugar onde t+1 é acessado.

**Teste de invariante precisa ser validado por mutação.** A primeira versão do teste de
vazamento usava série de 7 pontos: as janelas de 20 pregões ficavam NaN e o teste passava
comparando NaN com NaN. Só foi descoberto ao introduzir `center=True` e `shift(-1)` de
propósito e ver o teste passar mesmo assim. Corrigido para 60 pontos com asserção-guarda.
**Um teste que nunca falhou não prova nada.**

**Um teste só vale se já falhou.** O teste de precisão foi verificado trocando `Double`
por `Float`: falhou no MySQL e passou no PostgreSQL — reproduzindo exatamente o bug
original. O de vazamento temporal passou pelo mesmo crivo.

**Otimização sem medição é chute.** A paralelização do enrich parecia obviamente
vantajosa e, na primeira versão, deixou o pipeline MAIS LENTO. Só a medição com dados
reais revelou que a fila vinha agrupada por domínio.

**Perda silenciosa é pior que erro.** O teto de 100 itens e o `hit_cap` existem porque
uma janela saturada não gera erro — só devolve menos notícias.

---

## OPERAÇÃO DA MEMÓRIA (codebase-memory-mcp)

**ARMADILHA VERIFICADA: `index_repository` APAGA o ADR.** Confirmado nesta sessão — após
reindexar, `manage_adr(mode='get')` retornou `no_adr`. Portanto:

> Sempre que rodar `index_repository`, **regrave o ADR em seguida** com
> `manage_adr(mode='update')`, e confirme com `manage_adr(mode='get')`.

Mantenha uma cópia do conteúdo do ADR versionada no repositório para poder restaurá-lo.

Protocolo completo de descoberta e atualização em `CLAUDE.md` (seções 3 e 4) e em
`.cursor/rules/codebase-memory.mdc`.
