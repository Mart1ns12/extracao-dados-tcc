# Entrega para a etapa seguinte do TCC: 8 tabelas com notícias de alta qualidade

Data: 2026-10-19. Estado: **rascunho para aprovação** (nada construído).

Este documento descreve o que o pipeline passa a entregar para a etapa seguinte do TCC
(modelo XGBoost + interface), como cada tabela é produzida e como a qualidade das notícias
é garantida. Um construtor que conheça o repositório e o `CLAUDE.md` deve conseguir planejar
a implementação sem voltar ao autor. Decisões marcadas **[minha escolha]** foram tomadas pelo
autor deste documento e podem ser revistas; itens marcados **[spike]** precisam de uma
verificação com chamada real antes de implementar; itens **[pendente]** aguardam resposta
do dono do projeto.

---

## 1. Intenção e porquê

A etapa seguinte do TCC recebe **8 tabelas** com colunas, chaves e formatos fixos (seção 3)
e precisa delas em dois regimes: **carga histórica** completa e **atualização diária** até
as 20h de Brasília, em todo dia de pregão. Hoje o pipeline produz um dataset de 14 colunas
(`features_dataset_v1`) que cobre parte disso (notícias com FinBERT, preços, Selic e PTAX).

O critério de qualidade declarado pelo dono do projeto: **"o que seria ruim passar adiante é
notícia de baixa qualidade"**. Panoramas de mercado atribuídos a uma empresa, listas de
"5 ações para comprar", homônimos ("Vale" o verbo, "Vamos" a locadora) e matérias da matriz
estrangeira entram hoje como notícia da empresa. A LLM (Gemini) passa a atuar como **juíza**
dessas matérias, nunca como fonte de dado estruturado, e a CVM entra como **fonte oficial**
de fatos relevantes (IPE).

## 2. Objetivos, não-objetivos e anti-objetivos

**Objetivos**

1. Produzir as 8 tabelas exatamente como a spec (nomes de colunas, chaves, formatos), no banco
   e em CSV, com carga histórica e atualização diária idempotente (upsert).
2. Toda fonte nova é **oficial ou já aprovada, gratuita e sem chave**: CVM dados abertos, B3,
   BCB, Yahoo. Nenhuma API paga além do Gemini (teto já aprovado de US$ 5/mês, que inclui a
   juíza).
3. Elevar a qualidade da `noticias`: só reportagem sobre a empresa, mais fatos relevantes
   oficiais, cada linha com fonte, link, hora com fuso e probabilidades do FinBERT.
4. Manter o que existe funcionando: `features_dataset_v1`, o `analyze-llm` v3 e os testes.

**Não-objetivos (desta entrega)**

- Calcular indicadores (P/L, beta, dividend yield, etc.): é trabalho da etapa seguinte.
- Redesenhar a análise diária do Gemini (casos 11–20 do brainstorm: estado do dia, humor,
  temperatura, explicador de anomalias). Fica como **Fase C**, com documento próprio (seção 11).
- Fluxo de investidor estrangeiro e aluguel de ações (séries da B3): fora da spec; anotado
  como extensão futura.

**Anti-objetivos (o que tornaria a entrega um fracasso mesmo funcionando)**

- A LLM gerar número (preço, lucro, provento, data) que vá para uma tabela entregue.
- Vazamento temporal: notícia datada antes de estar de fato disponível, ou hora inventada.
- Zero no lugar de "não tem" (ex.: `volume = 0` para o dólar, `lucro_bruto = 0` em banco).
- Reintroduzir fonte rejeitada no `CLAUDE.md` (Marketaux, GDELT, FRED) ou coleta sem throttle.

## 3. Contrato de entrega (a spec, resumida)

A spec completa, transcrita como foi recebida, está em
**`docs/spec-entrega-etapa-seguinte.md`** (parte 1: descrição e regras; parte 2: dicionário
de colunas com exemplos). Esta seção é só o resumo.

Cada arquivo vira uma tabela com o mesmo nome (sem `.csv`) e os mesmos nomes de colunas.
Chaves em negrito. Formatos: datas `AAAA-MM-DD`; hora com fuso `2026-06-18T19:42:00-03:00`;
ponto decimal; UTF-8; vazio em branco; ticker sem sufixo `.SA`; CNPJ só dígitos; balanços em
reais sem escala; volume em quantidade de ações. Vocabulários fechados: `tipo_documento` ∈
{ITR, DFP}; `tipo_acao` ∈ {ON, PN, UNIT}; `tipo` de proventos ∈ {DIVIDENDO, JCP}; `indice` ∈
{CDI, IPCA, IDIV, SMLL, IFIX}.

O que a spec **não** fixa e fica decidido aqui **[minha escolha]**: delimitador `,`
(vírgula), linha de cabeçalho, UTF-8 sem BOM, aspas só quando necessário (`QUOTE_MINIMAL`),
quebra de linha `\n`, nome de arquivo igual ao da tabela (`noticias.csv`), **dump completo**
da tabela (não incremento), gerado sob demanda (seção 9.1).

| Tabela | Colunas (chave em negrito) |
|---|---|
| `noticias` | **id**, **ticker**, data_hora_publicacao, data_hora_coleta, titulo, fonte, link, prob_positivo, prob_neutro, prob_negativo |
| `precos` | **data**, **ticker**, abertura, maxima, minima, fechamento, fechamento_ajustado, volume |
| `cadastro` | **ticker**, cnpj, empresa, setor, subsetor, tipo_acao, unit_qtd_on, unit_qtd_pn |
| `mudancas_ticker` | **ticker_antigo**, ticker_novo, data_mudanca |
| `balancos` | **cnpj**, **tipo_documento**, **data_inicio_periodo**, **data_fim_periodo**, data_entrega, receita_liquida, lucro_bruto, lucro_liquido, patrimonio_liquido |
| `acoes_emitidas` | **cnpj**, **data_referencia**, qtd_on, qtd_pn |
| `proventos` | **ticker**, **tipo**, **data_com**, valor_por_acao, data_pagamento |
| `indices` | **data**, **indice**, valor |

Períodos pedidos: notícias = período do dataset + 1 mês antes; preços e índices = período
do dataset + 1 ano antes; balanços, ações emitidas e proventos ≥ 5 anos.

### 3.1 Escopo temporal (decidido pelo dono em 2026-10-19: **5 anos, do zero**)

| Item | Valor |
|---|---|
| `DATA_START` | **2021-09-01** (5 anos antes do fim da janela atual, 2026-09-01) |
| Fim | rolante (`data_fim_coleta` = hoje), seção 9.3 |
| Notícias (Google News, IPE) | 2021-08-01 (`DATA_START − 31 dias`) até hoje |
| Preços e índices | 2020-07-28 (`DATA_START − 400 dias`) até hoje |
| Balanços, ações emitidas, proventos | zips da CVM de 2020 em diante; B3 tudo que devolver |
| Carga | **do zero**, com o pipeline novo; nada do banco atual é reaproveitado (Postgres local e MySQL remoto são recarregados) |

O que o salto de 1 para 5 anos muda, e que o plano precisa orçar:

- **Google News**: a coleta de 1 ano levou 6–9 h; 5 anos são 30–45 h, retomáveis, em
  segmentos anuais com checkpoint próprio. **[spike]** medir em 3 tickers quantos itens as
  janelas semanais de 2021 e 2022 devolvem contra 2025: o `CLAUDE.md` só validou 2 anos de
  histórico e a cobertura antiga pode ser mais rala (isso vira limitação declarada no TCC).
