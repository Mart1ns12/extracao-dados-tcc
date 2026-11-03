# Spec de entrega para a etapa seguinte do TCC (recebida em 2026-10-19)

Texto recebido do dono do projeto, transcrito sem alteração de conteúdo. É o **contrato**
que `docs/superpowers/specs/2026-10-19-entrega-etapa-seguinte-design.md` implementa.
Qualquer mudança aqui exige nova versão do desenho.

---

## Parte 1: descrição das 8 entregas

**Para montar o dataset do modelo, a coleta precisa mandar 4 coisas:**

### 1. Notícias
Arquivo: `noticias.csv`. Cada notícia com: código único da notícia (id); data e hora da
coleta; data e hora de publicação (com fuso horário); empresa (ticker); título; fonte e link;
resultado do FinBERT: probabilidade de positivo, neutro e negativo.
Colunas: `id, data_hora_publicacao, data_hora_coleta, ticker, titulo, fonte, link,
prob_positivo, prob_neutro, prob_negativo`

### 2. Preços diários
Arquivo: `precos.csv`. De cada ação, do Ibovespa, do dólar (USD/BRL), do petróleo Brent e do
IVVB11: abertura; máxima; mínima; fechamento (ajustado só por desdobramentos e
bonificações); fechamento ajustado (ajustado também por dividendos); volume.
Colunas: `data, ticker, abertura, maxima, minima, fechamento, fechamento_ajustado, volume`
(Ibovespa, dólar, Brent e IVVB11 entram como tickers: IBOV, USDBRL, BRENT, IVVB11).
Para IBOV, USDBRL e BRENT: fechamento ajustado = fechamento; volume em branco quando não existir.

### 3. Cadastro das ações
Arquivo: `cadastro.csv`. ticker, CNPJ e nome da empresa; setor e subsetor (classificação da
B3); tipo da ação: ON, PN ou UNIT; nas units: quantas ações ON e quantas PN formam cada unit
(ex.: KLBN11 = 1 ON + 4 PN).
Colunas: `ticker, cnpj, empresa, setor, subsetor, tipo_acao, unit_qtd_on, unit_qtd_pn`

### 4. Mudanças de ticker
Arquivo: `mudancas_ticker.csv`. Código antigo, código novo e data da mudança
(ex.: ELET3 → AXIA3). Colunas: `ticker_antigo, ticker_novo, data_mudanca`

### Período
- Notícias: o mesmo período do dataset, mais 1 mês antes (para calcular a mudança de tom).
- Preços: o período do dataset mais 1 ano antes. O beta (252 pregões) e a distância da
  máxima e da mínima de 52 semanas precisam de 1 ano de história. Sem esse ano extra, os
  primeiros meses do dataset ficam sem esses valores.
- Ideal: 5 anos de dataset, que é o que deixa os resultados conclusivos.

**Para o cálculo dos outros fatores:**

### 5. Balanços das empresas (ITR e DFP, da CVM)
Arquivo: `balancos.csv`. CNPJ da empresa; tipo do documento (ITR ou DFP); data de início e
de fim do período de cada valor (para saber se é do trimestre ou acumulado no ano); data de
entrega de cada balanço; receita líquida; lucro bruto; EBIT; depreciação e amortização do período; empréstimos e
debêntures de curto e de longo prazo (sem arrendamento); caixa e aplicações financeiras
(em bancos e seguradoras, lucro bruto, EBIT, depreciação, empréstimos, caixa e aplicações
ficam em branco); lucro líquido; patrimônio líquido; sempre o balanço consolidado; período:
pelo menos 2 anos (ideal: 5 anos, igual aos proventos, para o gráfico de payout).
Colunas: `cnpj, tipo_documento, data_inicio_periodo, data_fim_periodo, data_entrega,
receita_liquida, lucro_bruto, lucro_liquido, patrimonio_liquido, ebit,
depreciacao_amortizacao, emprestimos_curto_prazo, emprestimos_longo_prazo, caixa,
aplicacoes_financeiras`

### 6. Quantidade de ações
Arquivo: `acoes_emitidas.csv`. CNPJ da empresa, data de referência, número de ações ON e
número de ações PN. Colunas: `cnpj, data_referencia, qtd_on, qtd_pn`

