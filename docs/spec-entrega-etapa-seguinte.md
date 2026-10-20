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
