# Entrega: as 11 tabelas para a etapa seguinte

Este documento cobre as 11 tabelas entregues: as 8 da spec (`v1-noticias` a `v1-indices`), `v1-composicao_ibov`, que não está na spec (seção 10), `v1-eventos_acoes`, que entrou pelo adendo de 2026-11-02 da spec (seção 11), e `v1-emissores_ticker`, que entrou pelo adendo de 2026-11-03 (seção 12). Para cada uma: a origem de cada coluna, a chave, a regra de atualização e as regras especiais. A especificação do que foi pedido está em [spec-entrega-etapa-seguinte.md](spec-entrega-etapa-seguinte.md), e o desenho detalhado em [superpowers/specs/2026-10-19-entrega-etapa-seguinte-design.md](superpowers/specs/2026-10-19-entrega-etapa-seguinte-design.md). Este texto descreve o código atual.

## 1. Visão geral

| Tabela | Chave | Fonte principal |
|---|---|---|
| `v1-noticias` | (`id`, `ticker`) | `raw_articles`, juíza, FinBERT, IPE |
| `v1-precos` | (`data`, `ticker`) | `raw_prices` (Yahoo) |
| `v1-cadastro` | (`ticker`) | `raw_b3_cadastro`, `raw_cvm_cadastro` |
| `v1-mudancas_ticker` | (`ticker_antigo`) | `MUDANCAS_TICKER` em `config.py` |
| `v1-balancos` | (`cnpj`, `tipo_documento`, `data_inicio_periodo`, `data_fim_periodo`) | `raw_cvm_balancos` |
| `v1-acoes_emitidas` | (`cnpj`, `data_referencia`) | `raw_cvm_acoes`, `v1-eventos_acoes` (base atual) |
| `v1-eventos_acoes` | (`ticker`, `data_com`, `tipo`, `isin_emitido`) | `v1-raw_b3_suplemento` (eventos em ações) |
| `v1-proventos` | (`ticker`, `tipo`, `data_com`) | `raw_b3_proventos`, `raw_b3_suplemento`, `raw_b3_cadastro` |
| `v1-indices` | (`data`, `indice`) | `raw_prices` (Yahoo) e `raw_macro` (BCB) |
| `v1-composicao_ibov` | (`ticker`, `vigencia_inicio`) | Carteiras oficiais em `data/composicao_ibov/`, `REBALANCEAMENTOS_IBOV`, `EVENTOS_IBOV_EXTRAORDINARIOS` e `MUDANCAS_TICKER` em `config.py`, coletor `b3_carteira_ibov` |
| `v1-emissores_ticker` | (`ticker`, `ordem`) | `EMISSORES_POR_PERIODO` em `config.py` (emissor de cada ticker por período) |

Todas levam o prefixo `v1-`, como as demais tabelas da pipeline nova (ver [06-banco-de-dados.md](06-banco-de-dados.md)).

**Código:**

- `entrega/builder.py`: as funções `montar_*` são puras (recebem listas de dicionários e devolvem as linhas). `build_entrega` lê as fontes, monta cada tabela, grava por upsert e, se pedido, exporta CSV.
- `entrega/filtros.py`: a decisão de qualidade das notícias, em `aprovado_para_entrega`.
- `entrega/writers.py`: escrita dos CSVs.
- `db/models_entrega.py`: definição das tabelas e a ordem das colunas (`COLUNAS`).

**Gravação.** Cada tabela passa por `bulk_upsert` pela chave da spec. As colunas que não fazem parte da chave são atualizadas quando a linha já existe. Tabela sem linhas não faz upsert.

**Execução.** A etapa `entrega` (ordem 8) do daily chama `build_entrega`. Uma tabela que falha não impede as outras. Se alguma tabela faltar no resultado, a etapa termina como `falhou`.

## 2. Notícias: `v1-noticias`

Chave: (`id`, `ticker`). Uma linha por par (artigo, ticker) aprovado.

### 2.1 Colunas da spec

| Coluna | Origem | Regra |
|---|---|---|
| `id` | `raw_articles.id` | `n` seguido do id com zeros à esquerda até 6 dígitos (`n000123`). Cresce sem colisão além disso |
| `ticker` | `article_tickers` e `tickers_adicionais` da juíza | Uma linha por par. Tickers adicionais só entram se estiverem no universo de coleta |
| `data_hora_publicacao` | `raw_articles.published_at` | ISO 8601 no fuso de São Paulo, com offset. **Vazia** quando `published_precision = 'date'` (documentos do IPE e notícias do Google News sem hora, ver 2.3) |
| `data_hora_coleta` | `raw_articles.collected_at` | ISO 8601 no fuso de São Paulo |
| `titulo` | `raw_articles.title` | Sem o sufixo com o nome do veículo |
| `fonte` | `source_name`, senão `source_domain` | Documento do IPE: `CVM` |
| `link` | `raw_articles.resolved_url`, senão `url` | Para documentos do IPE, o que o coletor gravar em `url` |
| `prob_positivo`, `prob_neutro`, `prob_negativo` | `article_sentiment` (FinBERT) | Linha do modelo do idioma do artigo: `pt` usa `lucas-leme/FinBERT-PT-BR`, `en` usa `ProsusAI/finbert`. Sem linha, vazio |

### 2.2 Colunas extras

Depois das 10 colunas da spec, a tabela tem 7 colunas extras, nesta ordem:

| Coluna | Valores | Regra |
|---|---|---|
| `data_publicacao` | `AAAA-MM-DD`, sempre preenchida | Data de `published_at` no fuso de São Paulo |
| `momento_pregao` | `antes_abertura`, `durante`, `apos_fechamento`, `sem_pregao`, `desconhecido` | Ver a regra abaixo |
| `origem` | `google_news` (padrão), `wordpress`, `cvm_ipe` | `raw_articles.origem` |
| `papel` | `protagonista`, `citada_com_fato`, `mencao`, `errada`, `matriz_estrangeira` | Julgamento da juíza do próprio ticker. Vazio sem julgamento |
| `tipo_conteudo` | ver [04-juiza-e-llm.md](04-juiza-e-llm.md), seção 2.3 | Julgamento da juíza |
| `evento` | ver [04-juiza-e-llm.md](04-juiza-e-llm.md), seção 2.3 | Julgamento da juíza |
| `qualidade` | Número de 0 a 1 | Nota da juíza (é um número gerado pela LLM; ver a seção 6) |