### 7. Proventos (B3)
Arquivo: `proventos.csv`. Código da ação; tipo (dividendo ou JCP); valor por ação, já
ajustado por desdobramentos e bonificações; data-com; data de pagamento; período: pelo menos
5 anos. Colunas: `ticker, tipo, valor_por_acao, data_com, data_pagamento`

### 8. Índices
Arquivo: `indices.csv`. CDI diário e IPCA mensal (Banco Central); IDIV, SMLL e IFIX diários
(B3). CDI e IPCA vêm como taxa em % (ex.: CDI diário 0.0551 = 0,0551% ao dia; IPCA 0.26 =
0,26% no mês); IDIV, SMLL e IFIX vêm em pontos. No IPCA, a data é o dia 1 do mês
(ex.: 2026-06-01). Período: o mesmo dos preços. Colunas: `data, indice, valor`

### Regras para combinar os módulos
- Mesmo código de ação em todos (ex.: PETR4, sem sufixos como .SA).
- Datas no formato AAAA-MM-DD, e hora com fuso nas notícias.
- Ponto como separador decimal e codificação UTF-8.
- Valores vazios em branco, sem 0 no lugar de "não tem".
- Nomes das colunas exatamente como listados acima.
- Valores dos balanços em reais, sem escala (a CVM costuma publicar em milhares; converter para reais).
- Unidades: preços das ações em reais; USDBRL em reais por dólar; BRENT em dólares por barril;
  volume em quantidade de ações negociadas (não em reais).
- CNPJ só com números (ex.: 33000167000101).

### Quais ações e como entregar
- Ações a coletar: todas as ações que fizeram parte do Ibovespa em algum momento do período
  coletado (não só as de hoje), para evitar o viés de sobrevivência.
- Entrega (banco de dados): cada arquivo vira uma tabela no banco, com os mesmos nomes de
  colunas. Histórico: carga inicial completa das 8 tabelas. Atualização diária: todo dia de
  pregão, até as 20h (horário de Brasília).
- Como atualizar cada tabela: notícias, índices, mudanças de ticker e quantidade de ações:
  acrescentar as linhas novas. Preços: todo dia, conferir o fechamento ajustado do pregão
  anterior de cada ação; se for diferente do que está no banco (houve dividendo,
  desdobramento ou bonificação), substituir o histórico completo só daquela ação; senão, só
  acrescentar o dia novo. Proventos, balanços e cadastro: acrescentar os novos e corrigir os
  que mudarem.
- Linhas nunca podem se repetir: usar "upsert" (se a linha já existe, mesma chave, atualiza;
  se não existe, cria). Chaves: noticias (id + ticker); precos (data + ticker); indices
  (data + indice); proventos (ticker + data_com + tipo); balancos (cnpj + tipo_documento +
  data_inicio_periodo + data_fim_periodo); acoes_emitidas (cnpj + data_referencia);
  cadastro (ticker); mudancas_ticker (ticker_antigo).

---

## Parte 2: dicionário de colunas (com exemplos)

