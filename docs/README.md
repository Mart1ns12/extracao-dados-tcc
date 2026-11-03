# Documentação

Índice da documentação do `tcc_scraper`. Cada documento tem um público e um momento de leitura.
Comece pelo [README da raiz](../README.md) se ainda não conhece o projeto.

## Documentos

| Documento | Para quem é | Quando ler |
|---|---|---|
| [01 - Visão geral](01-visao-geral.md) | Todos. Quem lê o TCC ou a etapa seguinte encontra aqui o que o sistema faz e de onde vem cada dado | Antes de tudo. Para entender as fontes, as camadas e os termos |
| [02 - Pipeline diária](02-pipeline-diaria.md) | Operador | Para saber o que cada uma das 9 etapas do daily faz, o que grava e o que acontece quando uma falha |
| [03 - Carga histórica](03-carga-historica.md) | Operador | Ao carregar anos passados, pausar, cancelar ou retomar uma carga |
| [04 - Juíza e LLM](04-juiza-e-llm.md) | Operador e leitor do TCC | Para entender como as notícias são julgadas e como funciona a análise diária por Gemini, e o custo |
| [05 - Entrega](05-entrega.md) | Quem consome as tabelas (etapa seguinte) | Para saber a origem, a chave e a regra de atualização de cada coluna das onze tabelas entregues |
| [06 - Banco de dados](06-banco-de-dados.md) | Operador e quem consulta o banco | Para ver o dicionário de tabelas, o schema e as alterações, e as regras de tipo |
| [07 - Executor e painel](07-executor-e-painel.md) | Operador | Para ligar o worker, entender os comandos do painel e conferir as tabelas de progresso |
| [08 - Operação e deploy](08-operacao-e-deploy.md) | Operador | Ao preparar o ambiente, configurar variáveis, publicar no Railway ou resolver um problema conhecido |
| [09 - Histórico de mudanças](09-historico-de-mudancas.md) | Orientador, banca e o próprio autor | Para saber o que foi feito, quando, por quê, e quais decisões e pendências ficaram abertas |

## Ordem sugerida de leitura

Para quem opera o pipeline:

1. [01 - Visão geral](01-visao-geral.md), para o modelo mental
2. [08 - Operação e deploy](08-operacao-e-deploy.md), para o ambiente e os comandos
3. [02 - Pipeline diária](02-pipeline-diaria.md) e [07 - Executor e painel](07-executor-e-painel.md)
4. [03 - Carga histórica](03-carga-historica.md) e [06 - Banco de dados](06-banco-de-dados.md)
5. [04 - Juíza e LLM](04-juiza-e-llm.md)

Para quem lê o TCC ou consome a entrega:

1. [01 - Visão geral](01-visao-geral.md)
2. [05 - Entrega](05-entrega.md), para as onze tabelas e a origem de cada coluna
3. [04 - Juíza e LLM](04-juiza-e-llm.md), para a qualidade das notícias
4. [09 - Histórico de mudanças](09-historico-de-mudancas.md), para o que mudou e o que ficou pendente

## Documentos de referência

Estes arquivos ficam onde estão. Os documentos acima apontam para eles quando precisam.

| Arquivo | O que é |
|---|---|
| [ADR.md](ADR.md) | Registro de decisões de arquitetura, com o motivo de cada escolha e o que se perdeu com ela |
| [spec-entrega-etapa-seguinte.md](spec-entrega-etapa-seguinte.md) | Texto da spec recebida do dono do projeto. É o contrato das oito tabelas de entrega |
| [superpowers/specs/2026-10-19-entrega-etapa-seguinte-design.md](superpowers/specs/2026-10-19-entrega-etapa-seguinte-design.md) | Desenho que implementa a spec de entrega |
| [Dataset1.0.xlsx](Dataset1.0.xlsx) | Modelo de referência do dataset v1 (14 colunas) |
| [fontes_dataset.xlsx](fontes_dataset.xlsx) | Planilha com as fontes usadas no dataset v1 |