### 2.3 Regras especiais

- **Janela.** Artigos publicados a partir de `DATA_START` menos 31 dias (`inicio_noticias`), à meia-noite de São Paulo.
- **Momento do pregão.** A hora de publicação, no fuso de São Paulo, é comparada com o calendário de pregões, que são as datas em que existe preço do `^BVSP`:
  - `sem_pregao`: a data não tem pregão (fim de semana, feriado);
  - `desconhecido`: só a data é conhecida (`published_precision = 'date'`);
  - `antes_abertura`: antes das 10:00;
  - `durante`: das 10:00 às 17:59;
  - `apos_fechamento`: a partir das 18:00.

  Notícias do Google News só têm a data quando o RSS entrega 00:00 de Los Angeles. Nesse caso `published_precision = 'date'` e a hora vira `desconhecido`, a menos que a própria página da matéria traga a hora (`published_time_source = 'pagina'`, ver `docs/09-historico-de-mudancas.md`, seção 12).

  Antes de 2026-11-03, parte dessas matérias tinha a hora falsa de 00:00 do Pacífico gravada como hora exata (em geral caindo em `antes_abertura`). O `fix-news-times` (`corrigir_horas_publicacao`, em `enrich/article_fetcher.py`), fase (a), remarca essas matérias para `date`, com `published_at` às 00:00 de São Paulo do mesmo dia. O dia não muda, então `features_dataset_v1` não muda. Em `v1-noticias`, a linha passa a ter `data_hora_publicacao` vazia e `momento_pregao` = `desconhecido`. A fase (b), opcional, busca a hora na página (~15.900 páginas).
- **Julgamento por ticker.** Cada par leva o julgamento do próprio ticker, na versão mais alta do prompt da juíza. Tickers adicionais herdam o julgamento do ticker principal do artigo.
- **Emissor por período (IPE, 2026-11-03).** Um documento do IPE só fica no ticker se o emissor desse ticker valia na `data_entrega` do documento (ver seção 12). Por isso, as 7 notas da Natura Cosméticos ligadas a `NATU3` saem de `v1-noticias`: a Natura Cosméticos (CVM 19550) não era o emissor de `NATU3` no período em que elas foram entregues.
- **Filtro da juíza.** Controlado por `ENTREGA_FILTRO_JUIZA`, com padrão `false`.
  - Com o filtro **desligado**, entra todo par com vínculo, inclusive panoramas e opiniões. Os campos da juíza saem vazios quando não há julgamento.
  - Com o filtro **ligado**, documentos do IPE entram sempre. As demais notícias só entram se `tipo_conteudo = reportagem` e `papel` for `protagonista` ou `citada_com_fato`. Artigo sem julgamento fica de fora.
- **Duplicatas de história.** Um artigo que `v1-staging_historias` marca como duplicata (republicação entre InfoMoney, Money Times e Exame, ou uma duplicata do Google News que aponta para o representante) não entra em `v1-noticias`. A história conta uma vez, pelo representante, que é o post de `date_gmt` mais antigo.
- **Remoção de reprovadas.** Com o filtro ligado, a montagem apaga da tabela as linhas que a juíza reprovou. Um artigo da janela que tem julgamento na versão vigente, mas nenhuma linha aprovada, sai pelo `id`. Um par que deixou de valer sai pela chave composta. Com o filtro desligado, saem só os pares obsoletos. Artigo sem julgamento na versão vigente fica como está.

## 3. Preços: `v1-precos`

Chave: (`data`, `ticker`). Fonte: `raw_prices`, do Yahoo. Exceção: a AZUL3 de 2026-01-13 a 2026-04-17 vem da COTAHIST da B3 (`data/correcoes_precos/AZUL3.csv`, gerado por `gerar-correcao-precos`), com `source` = `b3_cotahist`.

**Emendas de troca de código (2026-11-03).** `CORRECOES_PRECO_B3` ganhou duas entradas, com a mesma origem (COTAHIST) e o mesmo CSV em `data/correcoes_precos/`:

- `JBSS32`: série `JBSS3` de 2019-12-02 a 2025-06-06. Com `fator_troca` = 0,5 (1 BDR = 2 ações), o preço sai ×2 e o volume ×0,5. Corrige a alta falsa de +100% do JBSS32 em 2025-06-09, que o Yahoo emendava sem ajuste.
- `NATU3`: série `NTCO3` de 2019-12-18 a 2025-07-01, sem fator. Corrige os 4 dias falsos a R$ 36,86 (placeholder do Yahoo).

O CSV ganha as colunas `fator_troca` e `fator_dividendos`. O `adj_close` da correção é ajustado por proventos em dinheiro da B3 (fator = 1 − provento ÷ fechamento na data-com) e emendado na base do Yahoo pelo primeiro pregão depois da janela.

**Dividendos do Yahoo na correção do JBSS32 (2026-11-03).** A B3 não devolve os proventos da JBS S.A. depois de 2019-04-30, mas a JBS pagou dividendos de 2020 a 2025. A correção usa `proventos_yahoo="JBSS32.SA"`: os eventos de dividendo do Yahoo na janela entram no fator de dividendos, com data-com = pregão anterior à data ex. Os que a B3 já tem na mesma data-com ficam de fora. O resultado vai para `data/proventos_yahoo/JBSS32.csv` (gerado por `gerar-correcao-precos`, sem DDL) e para `v1-proventos`, na seção 8.

| Coluna | Origem | Regra |
|---|---|---|
| `data` | `date` | Data do pregão |
| `ticker` | `symbol` | Ações sem o sufixo `.SA`. Séries macro pelo nome de `MACRO_ENTREGA` |
| `abertura`, `maxima`, `minima`, `fechamento` | `open`, `high`, `low`, `close` | Como vêm do Yahoo |
| `fechamento_ajustado` | `adj_close` | Ajustado do Yahoo. Nas séries macro, é igual ao `close` |
| `volume` | `volume` | Volume zero vira vazio |