| Arquivo | Coluna | Chave | O que é | Exemplo |
|---|---|---|---|---|
| noticias.csv | id | Sim | Código único da notícia | n000123 |
| | ticker | Sim | Ação da empresa citada | PETR4 |
| | data_hora_publicacao | | Quando a notícia foi publicada, com fuso (em branco se não houver) | 2026-06-18T19:42:00-03:00 |
| | data_hora_coleta | | Quando a coleta pegou a notícia | 2026-06-18T20:05:00-03:00 |
| | titulo | | Manchete | Petrobras anuncia novo campo no pré-sal |
| | fonte | | Site de origem | InfoMoney |
| | link | | Endereço da notícia | https://... |
| | prob_positivo | | Probabilidade de tom positivo (FinBERT) | 0.81 |
| | prob_neutro | | Probabilidade de tom neutro (FinBERT) | 0.15 |
| | prob_negativo | | Probabilidade de tom negativo (FinBERT) | 0.04 |
| precos.csv | data | Sim | Dia do pregão | 2026-06-18 |
| | ticker | Sim | Ação ou ativo (inclui IBOV, USDBRL, BRENT, IVVB11) | PETR4 |
| | abertura | | Primeiro preço do dia | 37.12 |
| | maxima | | Maior preço do dia | 37.80 |
| | minima | | Menor preço do dia | 36.95 |
| | fechamento | | Último preço, ajustado só por desdobramentos e bonificações | 37.45 |
| | fechamento_ajustado | | Último preço, ajustado também por dividendos | 35.98 |
| | volume | | Quantidade de ações negociadas no dia | 41250300 |
| cadastro.csv | ticker | Sim | Código da ação | KLBN11 |
| | cnpj | | CNPJ da empresa, só números | 89637490000145 |
| | empresa | | Nome da empresa | Klabin |
| | setor | | Setor (classificação da B3) | Materiais Básicos |
| | subsetor | | Subsetor (classificação da B3) | Madeira e Papel |
| | tipo_acao | | ON, PN ou UNIT | UNIT |
| | unit_qtd_on | | Ações ON em cada unit (só nas units) | 1 |
| | unit_qtd_pn | | Ações PN em cada unit (só nas units) | 4 |
| mudancas_ticker.csv | ticker_antigo | Sim | Código anterior | ELET3 |
| | ticker_novo | | Código atual | AXIA3 |
| | data_mudanca | | Primeiro dia com o código novo | 2025-11-10 |
| balancos.csv | cnpj | Sim | CNPJ da empresa, só números | 33000167000101 |
| | tipo_documento | Sim | ITR (trimestre) ou DFP (ano) | ITR |
| | data_inicio_periodo | Sim | Começo do período do valor | 2026-04-01 |
| | data_fim_periodo | Sim | Fim do período do valor | 2026-06-30 |
| | data_entrega | | Quando o balanço foi entregue à CVM | 2026-08-07 |
| | receita_liquida | | Vendas, já sem impostos, em reais (sem escala) | 121500000000 |
| | lucro_bruto | | Receita menos o custo do que foi vendido, em reais (em branco em bancos e seguradoras) | 58300000000 |
| | lucro_liquido | | Lucro final, depois de tudo, em reais | 26700000000 |
| | patrimonio_liquido | | O que pertence aos acionistas no fim do período, em reais | 412000000000 |
| | ebit | | Resultado antes do resultado financeiro e dos tributos, em reais (em branco em bancos e seguradoras) | 38900000000 |
| | depreciacao_amortizacao | | Depreciação e amortização do período, em reais, sempre positiva (em branco em bancos e seguradoras) | 9300000000 |
| | emprestimos_curto_prazo | | Empréstimos, financiamentos e debêntures a pagar em até 1 ano, em reais (em branco em bancos e seguradoras) | 32100000000 |
| | emprestimos_longo_prazo | | Empréstimos, financiamentos e debêntures a pagar depois de 1 ano, em reais (em branco em bancos e seguradoras) | 198400000000 |
| | caixa | | Caixa e equivalentes de caixa no fim do período, em reais (em branco em bancos e seguradoras) | 41800000000 |
| | aplicacoes_financeiras | | Aplicações financeiras de curto prazo no fim do período, em reais (em branco em bancos e seguradoras) | 12600000000 |
| acoes_emitidas.csv | cnpj | Sim | CNPJ da empresa, só números | 33000167000101 |
| | data_referencia | Sim | Data em que a contagem vale | 2026-06-30 |
| | qtd_on | | Número de ações ordinárias | 7442454142 |
| | qtd_pn | | Número de ações preferenciais | 5602042788 |
| proventos.csv | ticker | Sim | Ação que recebe o provento | PETR4 |
| | tipo | Sim | DIVIDENDO ou JCP | JCP |
| | data_com | Sim | Último dia para ter a ação e receber | 2026-06-02 |
| | valor_por_acao | | Valor por ação, já ajustado por desdobramentos e bonificações | 0.4512 |
| | data_pagamento | | Quando o dinheiro cai na conta | 2026-08-20 |
| indices.csv | data | Sim | Dia (no IPCA, o dia 1 do mês) | 2026-06-18 |
| | indice | Sim | CDI, IPCA, IDIV, SMLL ou IFIX | CDI |
| | valor | | CDI e IPCA: taxa em % (0.0551 = 0,0551% ao dia); IDIV, SMLL e IFIX: pontos | 0.0551 |

