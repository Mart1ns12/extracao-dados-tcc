# Entrega: as 8 tabelas para a etapa seguinte

Este documento cobre as 8 tabelas entregues (`v1-noticias` a `v1-indices`): a origem de cada coluna, a chave, a regra de atualização e as regras especiais. A especificação do que foi pedido está em [spec-entrega-etapa-seguinte.md](spec-entrega-etapa-seguinte.md), e o desenho detalhado em [superpowers/specs/2026-10-19-entrega-etapa-seguinte-design.md](superpowers/specs/2026-10-19-entrega-etapa-seguinte-design.md). Este texto descreve o código atual.

## 1. Visão geral

| Tabela | Chave | Fonte principal |
|---|---|---|
| `v1-noticias` | (`id`, `ticker`) | `raw_articles`, juíza, FinBERT, IPE |
| `v1-precos` | (`data`, `ticker`) | `raw_prices` (Yahoo) |
| `v1-cadastro` | (`ticker`) | `raw_b3_cadastro`, `raw_cvm_cadastro` |
| `v1-mudancas_ticker` | (`ticker_antigo`) | `MUDANCAS_TICKER` em `config.py` |
| `v1-balancos` | (`cnpj`, `tipo_documento`, `data_inicio_periodo`, `data_fim_periodo`) | `raw_cvm_balancos` |
| `v1-acoes_emitidas` | (`cnpj`, `data_referencia`) | `raw_cvm_acoes` |
| `v1-proventos` | (`ticker`, `tipo`, `data_com`) | `raw_b3_proventos`, `raw_b3_suplemento`, `raw_b3_cadastro` |
| `v1-indices` | (`data`, `indice`) | `raw_prices` (Yahoo) e `raw_macro` (BCB) |

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
| `data_hora_publicacao` | `raw_articles.published_at` | ISO 8601 no fuso de São Paulo, com offset. **Vazia** quando `published_precision = 'date'` (documentos do IPE) |
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
| `origem` | `google_news` (padrão), `cvm_ipe` | `raw_articles.origem` |
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
- **Julgamento por ticker.** Cada par leva o julgamento do próprio ticker, na versão mais alta do prompt da juíza. Tickers adicionais herdam o julgamento do ticker principal do artigo.
- **Filtro da juíza.** Controlado por `ENTREGA_FILTRO_JUIZA`, com padrão `false`.
  - Com o filtro **desligado**, entra todo par com vínculo, inclusive panoramas e opiniões. Os campos da juíza saem vazios quando não há julgamento.
  - Com o filtro **ligado**, documentos do IPE entram sempre. As demais notícias só entram se `tipo_conteudo = reportagem` e `papel` for `protagonista` ou `citada_com_fato`. Artigo sem julgamento fica de fora.
- **Remoção de reprovadas.** Com o filtro ligado, a montagem apaga da tabela as linhas que a juíza reprovou. Um artigo da janela que tem julgamento na versão vigente, mas nenhuma linha aprovada, sai pelo `id`. Um par que deixou de valer sai pela chave composta. Com o filtro desligado, saem só os pares obsoletos. Artigo sem julgamento na versão vigente fica como está.

## 3. Preços: `v1-precos`

Chave: (`data`, `ticker`). Fonte: `raw_prices`, do Yahoo.

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
- `data_mudanca` está **vazia nas 10 linhas**. As datas dependem do levantamento nos avisos da B3, ainda não feito (ver a seção 7).

## 6. Balanços: `v1-balancos`

Chave: (`cnpj`, `tipo_documento`, `data_inicio_periodo`, `data_fim_periodo`). Fonte: `raw_cvm_balancos`, dos arquivos de DFP e ITR da CVM (dados abertos), nas demonstrações DRE e BPP.

| Coluna | Origem | Regra |
|---|---|---|
| `cnpj`, `tipo_documento` | `CNPJ_CIA`, tipo do documento (DFP ou ITR) | Vêm do registro |
| `data_inicio_periodo`, `data_fim_periodo` | `DT_INI_EXERC`, `DT_FIM_EXERC` da DRE | No ITR, o trimestre e o acumulado do ano são linhas diferentes, e as duas são entregues |
| `data_entrega` | `DT_RECEB` da DRE do período | |
| `receita_liquida` | DRE, conta `3.01` | |
| `lucro_bruto` | DRE, conta `3.03` | A descrição precisa começar com "resultado bruto" e não pode conter "intermediacao" nem "operacoes" |
| `lucro_liquido` | DRE, conta `3.11`, senão `3.09` | A descrição precisa começar com "lucro/prejuizo consolidado do periodo" ou "lucro/prejuizo do periodo" |
| `patrimonio_liquido` | BPP, conta `2.08`, senão `2.03` | Linha com `DT_FIM_EXERC` igual à data da DRE. A descrição precisa começar com "patrimonio liquido" |