**Séries incluídas.** Ações do universo e as séries de `MACRO_ENTREGA`:

| Símbolo | Ticker na tabela | Volume |
|---|---|---|
| `^BVSP` | `IBOV` | Vazio |
| `USDBRL=X` | `USDBRL` | Vazio |
| `BZ=F` | `BRENT` | Vazio |

Os índices `IDIV`, `SMLL` e `IFIX` não entram aqui. Vão para `v1-indices`. Linhas sem `close` são descartadas.

**Abertura do IBOV.** A coluna `abertura` fica vazia quando a fonte repete o fechamento anterior: se o `open` do dia é igual (com 2 casas) ao `close` do pregão anterior da série `IBOV`, grava-se `NULL`. É defeito do Yahoo, que faz isso em ~32% dos dias. A regra vale só para o IBOV, e a camada `raw_prices` não muda.

**Atualização.** Cada execução regrava todas as linhas de `raw_prices` pela chave. Quando o Yahoo muda o ajustado de um dia, a linha nova substitui a antiga.

## 4. Cadastro: `v1-cadastro`

Chave: (`ticker`). Uma linha por ticker do universo de coleta.

| Coluna | Origem | Regra |
|---|---|---|
| `ticker` | Universo de coleta | Inclui os tickers deslistados (CRFB3, STBP3, SRNA3) |
| `cnpj` | `raw_b3_cadastro.cnpj` | Se vazio, `raw_cvm_cadastro` pelo código CVM |
| `empresa` | `NOMES_EMPRESA` | Se o ticker não tiver nome cadastrado: nome da B3 (`trading_name`), depois `DENOM_SOCIAL` da CVM, depois o próprio ticker |
| `setor`, `subsetor` | `industry_classification` da B3 | Primeiro e segundo segmentos de "Setor / Subsetor / Segmento". O terceiro segmento não é entregue. A B3 usa ponto onde o português pede vírgula; a função corrige isso |
| `tipo_acao` | Sufixo do ticker | `3` = `ON`; `4`, `5`, `6` = `PN`; `11` = `UNIT`; `32` a `35` = `BDR`. Ticker sem sufixo reconhecido é pulado, com aviso no log |
| `unit_qtd_on`, `unit_qtd_pn` | `UNITS` em `config.py` | Só para as units: quantidade de ações ON e PN que compõem a unit. Vazio para os demais |

Units cadastradas: `KLBN11` (1 ON, 4 PN), `BPAC11` (1, 2), `ENGI11` (1, 2), `IGTI11` (1, 2), `SANB11` (1, 1) e `TAEE11` (1, 2). Os valores devem ser conferidos no RI de cada empresa.

**Atualização.** Upsert por `ticker`.

## 5. Mudanças de ticker: `v1-mudancas_ticker`

Chave: (`ticker_antigo`). Fonte: `MUDANCAS_TICKER` em `config.py`.

| Antigo | Novo |
|---|---|
| EMBR3 | EMBJ3 |
| ELET3 | AXIA3 |
| ELET6 | AXIA3 |
| JBSS3 | JBSS32 |
| CPLE6 | CPLE3 |
| NTCO3 | NATU3 |
| ODPV3 | SAUD3 |
| AZUL4 | AZUL3 |
| PETZ3 | AUAU3 |
| CCRO3 | MOTV3 |

- A tabela registra troca de código, não fusão. A fusão BRF + Marfrig que gerou MBRF3 fica de fora, porque não renomeia um ticker.
- `data_mudanca` é o primeiro pregão com o código novo, levantado em 2026-10-23 nos comunicados das companhias e avisos da B3.
- Três trocas passaram por código intermediário, que não entra na tabela: ELET6 → AXIA6 → AXIA3, CPLE6 → CPLE5 → CPLE3 e AZUL4 → AZUL54/AZUL53 → AZUL3. A data registrada é a da chegada ao código final. O detalhe está no comentário de `MUDANCAS_TICKER`.

## 6. Balanços: `v1-balancos`

Chave: (`cnpj`, `tipo_documento`, `data_inicio_periodo`, `data_fim_periodo`). Fonte: `raw_cvm_balancos`, dos arquivos de DFP e ITR da CVM (dados abertos), nas demonstrações DRE, BPA, BPP e DFC (método indireto).

| Coluna | Origem | Regra |
|---|---|---|
| `cnpj`, `tipo_documento` | `CNPJ_CIA`, tipo do documento (DFP ou ITR) | Vêm do registro |
| `data_inicio_periodo`, `data_fim_periodo` | `DT_INI_EXERC`, `DT_FIM_EXERC` da DRE | No ITR, o trimestre e o acumulado do ano são linhas diferentes, e as duas são entregues |
| `data_entrega` | `DT_RECEB` da DRE do período | |
| `receita_liquida` | DRE, conta `3.01` | Para a HAPV3, com o ajuste de seguros IFRS 17 (ver regras especiais). Em 2026-11-03 o significado mudou só para ela |
| `lucro_bruto` | DRE, conta `3.03` | A descrição precisa começar com "resultado bruto" e não pode conter "intermediacao" nem "operacoes". Em branco para bancos e seguradoras |
| `lucro_liquido` | DRE, conta `3.11`, senão `3.09` | A descrição precisa começar com "lucro/prejuizo consolidado do periodo" ou "lucro/prejuizo do periodo" |
| `patrimonio_liquido` | BPP, conta `2.08`, senão `2.03` | Linha com `DT_FIM_EXERC` igual à data da DRE. A descrição precisa começar com "patrimonio liquido" |
| `ebit` | DRE, conta `3.05` | A descrição precisa começar com "resultado antes do resultado financeiro". Em branco para bancos e seguradoras |
| `depreciacao_amortizacao` | DFC (método indireto), contas `6.01.01.NN` (4 níveis) | Soma das linhas cuja descrição contém "deprecia", "amortiz", "exaust" ou "deplec", sem termos de exclusão (custo de transação, captação, juros, estoque etc.). Valor sempre positivo. Em branco para bancos e seguradoras. Ver a regra do trimestre abaixo |
| `emprestimos_curto_prazo` | BPP, contas `2.01.04.01` + `2.01.04.02`; se somam zero, conta-pai `2.01.04` menos `2.01.04.03` | Soma de empréstimos e debêntures a pagar em até 1 ano. A descrição precisa começar com "emprestimos e financiamentos" ou "debentures" (a conta-pai, só com "emprestimos e financiamentos"). O arrendamento (`2.01.04.03`) fica de fora. Sem pai nem subcontas, fica em branco. Em branco para bancos e seguradoras |
| `emprestimos_longo_prazo` | BPP, contas `2.02.01.01` + `2.02.01.02`; se somam zero, conta-pai `2.02.01` menos `2.02.01.03` | Igual a `emprestimos_curto_prazo`, para o passivo de longo prazo. O arrendamento (`2.02.01.03`) fica de fora. Em branco para bancos e seguradoras |
| `caixa` | BPA, conta `1.01.01` | A descrição precisa começar com "caixa e equivalentes". Linha com `DT_FIM_EXERC` igual à data de referência do documento. Em branco para bancos e seguradoras |
| `aplicacoes_financeiras` | BPA, conta `1.01.02` | A descrição precisa começar com "aplicacoes financeiras". Linha com `DT_FIM_EXERC` igual à data de referência do documento. Em branco para bancos e seguradoras |
| `situacao` | Regra da entrega | `ok` (padrão), `ajuste_ifrs17` (receita com ajuste de seguros) ou `reapresentado` (linha que usa o comparativo de uma remessa posterior). Se as duas se aplicam, vale `reapresentado` |

