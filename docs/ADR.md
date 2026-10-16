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

Python 3.11+ · PostgreSQL 16 (schemas raw/staging/features) · SQLAlchemy 2.0 + Alembic ·
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
recusado. Custo: exige servidor rodando.

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


**Portabilidade PostgreSQL + MySQL 8.** O banco de produção do TCC é MySQL, e o
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
> **Cron em vez de loop.** Railway cobra por tempo de processo; loop com `sleep` custaria o mês
> inteiro por 1 min de trabalho. `get_engine().dispose()` no `finally` é o que deixa o processo sair.
>
> **Citações no JSON, não nas anotações.** A Interactions API devolve `url_citation` por offset
> sobre o texto; mapear sobre JSON é frágil. O prompt exige `fonte`/`url` em cada item; as
> anotações vão para `citacoes_json` só como auditoria.

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