Vocabulários fechados que aparecem na spec: `tipo_documento` ∈ {ITR, DFP};
`tipo_acao` ∈ {ON, PN, UNIT}; `tipo` (proventos) ∈ {DIVIDENDO, JCP};
`indice` ∈ {CDI, IPCA, IDIV, SMLL, IFIX}. A spec **não** fixa o delimitador do CSV nem o
nome dos arquivos além dos listados.

---

## Adendo (2026-11-02)

Acrescentado em 2026-11-02, sem alterar o texto acima. Cobre a quantidade de ações na base atual e os eventos societários que mudam essa quantidade. A implementação está em [05-entrega.md](05-entrega.md), seções 7 e 11.

### A. Colunas novas de `acoes_emitidas.csv`

As colunas `cnpj, data_referencia, qtd_on, qtd_pn` continuam como estão. Entram quatro colunas:

| Coluna | Descrição |
|---|---|
| data_entrega | Data em que o documento foi recebido pela CVM (`DT_RECEB`). Mostra quando o dado chegou |
| qtd_on_base_atual | Ações ON na base atual: a quantidade do documento convertida pelos desdobramentos, bonificações e grupamentos com data-com a partir da data de referência. É a mesma base do fechamento ajustado por eventos e de `valor_por_acao` em `proventos.csv`. Não é point-in-time |
| qtd_pn_base_atual | Idem, para ações PN |
| situacao | `ok`, `corrigido_escala`, `corrigido_digito`, `anulado` ou `suspeito`. `ok` é o padrão |

`qtd_on` e `qtd_pn` continuam como a CVM publica, exceto nas linhas corrigidas (`corrigido_escala` e `corrigido_digito`) e nas anuladas (`anulado`, em que ficam em branco).

### B. Dicionário de `eventos_acoes.csv`

Chave: (`ticker`, `data_com`, `tipo`, `isin_emitido`). Vocabulário de `tipo`: DESDOBRAMENTO, BONIFICACAO e GRUPAMENTO.

| Coluna | Chave | Descrição | Exemplo |
|---|---|---|---|
| ticker | Sim | Ação afetada | IRBR3 |
| data_com | Sim | Data-com do evento | 2023-01-24 |
| tipo | Sim | DESDOBRAMENTO, BONIFICACAO ou GRUPAMENTO | GRUPAMENTO |
| fator | | Ações depois do evento por ação antes. Desdobramento 1:2 = 2,0; grupamento 30:1 = 0,0333; bonificação de 20% = 1,2 | 0.0333 |
| isin | | ISIN da ação antes do evento | |
| isin_emitido | Sim | ISIN da classe que recebe as ações novas. Difere de `isin` quando a bonificação é paga em outra classe (ex.: RENT, 2025-12). Nesse caso, a quantidade da classe de `isin` não muda | |
| cnpj | | CNPJ da companhia, só números | |
| data_aprovacao | | Data de aprovação do evento, como vem no suplemento da B3 | |

Sentido do fator: `fator` multiplica a quantidade anterior. Para levar a quantidade de uma data de referência à base atual, multiplica-se pelos fatores dos eventos da classe com data-com maior ou igual à data de referência.

### C. O que fica de fora

- Incorporação (ex.: JBSS em 2025, NATU em 2019).
- Cisão (CIS RED CAP), resgate (RESG TOTAL RV) e redução ou restituição de capital. O sentido do fator desses eventos não foi verificado.
- Nenhum deles muda a contagem da própria classe.
- A troca de código de ação não entra aqui: aparece em `mudancas_ticker`.

---

## Adendo (2026-11-03)

Acrescentado em 2026-11-03, sem alterar o texto acima nem o adendo de 2026-11-02. Cobre o emissor por período (troca de código que veio junto com troca de companhia), a situação dos balanços e a hora das notícias do Google News. A implementação está em [05-entrega.md](05-entrega.md), seções 2.3, 6, 7, 8 e 12.

### A. `emissores_ticker.csv` (dicionário)