**Regras especiais:**

- Só entra a linha com `ORDEM_EXERC = ÚLTIMO`, salvo na reapresentação (abaixo). Desde 2026-11-03, o raw guarda também o `PENÚLTIMO` (comparativo do exercício anterior, reapresentado pela companhia) e as subcontas `3.01.NN`. O builder só lê o `PENÚLTIMO` na reapresentação.
- Para cada (CNPJ, tipo, `DT_REFER`), só a maior `VERSAO`.
- Valores com escala `MIL` são multiplicados por 1.000. A multiplicação usa `Decimal`.
- **Escala declarada errada.** Se a companhia declara `UNIDADE` num documento cuja magnitude fica 100x abaixo da mediana dos seus documentos em `MIL` (ou o contrário, com 100x acima), a escala do documento passa a ser a da maioria dos documentos do CNPJ. Cada correção sai no log. O raw não muda.
- **Posições** (`caixa`, `aplicacoes_financeiras`, `emprestimos_*`) são saldos na data de referência do documento. Como o patrimônio, são copiadas para todas as linhas de DRE do documento.
- **Empréstimos sem subconta.** CSN, Sabesp, Rede D'Or, Magalu, CMIN e outras lançam a dívida só na conta-pai (`2.01.04`, `2.02.01`), com as subcontas em 0. Por isso as subcontas valem quando somam mais que zero; senão, usa-se o pai menos o arrendamento (`.03`), tratado como 0 quando não existe. A regra não vale para aplicações financeiras, que zeradas continuam valendo 0. **Limitação da fonte:** se a empresa preenche só o pai e deixa o `.03` em 0, o valor pode incluir arrendamento, que a CVM não separa. As contas-pai só existem no `raw_cvm_balancos` depois de recoletar `cvm_itr_dfp`.
- **D&A no ITR.** A DFC do ITR é acumulada no ano. Na linha cujo período é igual ao de uma DFC (acumulado, primeiro trimestre ou DFP), a D&A é a dessa DFC. No trimestre seguinte, a D&A é a acumulada até o fim do período menos a acumulada até a véspera do início, ambas do mesmo CNPJ. Se o documento anterior não existir, ou a diferença sair negativa, a coluna fica em branco. Se houver mais de uma versão do documento anterior, vale a de maior `VERSAO`.
- **Bancos e seguradoras** usam outro plano de contas: o lucro fica em `3.11` ou `3.09` e o patrimônio em `2.08` ou `2.03`. A descrição não basta para separá-los (há bancos com "Caixa e Equivalentes de Caixa", e a seguradora PSSA3 usa o plano industrial). Por isso a lista explícita `CD_CVM_BANCOS_SEGURADORAS`, em `config.py`, diz quem são. Para eles, `lucro_bruto`, `ebit`, `depreciacao_amortizacao`, os empréstimos, `caixa` e `aplicacoes_financeiras` ficam em branco. Lucro líquido e patrimônio seguem a regra normal.
- **Receita IFRS 17 (2026-11-03).** Algumas companhias publicam a despesa de seguros dentro da conta `3.01`, como subconta `3.01.NN` negativa cuja descrição começa com "despesa" e contém "seguro". Nesse caso, `receita_liquida` subtrai essa subconta da `3.01` (como ela é negativa, o valor sobe) e `situacao` vira `ajuste_ifrs17`. Hoje só a HAPV3 cai nessa regra, em 1T25, 2T25, 1T26 e 2T26: o 1T25 passa de R$ 1,47 bi para R$ 7,80 bi, coerente com o 3T25 (R$ 7,92 bi) e com o anual. Para essa companhia, `receita_liquida` deixa de ser o valor publicado na `3.01`.
- **Reapresentação (2026-11-03).** Se a receita do documento `ÚLTIMO` vem zerada ou ausente, e uma remessa posterior traz o mesmo período como `PENÚLTIMO` com receita diferente de zero, a linha usa o comparativo da primeira remessa posterior. Nesse caso, `data_entrega` passa a ser a `DT_RECEB` dessa remessa, o que não gera vazamento, porque a linha só existe a partir dela. O documento original fica inválido: PL e posições saem em branco, e ele deixa de entrar na D&A acumulada. `situacao` vira `reapresentado`. Caso real: JBS N.V. 1T25, publicado com receita 0 em 2025-05-27 e reapresentado com R$ 114,1 bi em 2026-05-12.
- **SAUD3 (não corrigido).** No próprio dado da CVM, a receita da BRADSAÚDE salta de cerca de R$ 0,6 bi para cerca de R$ 8 bi por trimestre entre 1T26 e 2T26, por mudança de escopo da companhia. A entrega não corrige: a série fica como publicada.
- **Fora do escopo.** TIMS3 e BBSE3 publicam a conta `3.01` igual a 0 na DFP 2025. Não há tratamento para isso.
- **Emissor por período (2026-11-03).** Um balanço só entra se o CNPJ era o emissor do ticker na `data_entrega` (ver seção 12), e o ticker é sempre o atual. Saem da entrega os balanços da Natura Cosméticos de 2021 ao 1T25 e os da JBS N.V. antes da listagem; ficam no raw. Documentos da JBS S.A. após 2025-06-08 ficam sem ticker.

