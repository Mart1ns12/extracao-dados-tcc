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
dominante: **custo financeiro zero**. A etapa de análise/modelagem está fora deste escopo.

## STACK

Python 3.11+ · PostgreSQL 16 (schemas raw/staging/features) · SQLAlchemy 2.0 + Alembic ·
httpx · lxml · trafilatura · pandas + pyarrow · transformers/torch (extra opcional) ·
typer + rich · pytest + ruff.

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