Chave: (`ticker`, `ordem`). Uma linha por período de emissor de cada ticker. Ticker que nunca trocou de companhia tem uma linha só.

| Coluna | Descrição | Exemplo |
|---|---|---|
| ticker | Ticker atual, o mesmo das outras tabelas | JBSS32 |
| ordem | Número do período dentro do ticker | 1 |
| ticker_negociado | Código que a B3 negociava naquele período | JBSS3 |
| cd_cvm | Código CVM do emissor | 20575 |
| cnpj | CNPJ do emissor, só números | 02916265000160 |
| empresa | Nome do emissor naquele período | JBS S.A. |
| vigencia_inicio | Primeiro dia do período (inclusive) | |
| vigencia_fim | Último dia do período (inclusive). Vazio quer dizer vigente | 2025-06-08 |
| fator_acoes | Ações do ticker atual por ação do emissor. 1 BDR = 2 ações JBSS3 dá 0,5. Troca 1:1 dá 1 | 0.5 |

Os dois tickers com mais de um emissor:

- JBSS32: JBS S.A. (CVM 20575, negociada como JBSS3 até 2025-06-08, fator 0,5) e JBS N.V. (CVM 80233, desde 2025-06-09).
- NATU3: Natura Cosméticos (CVM 19550) até 2019-12-17; Natura &Co Holding (CVM 24783, negociada como NTCO3 de 2019-12-18 a 2025-07-01, troca 1:1); Natura Cosméticos de novo desde 2025-07-02.

### B. Regra do emissor por período

- **Data que decide.** Qual emissor vale é decidido pela data de entrega à CVM, não pela data de referência: `data_entrega` (`DT_RECEB`) nos balanços e nas ações, `data_entrega` nos documentos do IPE, e a data-com nos proventos. Pela data de referência, o 2T25 da Natura ficaria sem emissor, porque NTCO3 termina em 2025-07-01 e NATU3 volta em 2025-07-02. A data de entrega é a que já se conhecia na época, então não há vazamento temporal.
- **Ticker sempre o atual.** Em todas as tabelas, o ticker é o atual (JBSS32 ou NATU3), mesmo quando o documento veio sob o código antigo.
- **Ações emitidas.** Na base atual (`qtd_on_base_atual`, `qtd_pn_base_atual`), a quantidade do emissor é multiplicada pelo `fator_acoes` do emissor.
- **Balanços e notícias do IPE.** Só entram no ticker se o emissor valia na data.

### C. Situação em `balancos.csv`

Coluna nova `situacao`:

| Valor | Quando |
|---|---|
| ok | Padrão |
| ajuste_ifrs17 | A receita tem o ajuste de seguros do IFRS 17 (só HAPV3, em 1T25, 2T25, 1T26 e 2T26) |
| reapresentado | A linha usa o comparativo (PENÚLTIMO) de uma remessa posterior, porque o documento original veio com receita zerada |

Se as duas se aplicam, vale `reapresentado`.

### D. `receita_liquida` com ajuste IFRS 17 (HAPV3)

Para a HAPV3, `receita_liquida` deixa de ser a conta 3.01 publicada. A companhia publica a despesa de seguros dentro da 3.01, numa subconta 3.01.NN negativa; a entrega subtrai essa subconta, ou seja, soma o seu valor absoluto. Exemplo: 1T25 passa de R$ 1,47 bi para R$ 7,80 bi, coerente com o 3T25 (R$ 7,92 bi). Nas outras companhias, `receita_liquida` continua igual à conta 3.01.

### E. Proventos no ticker atual

`proventos.csv` traz sempre o ticker atual. Proventos de JBSS3 saem como JBSS32, com `valor_por_acao` dividido por 0,5. Proventos de NTCO3 saem como NATU3. Proventos de um BDR que só existe no suplemento da B3 entram.

### F. Notícias com data só

Notícia com precisão de data (`published_precision = 'date'`) sai com `data_hora_publicacao` vazia e `momento_pregao` = `desconhecido`. Isso vale também para as notícias do Google News que tinham a hora falsa de 00:00 do Pacífico gravada como hora exata: a correção remarca para data, e o dia não muda.