**Regras especiais:**

- Só entra a linha com `ORDEM_EXERC = ÚLTIMO`.
- Para cada (CNPJ, tipo, `DT_REFER`), só a maior `VERSAO`.
- Valores com escala `MIL` são multiplicados por 1.000. A multiplicação usa `Decimal`.
- **Bancos** não têm regra à parte. Eles usam outro plano de contas: o lucro fica em `3.11` ou `3.09` e o patrimônio em `2.08` ou `2.03`. Por isso a função tenta primeiro o código de número maior e confere a descrição.

**Atualização.** Upsert pela chave.

## 7. Ações emitidas: `v1-acoes_emitidas`

Chave: (`cnpj`, `data_referencia`). Fonte: `raw_cvm_acoes`, da composição do capital da CVM.

| Coluna | Origem |
|---|---|
| `cnpj` | `CNPJ_CIA` |
| `data_referencia` | `DT_REFER` |
| `qtd_on`, `qtd_pn` | Quantidades ON e PN do registro, como vêm da CVM |

Quando há mais de um registro para a mesma data, vence o DFP sobre o ITR; em empate de tipo, vence a maior `VERSAO`.

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

**Atualização.** Upsert pela chave.

## 9. Índices: `v1-indices`

Chave: (`data`, `indice`).

| Índice | Fonte | Valor |
|---|---|---|
| `CDI` | BCB SGS série 12 | Como vem: percentual ao dia |
| `IPCA` | BCB SGS série 433 | Como vem: percentual no mês. A data é a gravada em `raw_macro` |
| `IDIV`, `SMLL`, `IFIX` | Yahoo (`IDIV.SA`, `SMLL.SA`, `IFIX.SA`) | Fechamento (`close`), sem ajuste |

O IBOV não entra aqui. Vai para `v1-precos`, como `IBOV`.

**Atualização.** Upsert pela chave.

## 10. Como gerar as tabelas e os CSVs

```bash
python -m tcc_scraper.cli build-entrega                             # grava as 8 tabelas no banco
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

## 11. Lacunas e pendências

- **Filtro da juíza desligado por padrão.** Com `ENTREGA_FILTRO_JUIZA=false`, `v1-noticias` leva panoramas e opiniões. O filtro ligado é a regra de qualidade pretendida (spec, seção 5.1), e a decisão de ligá-lo é do dono.
- **`qualidade` é número gerado pela LLM.** A coluna vem da nota de 0 a 1 da juíza. O `CLAUDE.md` diz que a LLM nunca gera número que vá para tabela entregue. Esse ponto precisa de decisão do dono: manter a coluna com essa ressalva ou tirá-la da entrega.
- **Regra de atualização de `noticias` diferente da spec.** A spec diz que a regra diária só acrescenta e não reescreve `prob_*`. O código regrava todas as colunas que não são chave. A remoção de reprovadas é a única exceção.
- **`--reprocessar` não existe.** A spec (seção 5.1) cita a opção para refazer o FinBERT ou a juíza quando a versão muda. O comando `build-entrega` tem só `--csv` e `--tabela`.
- **`origem = 'rss_nativo'` não é gravado.** Nenhum coletor usa `NATIVE_FEEDS`. Hoje a tabela só tem `google_news` e `cvm_ipe`.
- **`data_mudanca` vazia em todas as mudanças de ticker.** Depende do levantamento nos avisos da B3.
- **CRFB3 sem código CVM.** Não há código CVM em `TICKER_CODE_CVM`. A linha do CRFB3 em `v1-cadastro` sai com `cnpj` e setor vazios. Está registrado como caso de teste pendente (`xfail` em `tests/test_config.py`).
- **Proventos de outros rótulos.** Rendimentos, restituições de capital e outros rótulos da B3 não entram. A spec previa listá-los no spike antes de decidir.
- **Fator de ajuste e data de pagamento.** A conferência dos fatores em cinco empresas e da data de pagamento está listada como spike na spec, seção 5.7, e não foi documentada como concluída.