**Atualização.** Upsert pela chave.

## 7. Ações emitidas: `v1-acoes_emitidas`

Chave: (`cnpj`, `data_referencia`). Fonte: `raw_cvm_acoes`, da composição do capital da CVM.

| Coluna | Origem |
|---|---|
| `cnpj` | `CNPJ_CIA` |
| `data_referencia` | `DT_REFER` |
| `qtd_on`, `qtd_pn` | Quantidades ON e PN do registro, como vêm da CVM, exceto nas linhas corrigidas pela escala ou por erro de dígito (ver abaixo) |
| `data_entrega` | `DT_RECEB`, data em que o documento foi recebido pela CVM. Mostra quando o dado chegou |
| `qtd_on_base_atual`, `qtd_pn_base_atual` | Quantidades convertidas para a base atual (ver abaixo). Vazias quando a linha foi anulada |
| `situacao` | `ok`, `corrigido_escala`, `corrigido_digito`, `anulado` ou `suspeito` |

Quando há mais de um registro para a mesma data, vence o DFP sobre o ITR; em empate de tipo, vence a maior `VERSAO`.

**Escala e erros da fonte.** A CVM não tem coluna de escala: cerca de 30% das linhas vêm em milhares e o resto em unidades. Algumas empresas estão sempre em milhares (VALE3) e outras alternam trimestre a trimestre (PSSA3). A correção é feita só na entrega, por CNPJ, em três camadas. O raw não muda, e cada linha corrigida ou suspeita sai com `situacao` diferente de `ok`.

1. **Escala (`corrigido_escala`).** A série é quebrada onde o total salta ~1000x, e um segmento está em milhares se a mediana do giro passar de 10% ao dia. O giro é a mediana do volume dos últimos 60 pregões (ON, PN e unit somados) dividida pelo total de ações, e é impossível passar de 10% em unidades. Segmento sem giro (ex.: trimestres de antes da listagem) herda pela cadeia: a escala alterna a cada troca de 1000x a partir do segmento decidido pelo giro mais próximo. CNPJ sem nenhum volume não tem como ser decidido e fica como a CVM publica: a STBP3, sem preço no Yahoo, segue em milhares (851.018 a 864.170).
2. **Erro de dígito (`corrigido_digito`, automático).** Valor isolado cuja razão com os dois vizinhos é ~10^k (k = ±1 ou ±2, tolerância de 3%), com os vizinhos concordando (até 5%), é dividido por 10^k. Casos: CMIN3 em 2025-12 e GMAT3 em 2022-06. A última linha da série com esse padrão não tem vizinho futuro: só é marcada `suspeito` e não é corrigida. Usar o vizinho futuro é aceito: corrige digitação e não traz informação de retorno. É o mesmo critério da escala dos balanços.
3. **Anulação (`anulado`, lista explícita).** `ACOES_CVM_ANULADAS`, em `config.py`, lista os documentos anulados, versionada. A quantidade fica NULL. Casos: GMAT3 em 2023-06; SRNA3 em 2021-06 e 2021-09; IGTI11 de 2021-03 a 2021-09; AUAU3 de 2023-12 a 2024-12. Os casos de IGTI11 e AUAU3 são hipótese: a base é anterior à reorganização ou à listagem, e o valor pode ser real.

Valor que não se encaixa em nenhuma camada não é alterado. O caso de AZUL3 em 2026-03-31 não é erro: a data é anterior ao grupamento 150.000:1 de 2026-04-17, e as 54,7 trilhões de ações viram ~364,9 milhões na base atual, coerente com 2026-06 (368,6 milhões).

**Base atual (`qtd_on_base_atual`, `qtd_pn_base_atual`).** É a quantidade na mesma base do fechamento ajustado por eventos (Yahoo) e de `valor_por_acao` dos proventos. Aplicam-se os eventos de `v1-eventos_acoes` da própria classe com data-com maior ou igual à data de referência: a quantidade é multiplicada pelo `fator` de cada um. A conversão **não é point-in-time**: usa os eventos que existem quando a tabela é montada, inclusive os posteriores ao documento. Quem precisar da quantidade como a CVM publicou usa `qtd_on` e `qtd_pn`.

**Eventos já refletidos no documento.** Um documento entregue depois de um evento pode já trazer a quantidade pós-evento. O DFP de IRBR3 de 2022-12, entregue em 2023-03-21 (`data_entrega`), já vem na base do grupamento 30:1 de 2023-01-24. Por isso, um evento com data-com entre a data de referência e a data de entrega só é aplicado se a quantidade ainda não o refletir: compara-se com a linha anterior, e, se a razão já for o fator do evento (tolerância de 5%), o evento não entra de novo. Essa conversão é feita pelo pipeline. Quem recebesse só os eventos aplicaria o grupamento duas vezes.

**Emissor por período e fator (2026-11-03).** Um documento só entra se o CNPJ era o emissor do ticker na `data_entrega` (ver seção 12). Na base atual, a quantidade é multiplicada também pelo `fator_acoes` do emissor, que é a quantidade de ações do ticker atual por ação do emissor: JBSS32 usa 0,5, porque 1 BDR corresponde a 2 ações JBSS3. Assim, base atual × fator do emissor.

**Atualização.** Upsert pela chave.

## 8. Proventos: `v1-proventos`

Chave: (`ticker`, `tipo`, `data_com`). Fontes: `raw_b3_proventos` (dividendos e JCP da B3), `raw_b3_suplemento` (eventos em dinheiro e em ações) e `raw_b3_cadastro` (códigos ISIN).