- **Enrich**: cresce com o número de artigos da allowlist (1 ano ≈ 11 mil ≈ 2,9 h) → ordem de
  15 h. O resolvedor de links foi medido em 3 janelas temporais; **[spike]** repetir numa
  amostra de 2021.
- **Juíza**: o backlog é 5× maior; o spike de custo (seção 6.2) decide se cabe no teto de
  US$ 5/mês ou se o histórico é julgado em meses sucessivos.
- **Banco**: `raw_articles` e `staging_*` crescem 5×; nenhuma mudança de desenho, mas o
  servidor do MySQL precisa do espaço.
- **Onde roda a carga**: numa máquina que fique ligada por dias (local ou o servidor do
  MySQL), nunca no cron do Railway.

Regras de atualização diária (da spec): notícias, índices, mudanças de ticker e ações
emitidas só acrescentam; preços conferem o `fechamento_ajustado` do pregão anterior e, se
mudou, substituem o histórico do ativo; proventos, balanços e cadastro acrescentam e
corrigem. Tudo por upsert na chave.

Universo: **todas as ações que fizeram parte do Ibovespa em algum momento do período**
(sem viés de sobrevivência), mais IBOV, USDBRL, BRENT e IVVB11 em `precos`.

## 4. Visão geral

Nada do que existe é jogado fora. Entra uma **camada de entrega** por cima das camadas
`raw` → `staging` → `features` já existentes:

```
Yahoo ──► raw_prices ─────────────────────────────────────────────┐
BCB SGS 12/433 ──► raw_macro ──────────────────────────────────────┤
Yahoo IDIV.SA/SMLL.SA/IFIX.SA ──► raw_prices (como índice) ────────┤
CVM ITR/DFP (DRE_con, BPP_con, composicao_capital) ─► raw_cvm_* ───┤
B3 (GetDetail, CashDividends, SupplementCompany) ──► raw_b3_* ─────┼─► build-entrega ─► 8 tabelas
CVM cad_cia_aberta + config ──► raw_cvm_cadastro ──────────────────┤      (upsert + CSV)
config UNIVERSO_HISTORICO / MUDANCAS_TICKER ───────────────────────┤
                                                                   │
Google News RSS + RSS nativos ──► raw_articles ─┐                  │
CVM IPE (fatos relevantes) ──► raw_ipe_docs ────┴► enrich ► score ► juíza ► filtro ─┘
                                                  (corpo)  (FinBERT) (LLM)   (seção 6)
```

Princípios que atravessam tudo:

- **Número vem das nossas tabelas; a LLM só lê texto.** A juíza classifica; não busca na web.
- **Regra barata primeiro, LLM só na zona cinzenta** (domínio bloqueado e padrões óbvios
  são resolvidos por regex antes de gastar token).
- **`raw` é imutável e append-only**; as tabelas de entrega são deriváveis a partir dela.
- Toda escrita por `bulk_upsert`; toda requisição por `collectors/base.Collector`; `Double`
  e `UTCDateTime`; schema só por Alembic.

## 5. As 8 tabelas: fonte, mapeamento e regra diária

### 5.1 `noticias`

**Fontes.** (a) `raw_articles` que já existe, alimentada pelo Google News RSS (histórico
**e** diário: as janelas recentes devolvem InfoMoney, Money Times e os demais veículos);
(b) **novo**: documentos do IPE da CVM (seção 7); (c) Fase B: **RSS nativos dos veículos**
(`config.NATIVE_FEEDS` existe, mas **nenhum coletor o usa hoje**; o `CLAUDE.md` o lista como
fonte aprovada, não como implementada). Um feed nativo não vem de busca por ticker, então o
vínculo artigo↔ticker precisa de uma regra própria: casamento de nome/apelido
(`config.APELIDOS`) no título ou no primeiro parágrafo, confirmado pela juíza, que também
devolve `tickers_adicionais`. Vantagens: `content:encoded` já traz o corpo (sem fetch) e a
hora de publicação é a do veículo. É item da Fase B, não bloqueia a Fase A **[minha
escolha]**; (d) Fase B, opcional: notícias descobertas pelo `analyze-llm` v3
(`staging_llm_noticias_empresas.url`), que entram em `raw_articles` pelo mesmo funil e pela
mesma deduplicação por `url_hash`.

Toda linha de `raw_articles` passa a declarar a **origem** (nova coluna `origem`:
`google_news` | `rss_nativo` | `cvm_ipe` | `llm`), que é o que permite ao dataset v1
continuar enxergando só o que enxergava (seção 9.3).

**Mapeamento.**

| Coluna | Origem | Regra |
|---|---|---|
| `id` | `raw_articles.id` | `"n" + id` com zero à esquerda até 6 dígitos (`n000123`); cresce além de 6 sem colisão **[minha escolha]** |
| `ticker` | `staging_article_tickers.ticker` ∪ `tickers_adicionais` da juíza (a união é feita no `build-entrega`; a juíza **não** grava em `staging_article_tickers`, seção 9.3) | uma linha por (artigo, ticker) |
| `data_hora_publicacao` | `raw_articles.published_at` | UTC → `America/Sao_Paulo`, ISO com offset. **Em branco** quando só a data é conhecida (`published_precision = 'date'`, IPE, seção 7); a data vai então em `data_publicacao` (coluna extra, abaixo). Decisão do dono: hora pode faltar, data não |
| `data_hora_coleta` | `raw_articles.collected_at` | idem conversão |
| `titulo` | `raw_articles.title` | sem sufixo " - Veículo" (o parser já remove) |
| `fonte` | `raw_articles.source_name`, senão `source_domain` | IPE: `"CVM"` |
| `link` | `raw_articles.resolved_url`, senão `url` (link do Google News, que abre no navegador) | IPE: `Link_Download` do PDF |
| `prob_*` | `staging_article_sentiment` | linha cujo `model` corresponde ao `lang` do artigo (pt → `lucas-leme/FinBERT-PT-BR`, en → `ProsusAI/finbert`). Hoje a junção não filtra `model`; passa a filtrar **[minha escolha]** |

