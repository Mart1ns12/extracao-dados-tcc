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

Python 3.11+ · MySQL 8 (único banco; camadas por prefixo `raw_`, `staging_`, `painel_`) · SQLAlchemy 2.0 + Alembic ·
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
banco; (3) DDL não tem rollback no MySQL, então uma migration ruim atinge produção. Por isso a
migration é revisada antes (`alembic ... --sql`) e aplicada da máquina local com usuário com DDL,
seguindo [MIGRACAO-MYSQL.md](MIGRACAO-MYSQL.md). As migrations 0001–0007 não foram editadas; a 0008 foi reescrita antes de
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

**Fonte JEV implementada, mas desligada.** Problema: a fonte JEV tem chave própria (`JEV_API_KEY`) e custo
que não foi decidido. Opções: remover o adaptador ou mantê-lo desligado. Escolhido manter o adaptador com
`JEV_ENABLED=false` por padrão. Motivo: decisão do usuário, ainda sem justificativa escrita; registrar antes de religar.
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