| Coluna | Regra |
|---|---|
| `ticker` | Código da empresa (`issuingCompany`) com sufixo pelo tipo de ação: ON `3`, PN `4`, PNA `5`, PNB `6`, UNT/UNIT `11`. Units recebem o ticker da unit (ex.: `KLBN11`) |
| `tipo` | `corporateAction` `DIVIDENDO` vira `DIVIDENDO`; `JRS CAP PROPRIO` vira `JCP`. Os demais rótulos são pulados, com aviso no log |
| `data_com` | `lastDatePrior` (última data com direito) |
| `valor_por_acao` | `valueCash` ÷ `quotedPerShares`, multiplicado pelos fatores de ajuste (abaixo) |
| `data_pagamento` | `paymentDate` do evento em dinheiro do suplemento que casa com o provento: mesmo ticker (pelo ISIN), mesma `lastDatePrior`, mesmo rótulo e taxa igual ao valor bruto (tolerância de 0,000001). Vazio se não houver casamento |

**Ajuste por evento em ações.** Só valem os eventos com `lastDatePrior` posterior à `data_com`:

| Rótulo | Fator aplicado ao valor antigo |
|---|---|
| `DESDOBRAMENTO` ou `BONIFICACAO` | ÷ (1 + `factor` / 100). O `factor` é o percentual de ações novas: 100 é dobrar; 33,33 é 1 para 3 |
| `GRUPAMENTO` | × (1 / `factor`). O `factor` é a razão: 0,01 é 100 para 1 |

Os fatores se multiplicam. Exemplo de conferência: o desdobramento 1:2 de 2008 da PETR4 tem `factor` = 100 na fixture `b3_supplement_PETR.json`.

**Colisão na chave.** Dois proventos com o mesmo ticker, tipo e `data_com` têm os valores somados. A `data_pagamento` é a primeira não vazia. Os valores originais ficam em `raw_b3_proventos`.

**Units.** A unit é a soma por composição: `quantidade ON × valor ON + quantidade PN × valor PN`, com as quantidades de `UNITS`. A `data_pagamento` vem do evento da ON na data; se não houver evento de ON nessa data, vem do evento da PN.

**Emissor por período e BDR (2026-11-03).** O `ticker` é sempre o atual. Proventos de JBSS3 saem como `JBSS32`, com `valor_por_acao` ÷ 0,5 (1 BDR = 2 ações); proventos de NTCO3 saem como `NATU3`. O coletor de proventos da B3 ganha as unidades `prov|JBSS|20575` e `prov|NTCO|24783`, para os códigos antigos. Proventos de um BDR que só existe no suplemento da B3 entram. A janela de cada emissor é decidida pela data-com (seção 12).

**Complemento do Yahoo: JBS S.A. 2020 a 2025 (2026-11-03).** Os dividendos do Yahoo de JBSS3 (`data/proventos_yahoo/JBSS32.csv`, ver seção 3) entram como `JBSS32`, `tipo` = `DIVIDENDO`, com a mesma conversão (÷ 0,5) e o mesmo filtro de emissor por data. A `data_pagamento` fica vazia. A B3 vence onde já tem a mesma data-com e o tipo, sem duplicar. O Yahoo não distingue JCP de dividendo, então todo evento entra como `DIVIDENDO`.

**Atualização.** Upsert pela chave.

## 9. Índices: `v1-indices`

Chave: (`data`, `indice`).

| Índice | Fonte | Valor |
|---|---|---|
| `CDI` | BCB SGS série 12 | Como vem: percentual ao dia |
| `IPCA` | BCB SGS série 433 | Como vem: percentual no mês. A data é a gravada em `raw_macro` |
| `IDIV`, `SMLL`, `IFIX` | B3 (`GetPortfolioDay`), gravados em `raw_prices` com `source='b3'` | Fechamento (`close`), sem ajuste |

O IBOV não entra aqui. Vai para `v1-precos`, como `IBOV`, e a regra da coluna `abertura` dele está na seção 3.

**Atualização.** Upsert pela chave.

## 10. Composição do IBOV: `v1-composicao_ibov`

Uma linha por período contínuo em que o papel esteve no IBOV. `vigencia_fim` NULL quer dizer vigente. `participacao` é o peso (%) no início da vigência, e fica NULL quando a fonte não traz peso. `fonte` diz de onde veio o período: `paccafelipe_csv`, `rebalanceamento` ou `b3_api`.

Montagem (`entrega/composicao_ibov.py`, e `montar_composicao` em `builder.py`):

1. **Carteiras oficiais, jan/2021 a set/2024.** Doze CSVs em `data/composicao_ibov/`, com origem e atribuição em `LEIAME.md`. O início de vigência é o primeiro dia útil do mês (segunda a sexta, sem feriados), salvo as cinco datas conhecidas (2023-05-02, 2023-09-04, 2024-01-02, 2024-05-06 e 2024-09-02).
2. **Rebalanceamentos e eventos extraordinários depois de set/2024** (`REBALANCEAMENTOS_IBOV` e `EVENTOS_IBOV_EXTRAORDINARIOS`, em `config.py`). Cada evento tem a data do primeiro pregão em que a mudança vale; a fonte (comunicado da B3 ou da companhia, conferido na imprensa em 2026-10-23) fica em comentário. Os eventos extraordinários são os que não são rebalanceamento: fusões, cisões, recuperações judiciais e extrajudiciais, OPAs, deslistagens, conversão de BDR e trocas de código com mais de um papel.
3. **Trocas de código de `MUDANCAS_TICKER`** entram na mesma linha do tempo, mas só valem se o código antigo está na carteira naquela data e se nenhum evento explícito já trata o par (o antigo sai ou o novo entra no evento). Por isso JBSS3 → JBSS32, AZUL4 → AZUL3, PETZ3 → AUAU3 e as trocas já cobertas por eventos (ELET3/ELET6 → AXIA3, CPLE6 → CPLE3) não adicionam o código novo uma segunda vez. A regra está em `eventos_trocas` (`entrega/composicao_ibov.py`).
4. **Vigências.** Cada papel gera uma linha por período contínuo. O fim é a véspera da primeira carteira sem o papel.
5. **Fechamento gravado pelo coletor prevalece** sobre a reconstrução, porque veio da carteira publicada pela B3.