**Colunas extras (decisão do dono: "pode ter mais colunas, desde que a notícia saia com a
maior qualidade e o mais pronta possível").** Depois das 10 colunas da spec, que mantêm nome
e ordem exatos, a `noticias` ganha, nesta ordem:

| Coluna | Valores | Origem |
|---|---|---|
| `data_publicacao` | `AAAA-MM-DD`, **sempre preenchida** | data de `published_at` em São Paulo; IPE: `Data_Entrega` |
| `momento_pregao` | `antes_abertura` (< 10:00) · `durante` (10:00–18:00) · `apos_fechamento` (≥ 18:00) · `sem_pregao` (fim de semana, feriado) · `desconhecido` (só data) | hora em São Paulo contra o calendário do `^BVSP`; responde ao pedido "saber se saiu antes ou depois de um pregão" |
| `origem` | `google_news` · `cvm_ipe` · `rss_nativo` | `raw_articles.origem` |
| `papel` | protagonista · citada_com_fato | juíza (seção 6.2); IPE = protagonista |
| `tipo_conteudo` | reportagem (só este passa o filtro) | juíza |
| `evento` | lista da seção 6.2 | juíza |
| `qualidade` | 0–1 | juíza |

**Filtro de qualidade (Fase B, seção 6).** Entram na entrega apenas linhas que a juíza (ou
a regra barata) marcou como `tipo_conteudo = reportagem` **e** `papel ∈ {protagonista,
citada_com_fato}`, mais todos os documentos do IPE. Panorama de mercado, opinião, lista,
agenda, podcast, newsletter, patrocinado, atribuição errada e matriz estrangeira ficam só nas
tabelas internas (decisão do dono: entregar só o que tem qualidade). Na Fase A (antes da
juíza) a `noticias` sai sem filtro e com as colunas da juíza em branco; a tabela só é
declarada "pronta" para o consumidor na Fase B.

**Período.** `DATA_START − 31 dias` até a data de coleta (a spec pede 1 mês antes para a
mudança de tom). O mês extra é coletado como **segmento próprio** (`collect-news --desde
--ate`), com chaves de checkpoint ancoradas no início do segmento: 31 não é múltiplo de 7, e
deslocar o início da série semanal invalidaria as chaves já gravadas e refaria as 6–9 h
**[minha escolha]**.

**Regra diária.** Só acrescenta. O job diário coleta as janelas dos últimos 2 dias
(sobreposição de propósito; a deduplicação por `url_hash` absorve), enriquece, pontua, julga
e exporta. Nunca reescreve `prob_*` de linha já entregue, exceto se o `prompt_versao` da
juíza ou o modelo do FinBERT mudar, e aí a reexecução é explícita (`--reprocessar`).

### 5.2 `precos`

**Fonte.** Yahoo chart API, como hoje (`collectors/prices_yahoo.py`). Símbolos: universo
histórico (seção 8) com `.SA`, mais `^BVSP → IBOV`, `USDBRL=X → USDBRL`, `BZ=F → BRENT` e
**novo** `IVVB11.SA → IVVB11` (verificado: existe no Yahoo como EQUITY, com volume).

**Mapeamento.** `open/high/low/close/adj_close/volume` → `abertura/maxima/minima/fechamento/
fechamento_ajustado/volume`. O `close` do Yahoo já é ajustado por desdobramento e
bonificação; o `adjclose` também por dividendo. Isso é exatamente o par que a spec pede.

- Para `IBOV`, `USDBRL` e `BRENT`: `fechamento_ajustado = fechamento` (forçado) e `volume`
  em branco **[minha escolha]**: o Yahoo devolve um volume para `^BVSP`, mas não é
  "quantidade de ações negociadas", e para o dólar devolve 0. `IVVB11` é ETF real: entrega
  volume e ajustado como vêm.
- Linhas sem `close` são descartadas (já é assim).

**Período.** `DATA_START − PRICE_WARMUP_DAYS` até hoje, com `PRICE_WARMUP_DAYS` subindo de 45
para **400** **[minha escolha]**: a spec pede 1 ano antes (beta de 252 pregões e máxima/mínima
de 52 semanas); 400 dias corridos cobrem 252 pregões com folga de feriados.

**Regra diária.** A spec manda conferir o ajustado do pregão anterior e, se mudou, substituir
o histórico do ativo. **[minha escolha]** Implementar de forma mais simples e equivalente:
rebaixar o histórico completo de todos os símbolos todo dia (≈ 95 requisições, 1 por símbolo,
já com throttle) e fazer upsert por `(data, ticker)`. Linha que não mudou é regravada igual;
linha cujo ajustado mudou é corrigida; dia novo é inserido. Satisfaz a intenção da regra sem
a lógica de comparação e sem risco de histórico pela metade.

**Troca de código.** O Yahoo serve o histórico inteiro sob o código novo (ex.: `AXIA3.SA`
inclui os anos de `ELET3`) **[spike]**: confirmar para cada entrada de `mudancas_ticker`. A
entrega usa sempre o código **atual**; `mudancas_ticker` permite à etapa seguinte costurar.

### 5.3 `cadastro`

**Fontes.** (a) `cad_cia_aberta.csv` da CVM (verificado: colunas `CNPJ_CIA`, `DENOM_SOCIAL`,
`CD_CVM`, `SIT`, `SETOR_ATIV`...); (b) B3 `listedCompaniesProxy/CompanyCall/GetDetail/{b64}`
por `codeCVM` (verificado: devolve `cnpj`, `tradingName`, `industryClassification` no formato
`"Setor / Subsetor / Segmento"`, `otherCodes` com todos os tickers da empresa); (c) `config.py`.

**Mapeamento.**

| Coluna | Regra |
|---|---|
| `ticker` | universo histórico |
| `cnpj` | B3 `GetDetail.cnpj` (só dígitos), conferido com `cad_cia_aberta` |
| `empresa` | **novo mapa manual** `config.NOMES_EMPRESA` (ticker → nome legível curto, ex.: "Klabin", "Petrobras", "Vamos"). Os valores de `IBOV_TICKERS` são termos de busca ("Vamos locacao", "Axia Energia Eletrobras") e não servem. Fallback: `tradingName` da B3 em caixa normal **[minha escolha]** |
| `setor`, `subsetor` | 1º e 2º segmentos de `industryClassification` separados por `" / "`; o 3º (segmento) não é entregue. Atenção: a B3 troca vírgula por ponto no texto ("Petróleo. Gás e Biocombustíveis"); normalizar `". "` → `", "` **[minha escolha]** |
| `tipo_acao` | sufixo do ticker: 3 → ON; 4, 5, 6 → PN; 11 → UNIT; 32 a 35 → `BDR` (caso `JBSS32`; o dono mandou não tratar como caso especial, então segue a regra geral e o que a CVM não tiver fica em branco) **[minha escolha]** |
| `unit_qtd_on`, `unit_qtd_pn` | mapa manual em `config.py` (`UNITS = {"KLBN11": (1, 4), ...}`), só para units; em branco para os demais. Conferir no RI de cada empresa **[spike, lista pequena]** |

Mapa `ticker → codeCVM` fica em `config.py` (85 + removidos), validado por um comando
`validate-cadastro` que confere se o ticker aparece em `otherCodes` **[minha escolha]**.
O endpoint da B3 é interno e sem documentação: throttle próprio (domínio
`sistemaswebb3-listados.b3.com.br`), fixture em teste, e `None` em falha (não levanta).

**Regra diária.** Upsert por `ticker`, uma vez por dia (≈ 90 chamadas; cabe no job).

### 5.4 `mudancas_ticker`

**Fonte.** Lista mantida em `config.py` (`MUDANCAS_TICKER = [("ELET3", "AXIA3", date(...)),
...]`), semeada com as reestruturações já anotadas em comentário (`config.py:137-143`:
EMBR3→EMBJ3, ELET3/ELET6→AXIA3, JBSS3→JBSS32, CPLE6→CPLE3, NTCO3→NATU3, ...). As **datas**
precisam ser levantadas nos avisos da B3 **[spike/construtor]**. Chave `ticker_antigo`:
ELET3 e ELET6 viram duas linhas apontando para AXIA3.

**Regra diária.** Upsert a partir do config. Na Fase B a juíza marca eventos
`mudanca_ticker` em notícias como candidatos (log + alerta), e a inclusão continua manual.

### 5.5 `balancos`

**Fonte.** CVM dados abertos: `https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_AAAA.zip`
e `.../DFP/DADOS/dfp_cia_aberta_AAAA.zip`. Dentro de cada zip: arquivo principal
(`*_cia_aberta_AAAA.csv`: `CNPJ_CIA`, `CD_CVM`, `DT_REFER`, `VERSAO`, `DT_RECEB`, `LINK_DOC`),
`*_DRE_con_AAAA.csv` e `*_BPP_con_AAAA.csv` (verificado: `CD_CONTA`, `DS_CONTA`, `VL_CONTA`,
`ESCALA_MOEDA`, `ORDEM_EXERC`, `DT_INI_EXERC`, `DT_FIM_EXERC`, `VERSAO`). Separador `;`,
codificação latin-1.

**Mapeamento.**

| Coluna | Regra |
|---|---|
| `cnpj` | `CNPJ_CIA` só dígitos |
| `tipo_documento` | `ITR` ou `DFP` conforme o zip |
| `data_inicio_periodo`, `data_fim_periodo` | `DT_INI_EXERC`, `DT_FIM_EXERC` da DRE. ITR traz o trimestre e o acumulado no ano como linhas distintas (início diferente): **as duas são entregues**, como a spec prevê. Para o BPP (PL) não há início: usa-se o mesmo par da DRE correspondente ao `DT_REFER` |
| `data_entrega` | `DT_RECEB` do arquivo principal, pela chave `(CNPJ_CIA, DT_REFER, VERSAO)` |
| `receita_liquida` | `CD_CONTA = 3.01` |
| `lucro_bruto` | `CD_CONTA = 3.03`; em branco quando a conta não existe (bancos e seguradoras) |
| `lucro_liquido` | `CD_CONTA = 3.11` (Lucro/Prejuízo Consolidado do Período) |
| `patrimonio_liquido` | BPP `CD_CONTA = 2.03` (Patrimônio Líquido Consolidado) |

Regras: só `ORDEM_EXERC = 'ÚLTIMO'`; só a maior `VERSAO` por `(CNPJ, tipo, DT_REFER)`
(reapresentação substitui); `ESCALA_MOEDA = 'MIL'` multiplica por 1.000, `'UNIDADE'` não
muda; sempre arquivos `_con` (consolidado); empresa sem consolidado fica em branco (não cai
para o individual) **[minha escolha, segue a spec]**. Só CNPJs do `cadastro`.
Códigos de conta `3.01/3.03/3.11/2.03` **[spike]**: confirmar em 3 empresas (uma industrial,
um banco, uma holding) que os códigos fixos são estes em ITR e DFP.

**Período.** Zips dos anos `ano(DATA_START) − 1` (2020) até o ano corrente (DFP e ITR):
cobre os 5 anos do dataset e mais 1 ano para os 12 meses móveis no início da janela.

**Regra diária.** Baixar os zips **do ano corrente** (ITR e DFP) só se o `Last-Modified`
do servidor mudou (requisição `HEAD` antes) **[minha escolha]**; ler só os 3 CSVs
necessários direto do zip; upsert pela chave da spec. O `raw_cvm_balancos` (append-only,
com `VERSAO` e `DT_RECEB`) preserva as versões anteriores.

### 5.6 `acoes_emitidas`

**Fonte.** Mesmos zips: `*_composicao_capital_AAAA.csv` (verificado: `CNPJ_CIA`, `DT_REFER`,
`VERSAO`, `QT_ACAO_ORDIN_CAP_INTEGR`, `QT_ACAO_PREF_CAP_INTEGR`, `QT_ACAO_TOTAL_CAP_INTEGR`,
e as três de tesouraria). É trimestral, o que é melhor que o FRE anual.

**Mapeamento.** `cnpj = CNPJ_CIA`; `data_referencia = DT_REFER`; `qtd_on =
QT_ACAO_ORDIN_CAP_INTEGR`; `qtd_pn = QT_ACAO_PREF_CAP_INTEGR` (capital integralizado, inclui
tesouraria, que é o "número de ações" usual para P/L e P/VP) **[minha escolha]**. Maior
`VERSAO` por `(CNPJ, DT_REFER)`. Se ITR e DFP tiverem a mesma `DT_REFER` (31/12), vale a DFP.

**Regra diária.** Igual a `balancos` (mesmo download).

### 5.7 `proventos`

**Fontes (B3, verificadas com chamada real).**

1. `listedCompaniesProxy/CompanyCall/GetListedCashDividends/{b64({"language":"pt-br",
   "pageNumber":N,"pageSize":120,"tradingName":"PETROBRAS"})}`: histórico longo (Petrobras:
   343 registros), com `typeStock` (ON/PN), `corporateAction` (`DIVIDENDO`, `JRS CAP
   PROPRIO`, ...), `valueCash` (vírgula decimal), `lastDatePriorEx` (= data-com), `ratio`,
   `quotedPerShares`, `dateApproval`. **Não tem data de pagamento.** Paginação: acima de 120
   por página a B3 devolve vazio.
2. `listedCompaniesProxy/CompanyCall/GetListedSupplementCompany/{b64({"issuingCompany":
   "PETR","language":"pt-br"})}`: `cashDividends` recentes (Petrobras: 24) com `paymentDate`,
   `rate`, `label`, `lastDatePrior`, `isinCode`, `approvedOn`; `stockDividends` com
   `DESDOBRAMENTO`/`BONIFICACAO` e `factor` (base para o ajuste); `numberCommonShares`,
   `numberPreferredShares` (conferência para `acoes_emitidas`).

**Mapeamento.**

| Coluna | Regra |
|---|---|
| `ticker` | prefixo da empresa (`issuingCompany`, 4 letras) + sufixo por `typeStock`: ON → 3, PN → 4, PNA → 5, PNB → 6, UNIT → 11. Confirmar os valores reais de `typeStock` em 5 empresas, incluindo uma com unit e uma com PNA/PNB **[spike]** |
| `tipo` | `DIVIDENDO` → `DIVIDENDO`; `JRS CAP PROPRIO` → `JCP`. Outros rótulos (ex.: "RENDIMENTO", "RESTITUICAO DE CAPITAL") são listados no spike e, até decisão, **não entram** **[minha escolha]** |
| `data_com` | `lastDatePriorEx` |
| `valor_por_acao` | `valueCash / quotedPerShares`, multiplicado pelos fatores de desdobramento/bonificação com `lastDatePrior` **posterior** à `data_com` (`stockDividends.factor`; bonificação de x% vira fator `1 + x/100`). Cruzar com `events=splits` do Yahoo em 3 empresas **[spike]** |
| `data_pagamento` | de `SupplementCompany.cashDividends`, casado por `(isinCode → ticker, lastDatePrior, label, rate)`; em branco se não houver |

Colisão na chave `(ticker, tipo, data_com)` (dois JCPs na mesma data-com): **somar** os
valores **[minha escolha]**, registrando os originais no `raw_b3_proventos`.
Mapa `ticker → tradingName/issuingCompany` vem do `GetDetail` (seção 5.3).

**Período.** Tudo que a B3 devolver (≥ 5 anos).

**Regra diária.** Rebaixar tudo para o universo (≈ 2 chamadas por empresa com throttle) e
upsert: datas de pagamento e valores podem ser corrigidos pela B3.

### 5.8 `indices`

**Fontes (verificadas).**

| `indice` | Fonte | Formato na origem | Entrega |
|---|---|---|---|
| `CDI` | BCB SGS série **12** (`api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados?dataInicial=&dataFinal=`) | `"valor":"0.050788"` = % ao dia | como vem |
| `IPCA` | BCB SGS série **433** | `"data":"01/09/2026","valor":"0.82"` = % no mês | `data` = dia 1 do mês |
| `IDIV`, `SMLL`, `IFIX` | Yahoo chart `IDIV.SA`, `SMLL.SA`, `IFIX.SA` (tipo INDEX) | pontos | `close` do dia |

Limite do SGS: série diária aceita no máximo 10 anos por requisição; o coletor fatia por ano.
Os três índices do Yahoo entram em `raw_prices` com `source = 'yahoo'` e símbolo original;
o `build-entrega` os roteia para `indices`, não para `precos` **[minha escolha]**. Os
`raw_macro.selic` e `ptax_venda` continuam coletados para o dataset v1, mas não são entregues.

**Período.** Igual a `precos` (dataset + 1 ano antes). **Regra diária.** Só acrescenta.

## 6. Qualidade das notícias: regra barata + juíza

### 6.1 Regra barata (sem LLM)

Aplicada a todo artigo antes da juíza, grava a decisão com `modelo = 'regra'`:

- **Domínio bloqueado** (`config.DOMINIOS_BLOQUEADOS`): youtube.com, podcasts, agregadores
  sem conteúdo próprio. Lista inicial curta; cresce com o conjunto-ouro.
- **Padrões de panorama/agenda** no título (regex, `config.PADROES_PANORAMA`): "Ibovespa
  (abre|fecha|sobe|cai|recua|opera)", "agenda do dia", "carteira recomendada", "ações para
  (comprar|investir)", "o que esperar", "fechamento", "resumo do mercado", "veja as ações".
- **Múltiplos tickers no título** (3 ou mais nomes do universo): panorama.

Só o que sobrar vai para a LLM.

### 6.2 Juíza (LLM, sem busca)

**Entrada.** Lotes de até 50 itens: `id`, `ticker`, nome da empresa, setor, `titulo`,
`fonte`, e os primeiros 300 caracteres do corpo quando houver. O contexto fixo (universo com
nomes, apelidos e marcas conhecidas por ticker: `config.APELIDOS = {"VIVT3": ["Vivo",
"Telefônica Brasil"], "JBSS32": ["Seara", "Friboi"], ...}`) vai no prompt como cabeçalho e
fica em cache de contexto quando o SDK permitir.

**Saída (JSON schema, temperatura 0), por item:**

| Campo | Valores |
|---|---|
| `papel` | `protagonista` · `citada_com_fato` (a matéria traz fato específico da empresa) · `mencao` (só citada, panorama) · `errada` (homônimo, outra empresa) · `matriz_estrangeira` |
| `tipo_conteudo` | `reportagem` · `opiniao` · `lista` · `agenda` · `resumo_mercado` · `podcast_video` · `newsletter` · `patrocinado` · `outro` |
| `evento` | `resultado` · `dividendo_jcp` · `fato_relevante` · `fusao_aquisicao` · `guidance` · `recomendacao` · `regulatorio` · `operacional` · `governanca` · `juridico` · `mudanca_ticker` · `macro` · `outro` |
| `tickers_adicionais` | lista de tickers do universo que a matéria também trata (marca/subsidiária: Vivo → VIVT3, Ipiranga → UGPA3) |
| `qualidade` | 0–1 pela rubrica fixa: reportagem, protagonista, fato concreto, veículo conhecido, data coerente |
| `confianca` | 0–1 |

Os casos 1–6 e 10 do brainstorm estão cobertos por esses campos. O caso 7 (recorte do
trecho por ticker para o FinBERT), o 8 (agrupar o mesmo fato) e o 9 (ponte matéria ↔ IPE)
ficam para a Fase C, porque exigem corpo completo e embedding.

**Modelo e custo.** Primeira passada no Jev, se aprovado (seção 6.3); sem ele, ou para a
zona de baixa confiança, `gemini-3.1-flash-lite` (o `.env.example` já o lista como
alternativa barata), sem `google_search`, `response_format` JSON. Limites operacionais em `.env`:
`JUIZA_MAX_ITENS_DIA` (padrão 2.000) e `JUIZA_MAX_TOKENS_DIA`; o backlog histórico é
processado do mais recente para o mais antigo. **[spike]** medir custo real em 1 dia de
artigos antes do histórico, e verificar se o tier gratuito da API cobre esse volume (se
cobrir, custo zero).

**Persistência.**

- `raw_llm_juiza_runs` (append-only): `id`, `prompt_id` (FK `raw_llm_prompts`), `modelo`,
  `n_itens`, `tokens_entrada`, `tokens_saida`, `latencia_ms`, `status`, `erro`,
  `iniciado_em`, `finalizado_em`.
- `staging_noticias_julgamento`: PK `(article_id, ticker, prompt_versao)`; `run_id`,
  `modelo` (`'regra'` ou nome do modelo), `papel`, `tipo_conteudo`, `evento`,
  `tickers_adicionais` (JSON), `qualidade` (Double), `confianca` (Double), `julgado_em`.
- O prompt da juíza segue o padrão de `llm/prompt.py`: `llm/juiza_prompt.py` com `VERSAO`,
  template em `llm/prompts/juiza_v1.txt`, sha256 gravado em `raw_llm_prompts` (coluna `nome`
  distingue `analise_diaria` de `juiza`).

**Conjunto-ouro.** `tests/fixtures/juiza_gold.csv` com ~300 pares (artigo, ticker) rotulados
à mão em `papel` e `tipo_conteudo`, estratificados por caso (homônimos, marcas, panoramas,
matriz estrangeira, opinião). Comando `judge-news --avaliar` imprime precisão e revocação
por classe. Esse número vai para o TCC como método. A rotulagem é do dono do projeto
**[pendente, esforço humano]**.

**Filtro de entrega.** Definido na seção 5.1. A decisão fica numa única função
(`entrega/filtros.py: aprovado_para_entrega(julgamento) -> bool`) para ser trocada sem
mexer no resto.

### 6.3 Jev (TypeSafe AI): onde entra e onde não entra

**O que é (verificado em docs.typesafe.ai em 2026-10-19).** Modelo hospedado da TypeSafe AI,
chamado de "System One model": recebe um texto (`state`) e perguntas tipadas, e devolve
decisões com distribuição de probabilidade e confiança, **nunca texto livre**. Três
primitivas: `Choice` (escolhe 1 entre até 255 opções), `Score` (posição numa rubrica de 2 a
10 níveis), `Noul` (sim/não em 0–1). Várias perguntas por chamada, avaliadas em paralelo
sobre o mesmo texto. API `POST https://api.typesafe.ai/v1/systemone` com chave Bearer, SDK
`typesafe-sdk` (Python 3.10+), modelos `jev-1.13.0` (`jev-latest`) e `jev-1.14.0`
(`jev-preview`), contexto de 64k tokens por requisição. Preço: **US$ 0,042 por milhão de
tokens de entrada; saída grátis**. Latência anunciada de 70 a 500 ms. Inglês é o idioma
principal; outros idiomas "funcionam, mas não igualmente bem; teste no seu conteúdo". O blog
de lançamento (set/2026) fala em early access por lista de espera; a documentação não
menciona. Limites de taxa ficam no console, por organização. Empresa nova; versões mudam.

**Substituir o FinBERT? Não. Acrescentar.** Quatro motivos: a spec pede "resultado do
FinBERT"; a pergunta do TCC é "as notícias, lidas pelo FinBERT, ajudam a prever"; o FinBERT
tem pesos abertos e fixos, roda offline e de graça, e dá o mesmo número hoje e daqui a um
ano (um modelo hospedado que troca de versão não garante isso para um dataset acadêmico);
e a empresa tem um mês de vida. O que o Jev faz melhor que o FinBERT é **sentimento com
alvo**: a instrução pode dizer "em relação à empresa X", o que resolve o caso 7 sem recortar
trecho. Proposta: colunas extras na `noticias`, Fase B, opcionais: `jev_prob_positivo`,
`jev_prob_neutro`, `jev_prob_negativo`, `jev_confianca`, `jev_modelo` (versão fixada). O TCC
ganha uma comparação FinBERT × Jev; se a etapa seguinte achar o sinal do Jev melhor, a troca
é decisão dela, não nossa.

**Onde o Jev entra nas tarefas da LLM:**

| Tarefa | Jev | Como | Fica com o Gemini |
|---|---|---|---|
| `papel`, `tipo_conteudo`, `evento` (juíza) | sim | `Choice` com as opções da 6.2; a instrução cita o ticker, o nome e os apelidos | itens com confiança baixa |
| `qualidade` (juíza) | sim | `Score` com 5 níveis da rubrica | — |
| `tickers_adicionais` | parcial | `Noul` "a matéria trata da empresa X?" só para apelidos achados por regex no texto; `Choice` escolhe uma opção, não várias | extração livre |
| sentimento com alvo (caso 7) | sim | `Choice` positivo/neutro/negativo "em relação à empresa X" | — |
| agrupar o mesmo fato (caso 8) | não | precisa de embedding; `Noul` par a par é quadrático | — (embedding local) |
| ponte matéria–IPE (caso 9) | parcial | `Noul` "esta matéria cobre este fato relevante?" para candidatos do mesmo dia e CNPJ | — |
| tema da notícia macro (caso 12), bandeiras de risco (caso 17) | sim | `Choice` por tema; `Noul` por bandeira | — |
| direção esperada e confiança (v3 `acoes_influenciadas`; casos 14 e 19) | sim | `Choice` alta/baixa/neutra; confiança calibrada em vez de autoavaliada pelo Gemini | — |
| descoberta de notícias com busca (v3), estado do dia (11), explicador de anomalia (15), números extraídos (16), título e resumo do IPE (20) | não | exigem busca na web ou texto livre | sim |

**Roteamento.** Regra barata → Jev (todas as perguntas de classificação num só pedido por
artigo, uma pergunta por ticker candidato) → Gemini só para itens com `confianca` abaixo do
limiar (começar em 0,5, como a documentação sugere, e calibrar no conjunto-ouro) e para as
tarefas de texto. Modelo fixado por versão (`jev-1.13.0`, nunca `jev-latest`), versão gravada
em cada linha de `staging_noticias_julgamento` (coluna `modelo`).

**Custo estimado (entrada; saída é grátis):**

| Cenário | Volume | Tokens | Custo |
|---|---|---|---|
| Por artigo | título + 300 caracteres de corpo + perguntas | ≈ 550 | — |
| Histórico de 5 anos | ≈ 750 mil artigos (85 tickers × ~150/mês × 60 meses; o número real sai do coletor) | ≈ 410 M | ≈ US$ 17, uma vez |
| Diário | ≈ 1.000 artigos | ≈ 0,55 M | ≈ US$ 0,70/mês |

Custo do Gemini para a mesma tarefa: medir no spike, não estimar.

**Riscos e verificações antes de ligar [spike]:** (1) **português**: rodar o conjunto-ouro
(300 pares) em Jev, Gemini e FinBERT (só sentimento) e comparar com o rótulo humano; (2)
**acesso**: criar a conta no console e confirmar se há lista de espera, limites de taxa e
forma de pagamento (cartão não documentado); (3) **custo zero**: Jev é API paga, então
precisa de **exceção explícita do dono**, com teto próprio; (4) **versões**: fixar
`jev-1.13.0`; (5) **disponibilidade**: o pipeline nunca depende do Jev para produzir as
colunas da spec; se a API cair, a juíza cai para o Gemini e o FinBERT segue sozinho.

## 7. IPE da CVM como fonte oficial

**Fonte (verificada).** `https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_AAAA.zip`
(2026: 1,7 MB, 37 mil linhas até outubro). Colunas: `CNPJ_Companhia`, `Nome_Companhia`,
`Codigo_CVM`, `Data_Referencia`, `Categoria`, `Tipo`, `Especie`, `Assunto`, `Data_Entrega`,
`Tipo_Apresentacao`, `Protocolo_Entrega`, `Versao`, `Link_Download`. **`Data_Entrega` é só
data (AAAA-MM-DD), sem hora.** `Link_Download` devolve o PDF.

**Categorias que entram** **[minha escolha]**: `Fato Relevante`, `Comunicado ao Mercado`,
`Aviso aos Acionistas`. Ficam de fora: `Valores Mobiliários negociados e detidos` (formulário
de insiders, não é notícia), `Assembleia`, `Reunião da Administração`, `Dados
Econômico-Financeiros` (é o balanço, já coberto), `Calendário de Eventos Corporativos`.
Só CNPJs do `cadastro`.

**Ingestão.** `raw_ipe_docs` (append-only, todas as colunas do CSV + `fetched_at`), chave
`Protocolo_Entrega`. Cada documento vira também um `raw_articles` (para passar pelo mesmo
funil): `url = Link_Download`, `url_hash` como sempre, `title = Assunto`, `source_name =
"CVM"`, `source_domain = "rad.cvm.gov.br"`, `lang = "pt"`, `published_at = Data_Entrega`
às 00:00 de São Paulo gravado em UTC, e a **nova coluna** `raw_articles.published_precision`
(`'datetime'` padrão | `'date'`). Vínculo em `staging_article_tickers` com **todos** os
tickers da empresa (PETR3 e PETR4). O FinBERT pontua o `Assunto`. O julgamento é automático:
`papel = protagonista`, `tipo_conteudo = reportagem`, `evento = fato_relevante`,
`modelo = 'regra'`.

**Hora da entrega.** A hora exata existe no sistema RAD da CVM, mas não no CSV. Decisão do
dono: a hora pode faltar, a data não, e o que importa é saber se o documento saiu antes ou
depois do pregão. Por isso:

- `data_publicacao` = `Data_Entrega` (sempre); `data_hora_publicacao` em branco;
  `momento_pregao = desconhecido` enquanto não houver hora. Nunca meia-noite como se fosse
  a hora.
- **[spike prioritário]** recuperar a hora por documento no RAD: a página
  `frmExibirArquivoIPEExterno.aspx?NumeroProtocoloEntrega=` responde HTML sem a hora no
  corpo estático; inspecionar a consulta externa do RAD (grade "Data/Hora Entrega") e o
  cabeçalho do PDF. Se funcionar, um passo do `enrich` preenche `published_at` com hora,
  muda `published_precision` para `'datetime'` e `momento_pregao` passa a ser calculado.
  Fatos relevantes costumam ser protocolados depois das 18h, então esse campo é o que
  separa "reagiu hoje" de "reage amanhã" para a etapa seguinte.

**Fase C.** Texto do PDF extraído localmente (pypdf) para a LLM gerar título e resumo em
linguagem simples (casos 9 e 20); o `Assunto` costuma ser descritivo ("Petrobras aumenta
Reservas Provadas em 2025"), então a Fase B já é útil sem isso.

**Período.** Zips de `ano(DATA_START − 31 dias)` até o ano corrente. **Regra diária.** Zip
do ano corrente com `HEAD`/`Last-Modified` (como balanços); só protocolos novos.

## 8. Universo histórico (viés de sobrevivência)

`config.UNIVERSO_HISTORICO`: lista de `(ticker, data_entrada | None, data_saida | None)`.
Semeada com os 85 atuais (`entrada = None`) e com os removidos anotados no config
(`CRFB3`, `STBP3`, `SRNA3`, com `saida` na data em que deixaram o índice). As três
rebalanceamentos anuais da B3 (janeiro, maio, setembro) são a fonte das datas
**[spike/construtor]**: levantar as carteiras teóricas do período (B3 publica a vigente;
as anteriores via avisos de rebalanceamento ou Wayback Machine).

`IBOV_TICKERS` continua existindo (é o universo "vigente", usado pelo `analyze-llm`). Os
coletores de preços, notícias, cadastro, balanços e proventos usam a **união** do universo
histórico (sem recortar por data de entrada/saída: é mais simples e o custo extra é de
poucos tickers) **[minha escolha]**. `cadastro` entrega todos.

## 9. Job diário e carga histórica

### 9.1 Comando `daily` (novo)

Orquestra, em ordem, com um registro em `raw_collection_runs` por etapa, sem abortar nas
etapas seguintes quando uma independente falha (mesmo espírito do `coleta-completa.sh`),
e sempre executando o `build-entrega` ao final com o que houver:

1. `collect-prices` (histórico completo, upsert) e índices do Yahoo
2. `collect-macro` (Selic/PTAX, como hoje) e `collect-indices` (SGS 12 e 433)
3. `collect-news --desde D-2` (Google News, janelas recentes; RSS nativos só na Fase B)
4. `collect-ipe` (ano corrente)
5. `enrich` e `score` (só pendentes)
6. `judge-news` (só pendentes, dentro do orçamento do dia)
7. `collect-cadastro`, `collect-proventos`, `collect-cvm` (balanços + ações emitidas)
8. `build-entrega` (8 tabelas: upsert + CSVs em `exports/entrega/`)
9. `analyze-llm` (v3, como hoje)

**Horário.** Cron do Railway `30 21 * * *` (UTC) = 18:30 em Brasília; prazo 20:00. O Yahoo
já tem a barra do dia após o leilão de fechamento (≈ 18:00). Orçamento: 90 minutos;
cada etapa com `timeout` próprio em `.env` e o `judge-news` com o orçamento de itens.
Dias sem pregão: o job roda igual (notícias e IPE continuam), `precos` não ganha linha.

**FinBERT no Railway.** Hoje o `Dockerfile` instala só `[mysql,llm]` (sem torch) e o cron
roda apenas o `analyze-llm`. Para o `daily` rodar no Railway, a imagem precisa de
`[sentiment]` com **torch CPU** (`--index-url https://download.pytorch.org/whl/cpu`) e os
dois modelos baixados **no build** (cache do Hugging Face dentro da imagem), para não baixar
~900 MB a cada execução do cron. Estimativa: imagem de 2 a 3 GB e 1,5 GB de RAM na
pontuação **[spike]**: medir no plano do Railway em uso. Decisão do dono: **roda no
Railway**. Plano B: o `score` roda no servidor do MySQL (onde o `coleta-completa.sh` já
roda) e o Railway faz o resto; plano C: modelos em ONNX int8.

**Jev (TypeSafe AI).** O dono pediu para avaliar a substituição do FinBERT pelo Jev e quais
tarefas da LLM poderiam rodar nele. Análise na seção 6.3: o Jev entra como classificador da
juíza e como segunda opinião de sentimento (colunas extras), não como substituto do FinBERT.

**Banco.** O `daily` escreve no MySQL remoto com o usuário `tcc_railway` (sem DDL). As
migrations (seção 10) continuam aplicadas da máquina local, como documentado em
`docs/LLM-ANALISE.md`. Decisão do dono: **o consumidor lê as 8 tabelas direto nesse MySQL**,
e os nomes dos arquivos da spec são os nomes das tabelas.

**CSV e sistema de arquivos.** O filesystem do Railway é efêmero: nada gravado em
`exports/` sobrevive ao fim do cron. Por isso **o canal de entrega diário é o banco**; o
`daily` **não** gera CSV. Os CSVs (dump completo, nome fixo, seção 3) saem de
`build-entrega --csv`, rodado onde houver disco (máquina local ou servidor do banco), para
auditoria e para quem quiser um arquivo **[minha escolha; o canal oficial é o banco]**.

### 9.2 Carga histórica

Roda **fora** do Railway (máquina local ou servidor do banco), uma vez, pelo
`scripts/coleta-completa.sh` estendido com as etapas novas na mesma ordem. Custo dominante
continua sendo o Google News (6–9 h por ano de notícias) e, agora, a juíza sobre o backlog
(orçada no spike). CVM, B3, BCB e Yahoo para 5 anos levam minutos. Decisão do dono: **tudo
do zero**, 5 anos (seção 3.1), em segmentos anuais retomáveis, do ano mais recente para o
mais antigo, para que a entrega parcial já sirva à etapa seguinte enquanto o resto carrega
**[minha escolha]**.

### 9.3 Janela de datas e integridade do dataset v1

Hoje `Settings.data_end` é fixo e governa o `period2` do Yahoo, o BCB, as janelas do Google
News e o recorte do v1; o docstring de `config.py` trata a janela congelada como condição de
reprodutibilidade do TCC. Decisões **[minha escolha]**:

- Entra `Settings.data_fim_coleta`, **rolante** (padrão: hoje), usada por todos os coletores.
  `data_start`/`data_end` continuam fixos e só o `build-dataset` (v1) os usa.
- `build-dataset` **não** entra no `daily`. O v1 é um artefato congelado do TCC
  (`features_dataset_v1` e os Parquet/CSV já exportados).
- Se alguém reexecutar `build-dataset`, os valores saem **idênticos**: a agregação de
  notícias do v1 passa a filtrar `raw_articles.origem = 'google_news'` e a usar só
  `staging_article_tickers` (os vínculos da busca). Por isso a juíza grava
  `tickers_adicionais` só em `staging_noticias_julgamento`, nunca em
  `staging_article_tickers`, e o IPE entra com `origem = 'cvm_ipe'`. Há teste de regressão
  para isso (seção 10).

## 10. Mudanças de schema, código e testes

**Alembic `0004_entrega`** (Postgres e MySQL; DDL de referência em `sql/`):

- `raw_articles.published_precision` String(8) NOT NULL DEFAULT `'datetime'` e
  `raw_articles.origem` String(16) NOT NULL DEFAULT `'google_news'` (seção 9.3).
- Novas `raw_ipe_docs`, `raw_cvm_balancos`, `raw_cvm_acoes`, `raw_b3_cadastro`,
  `raw_b3_proventos`, `raw_llm_juiza_runs`, `staging_noticias_julgamento`.
- As **8 tabelas de entrega com o nome exato da spec**, sem prefixo de camada (confirmado
  pelo dono: "cada nome de arquivo é uma sugestão de tabela"). A `noticias` leva as 10
  colunas da spec e as 7 extras da seção 5.1, nessa ordem. Tipos: `Double` para valores,
  `BigInteger` para volume e quantidades, `String` ISO com offset para `data_hora_*`
  (texto, para preservar o fuso no MySQL) **[minha escolha]**, `Date` para datas.

**Módulos novos** (todos coletores herdam de `collectors/base.Collector`):
`collectors/cvm_ipe.py`, `collectors/cvm_demonstracoes.py`, `collectors/b3_cadastro.py`,
`collectors/b3_proventos.py`, `collectors/indices.py` (SGS 12/433; Yahoo reaproveita
`prices_yahoo`), `llm/juiza.py` + `llm/juiza_prompt.py` + `llm/prompts/juiza_v1.txt`,
`entrega/builder.py` (monta as 8 tabelas a partir das `raw`/`staging`), `entrega/filtros.py`,
`entrega/writers.py` (CSV com ordem exata de colunas, vazio para nulo). Novos subcomandos
no `cli.py`: `collect-ipe`, `collect-cvm`, `collect-cadastro`, `collect-proventos`,
`collect-indices`, `judge-news`, `build-entrega`, `validate-cadastro`, `daily`.
`collect-news` ganha `--desde`/`--ate`. `config.py` ganha `UNIVERSO_HISTORICO`,
`MUDANCAS_TICKER`, `UNITS`, `APELIDOS`, `NOMES_EMPRESA`, `TICKER_CODE_CVM`,
`DOMINIOS_BLOQUEADOS`, `PADROES_PANORAMA`, `PRICE_WARMUP_DAYS = 400` e
`Settings.data_fim_coleta`.

Correção de passagem (achado do mapeamento): `enrich/article_fetcher.py:225-230` grava
`resolved_url` com um `execute` por linha dentro de laço; trocar por `bulk_upsert`.

**Testes** (sem rede, fixtures em `tests/fixtures/`): parser de cada fonte nova (IPE, DRE,
BPP, composição de capital, GetDetail, CashDividends, SupplementCompany, SGS 12/433);
ordem e nomes exatos das colunas das 8 tabelas; vazio em vez de zero (dólar sem volume,
banco sem lucro bruto); escala `MIL` → reais; ajuste de provento por desdobramento;
colisão de chave em proventos; filtro de entrega por `papel`/`tipo_conteudo`; roteamento
dos índices do Yahoo para `indices`; `data_hora_publicacao <= data_hora_coleta` em toda
linha; `published_precision = 'date'` nunca gera hora; juíza contra o conjunto-ouro
(precisão mínima por classe como limiar); paridade Postgres/MySQL das 8 tabelas (mesmo
padrão de `test_batch_regression.py`); **regressão do v1**: com IPE, RSS nativo e
`tickers_adicionais` presentes na base, `build-dataset` produz exatamente as mesmas linhas
de antes.

**Pegadinhas conhecidas para o plano** (levantadas na revisão do desenho, decididas pelo
construtor): o checkpoint do Google News é por janela semanal (`iter_weeks`), então
`--desde`/`--ate` precisam gerar chaves próprias e não colidir com as da carga histórica;
`collect-prices` diário roda com `resume=False`, porque a unidade de checkpoint é o ticker e
"já coletado" nunca seria revisitado; `llm/gemini_client.py:_chamar_api` fixa a ferramenta
`google_search` na chamada, e a juíza precisa de um caminho **sem** ela; uma linha de
`noticias` cujo `score` falhou sai sem `prob_*` e é regravada quando o score chegar (a regra
"nunca reescreve" vale para linhas completas); `enrich/article_fetcher.py:225-230` grava
`resolved_url` linha a linha e deve passar a `bulk_upsert`.

## 11. Fases

| Fase | Entrega | Depende de |
|---|---|---|
| **A: 8 tabelas** | Todas as tabelas com fontes oficiais, `daily` no Railway, carga histórica de 5 anos do zero (ano a ano, do mais recente ao mais antigo), CSVs sob demanda. `noticias` sem filtro e com as colunas da juíza em branco. | Spikes de B3 (`typeStock`, rótulos), CVM (códigos de conta), Yahoo (histórico sob código novo), Google News (cobertura de 2021), Railway (FinBERT) |
| **B: qualidade** | IPE + regra barata + juíza + filtro de entrega + conjunto-ouro. | Fase A; rotulagem do conjunto-ouro; spike de custo da juíza; spike da hora do IPE |
| **C: análise diária v4** | Casos 7–9 e 11–20 do brainstorm (estado do dia com números calculados, humor e temperatura, explicador de anomalias, extração de eventos, bandeiras de risco, agenda, fato relevante em linguagem simples). Tabelas próprias, fora da spec. | Documento de desenho próprio, após a Fase B |

## 12. Decisões do dono (2026-10-19) e pendências

**Respondidas:**

| Pergunta | Decisão | Onde está no desenho |
|---|---|---|
| Canal de entrega | Direto no MySQL remoto; nome do arquivo = nome da tabela | 9.1, 10 |
| Período | **5 anos**, do zero | 3.1, 9.2 |
| Filtro e colunas da `noticias` | Pode ter mais colunas; entregar só o que tem qualidade e o mais pronto possível | 5.1 (colunas extras + filtro) |
| Railway | Pode rodar lá, inclusive o FinBERT | 9.1 |
| Histórico já carregado? | Não reaproveitar: tudo do zero | 3.1, 9.2 |
| Hora do IPE | Hora pode faltar, data não; saber se saiu antes ou depois do pregão | 5.1 (`data_publicacao`, `momento_pregao`), 7 |
| `JBSS32` | Não tratar como caso especial | 5.3 |

**Ainda abertas:**

- **Q6. Conjunto-ouro.** Para medir a juíza é preciso uma "resposta certa" feita por pessoa:
  ~300 pares (manchete, ticker) rotulados à mão em `papel` e `tipo_conteudo`. Proposta para
  reduzir o esforço: a LLM pré-rotula e o dono só revisa e corrige (cerca de 1 h), antes da
  Fase B. Sem isso a precisão da juíza fica sem número no TCC.
- **Q9. Jev.** Analisado na seção 6.3. Falta: (a) aprovar o Jev como **segunda exceção à
  regra de custo zero**, com teto próprio (sugestão: US$ 20 uma vez para o histórico de 5
  anos e US$ 2/mês depois); (b) criar a conta no console da TypeSafe (o blog fala em early
  access) e confirmar limites de taxa e forma de pagamento; (c) rodar o spike de português
  no conjunto-ouro antes de ligar o Jev na juíza.

## 13. O que fica para o construtor

Estrutura interna dos módulos; formato do `raw_*` de cada fonte (desde que append-only e
com as colunas de origem); estratégia de paginação e throttle por domínio da B3 e da CVM;
como fatiar o SGS por ano; parsing de latin-1 e vírgula decimal; organização dos testes;
texto exato do prompt da juíza (respeitando os campos e valores da seção 6.2); escolha
entre HEAD/Last-Modified e ETag; e a ordem de implementação dentro de cada fase.

## 14. Referências verificadas em 2026-10-19

- CVM IPE: `dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/` e `.../META/meta_ipe_cia_aberta.txt`
- CVM ITR/DFP: `.../DOC/ITR/DADOS/`, `.../DOC/DFP/DADOS/`, dicionários em `.../META/`
- CVM cadastro: `.../CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv`
- B3: `sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall/{GetDetail, GetListedCashDividends, GetListedSupplementCompany, GetInitialCompanies}/{base64 json}`
- BCB: `api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados` (CDI diário), `.../bcdata.sgs.433/dados` (IPCA mensal)
- Yahoo: `query1.finance.yahoo.com/v8/finance/chart/{IVVB11.SA, IDIV.SA, SMLL.SA, IFIX.SA}`
- Brapi sem token **não** serve índices (401); fica só como fallback de preço com token free.
- Índices de sentimento de notícia na literatura (para o TCC): Daily News Sentiment Index
  (Fed de São Francisco) e IIE-Br Mídia (FGV IBRE).