Eventos extraordinários (data = primeiro pregão com a mudança):

| Data | Entram | Saem | Motivo |
|---|---|---|---|
| 2024-09-09 | BRAV3 | RRRP3 | 3R vira Brava (troca de código) |
| 2024-11-18 | ISAE4 | TRPL4 | ISA CTEEP vira ISA Energia (troca de código) |
| 2024-12-16 | AMOB3 | — | Cisão da Vamos (inclusão automática) |
| 2025-05-30 | — | AZUL4 | Recuperação judicial (exclusão no fechamento de 29/05) |
| 2025-06-02 | — | CRFB3 | Deslistagem (último pregão 30/05) |
| 2025-06-09 | — | JBSS3 | Vira BDR JBSS32, que não entra no índice |
| 2025-09-23 | MBRF3 | BRFS3, MRFG3 | Fusão BRF + Marfrig |
| 2025-10-06 | — | STBP3 | OPA e deslistagem (último pregão 03/10) |
| 2025-11-10 | AXIA3, AXIA6, CPLE5 | ELET3, ELET6, CPLE6 | Troca de código da Eletrobras e da Copel |
| 2025-12-22 | CPLE3 | CPLE5 | Migração da Copel ao Novo Mercado |
| 2026-03-12 | — | PCAR3 | Recuperação extrajudicial (último pregão 11/03) |
| 2026-03-13 | — | RAIZ4 | Último pregão 12/03 |
| 2026-06-08 | — | AXIA6 | Convertida em AXIA3, que já está. AXIA5 também foi convertida, mas nunca esteve na carteira |
| 2026-08-26 | — | BRKM5 | Recuperação extrajudicial (exclusão no fechamento de 25/08) |

ELET5 → AXIA5 não se aplica: ELET5 não estava na base de set/2024.

Conferência: a reconstrução chega a 76 papéis em 2026-10-23, o mesmo conjunto da carteira da API da B3 (`tests/fixtures/b3_carteira_ibov_2026.json`).

Coletor diário (`collectors/b3_carteira_ibov.py`, etapa `precos`): baixa a carteira do IBOV (`GetPortfolioDay`, 76 papéis em 2026-10-23) e compara com as vigências abertas. Se diferirem, grava as entradas com a data do header e fecha as saídas no dia anterior, com `fonte = b3_api`. Sem vigência aberta (antes do primeiro `build-entrega`), não grava nada. Não tem checkpoint: é um retrato do dia.

Trocas de código intermediárias não estão na tabela (ELET6 → AXIA6, CPLE6 → CPLE5, AZUL4 → AZUL54/AZUL53). O código antigo aparece até a data da troca final.

## 11. Eventos de ações: `v1-eventos_acoes`

Chave: (`ticker`, `data_com`, `tipo`, `isin_emitido`). Fonte: `v1-raw_b3_suplemento`, os eventos em ações. Tabela criada pela alteração de 2026-11-02 (`sql/alteracoes/2026-11-02_acoes_base_atual_eventos.mysql.sql`). Foi acrescentada à entrega pelo adendo de 2026-11-02 da spec.

| Coluna | Origem e regra |
|---|---|
| `ticker` | Ação afetada |
| `data_com` | Data-com do evento |
| `tipo` | `DESDOBRAMENTO`, `BONIFICACAO` ou `GRUPAMENTO` |
| `fator` | Ações depois do evento por ação antes. Desdobramento 1:2 = 2,0; grupamento 30:1 = 0,0333; bonificação de 20% = 1,2 |
| `isin` | ISIN da ação antes do evento |
| `isin_emitido` | ISIN da classe que recebe as ações novas. Difere de `isin` quando a bonificação é paga em outra classe (RENT, 2025-12). Nesse caso a quantidade da classe de `isin` não muda |
| `cnpj` | CNPJ da companhia |
| `data_aprovacao` | Data de aprovação do evento, como vem no suplemento da B3 |

`fator` multiplica a quantidade anterior. Por isso, a seção 7 multiplica a quantidade de uma data de referência pelos fatores dos eventos da própria classe com data-com maior ou igual à data de referência, para montar `qtd_on_base_atual` e `qtd_pn_base_atual`.

**Fica de fora:**

- **Incorporação** (ex.: JBSS em 2025, NATU em 2019), **cisão** (CIS RED CAP), **resgate** (RESG TOTAL RV) e **redução ou restituição de capital**. O sentido do fator desses eventos não foi verificado, e eles não mudam a contagem da própria classe.
- **Troca de código** de ação. Aparece em `v1-mudancas_ticker`, não aqui.

**O que se perde:**

- CNPJ antigo que não está no cadastro atual da B3 não tem eventos: a base atual fica igual à original.
- Bonificação paga em outra classe não ajusta a quantidade da classe de `isin`.
- CNPJ com mais de uma classe PN com fatores conflitantes fica com `qtd_pn_base_atual` NULL.

**Atualização.** Upsert pela chave.

## 12. Emissores por período: `v1-emissores_ticker`

Chave: (`ticker`, `ordem`). Fonte: `EMISSORES_POR_PERIODO` em `config.py`, que diz qual companhia (código CVM e CNPJ) era o emissor de cada ticker em cada intervalo de datas. Ticker sem entrada ali tem um único emissor, o de `TICKER_CODE_CVM`, em período aberto. Tabela criada pela alteração de 2026-11-03 (`sql/alteracoes/2026-11-03_emissores_ticker.mysql.sql`) e gravada pela etapa `entrega`. Foi acrescentada à entrega pelo adendo de 2026-11-03 da spec, que traz o dicionário de `emissores_ticker.csv`.

| Coluna | Regra |
|---|---|
| `ticker` | Ticker atual, o mesmo que aparece nas outras tabelas da entrega |
| `ordem` | Número do período dentro do ticker |
| `ticker_negociado` | Código que a B3 negociava naquele período (ex.: `JBSS3`, `NTCO3`) |
| `cd_cvm`, `cnpj` | Código CVM e CNPJ da companhia emissora |
| `empresa` | Nome da companhia emissora |
| `vigencia_inicio`, `vigencia_fim` | Datas do período, inclusivas. `vigencia_fim` vazio quer dizer vigente |
| `fator_acoes` | Ações do ticker atual por ação do emissor. JBSS32 em JBSS3: 0,5 (1 BDR = 2 ações). Natura &Co em NTCO3: 1,0 (troca 1:1) |

Os dois tickers com mais de um emissor:

- `JBSS32`: JBS S.A. (CVM 20575, negociada como JBSS3 até 2025-06-08, fator 0,5); JBS N.V. (CVM 80233, desde 2025-06-09).
- `NATU3`: Natura Cosméticos (CVM 19550) até 2019-12-17; Natura &Co Holding (CVM 24783, negociada como NTCO3 de 2019-12-18 a 2025-07-01); Natura Cosméticos de novo desde 2025-07-02.

**Regra de emissor por período.** Qual emissor vale é decidido pela **data de entrega à CVM**, não pela data de referência: `dt_receb` nos balanços e nas ações, `data_entrega` nos documentos do IPE e a data-com nos proventos. Pela data de referência, o 2T25 da Natura não teria dono, porque o trimestre termina em 2025-06-30 e a Natura Cosméticos só volta a ser a emissora em 2025-07-02. A `data_entrega` é o que já se sabia na época, então não há vazamento temporal.

- **Ticker sempre o atual.** As linhas da entrega saem com `NATU3` ou `JBSS32`, mesmo quando o documento foi publicado sob o código antigo.
- **Balanços** (seção 6) e **ações emitidas** (seção 7) só entram se o CNPJ era o emissor na data de entrega. **Proventos** (seção 8) são convertidos para o ticker atual. **Notícias do IPE** (seção 2.3) só ficam no ticker se o emissor valia na data.
- **Gravação.** A etapa `entrega` apaga as linhas obsoletas de `v1-balancos`, `v1-acoes_emitidas`, `v1-proventos` e `v1-emissores_ticker`, mas só quando a montagem produz linhas para a tabela. Montagem vazia não apaga nada.

**Preço.** O histórico de `JBSS32` e `NATU3` em `v1-precos` vem das emendas de troca de código da COTAHIST (seção 3). A alta falsa de +100% do JBSS32 em 2025-06-09 e os quatro dias de NATU3 a R$ 36,86 saem corrigidos.

**O que se perde:**

- Balanços da Natura Cosméticos de 2021 ao 1T25 e da JBS N.V. antes da listagem saem da entrega. Ficam no raw.
- Documentos da JBS S.A. após 2025-06-08 ficam sem ticker.
- `volume_relativo` do JBSS32 tem quebra de regime: o BDR negocia cerca de 1% do volume da JBSS3, então a série não é comparável com a de antes de 2025-06-09.
- Risco: a B3 pode não devolver o histórico de NTCO3, que foi cancelada. Sem ele, a emenda de NATU3 não pode ser feita pela COTAHIST.

## 13. Como gerar as tabelas e os CSVs

```bash
python -m tcc_scraper.cli build-entrega                             # grava as 11 tabelas no banco
python -m tcc_scraper.cli build-entrega --csv                       # grava no banco e exporta CSV
python -m tcc_scraper.cli build-entrega --tabela noticias --csv     # só uma tabela; --tabela é repetível
```

- Os CSVs vão para `ENTREGA_CSV_DIR` (padrão `exports/entrega/`), um arquivo por tabela: `<tabela>.csv`.
- Formato: UTF-8 sem BOM, separador vírgula, campo vazio para valor nulo, datas em ISO e números sem notação científica.
- Nomes de tabela com hífen exigem crase em SQL:

```sql
SELECT ticker, data_publicacao, titulo
  FROM `v1-noticias`
 WHERE momento_pregao = 'durante'
 LIMIT 10;
```

## 14. Lacunas e pendências

- **Composição do IBOV não validada contra a B3.** A reconstrução de set/2024 até hoje dá 84 papéis, e a carteira da API tem 76. As divergências e as contagens citadas em B estão em [09-historico-de-mudancas.md](09-historico-de-mudancas.md), seção 14. A decisão é do dono.
- **Filtro da juíza desligado por padrão.** Com `ENTREGA_FILTRO_JUIZA=false`, `v1-noticias` leva panoramas e opiniões. O filtro ligado é a regra de qualidade pretendida (spec, seção 5.1), e a decisão de ligá-lo é do dono.
- **`qualidade` é número gerado pela LLM.** A coluna vem da nota de 0 a 1 da juíza. O `CLAUDE.md` diz que a LLM nunca gera número que vá para tabela entregue. Esse ponto precisa de decisão do dono: manter a coluna com essa ressalva ou tirá-la da entrega.
- **Regra de atualização de `noticias` diferente da spec.** A spec diz que a regra diária só acrescenta e não reescreve `prob_*`. O código regrava todas as colunas que não são chave. A remoção de reprovadas é a única exceção.
- **`--reprocessar` não existe.** A spec (seção 5.1) cita a opção para refazer o FinBERT ou a juíza quando a versão muda. O comando `build-entrega` tem só `--csv` e `--tabela`.
- **`origem = 'rss_nativo'` não é gravado.** Não há coletor de RSS nativo (a constante `NATIVE_FEEDS`, sem uso, foi removida em 2026-10-20). Hoje a tabela tem `google_news`, `wordpress` (InfoMoney, Money Times e Exame) e `cvm_ipe`.
- **CRFB3 sem código CVM.** Não há código CVM em `TICKER_CODE_CVM`. A linha do CRFB3 em `v1-cadastro` sai com `cnpj` e setor vazios. Está registrado como caso de teste pendente (`xfail` em `tests/test_config.py`).
- **Proventos de outros rótulos.** Rendimentos, restituições de capital e outros rótulos da B3 não entram. A spec previa listá-los no spike antes de decidir.
- **Fator de ajuste e data de pagamento.** A conferência dos fatores em cinco empresas e da data de pagamento está listada como spike na spec, seção 5.7, e não foi documentada como concluída.
