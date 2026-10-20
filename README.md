# tcc_scraper

Coleta notícias e dados de mercado da B3 para o TCC. Produz o dataset v1 (congelado, 14 colunas por
data e ticker) e as oito tabelas de entrega para a etapa seguinte, atualizadas todo dia de pregão.
Coleta de fontes públicas (Yahoo Finance, Google News, BCB, CVM, B3), com custo zero, exceto a LLM
do Google, com teto de US$ 5 por mês. A análise e a modelagem ficam fora deste repositório.

## Duas pipelines

| Pipeline | O que produz | Como roda |
|---|---|---|
| Dataset v1 (congelado) | Tabela `features_dataset_v1`, com Parquet e CSV. Janela fixa de 1 ano | Comandos por fase: `collect-prices`, `collect-news`, `enrich`, `score`, `build-dataset` |
| Pipeline nova (entrega) | Oito tabelas `v1-noticias` a `v1-indices`, com as colunas da spec | `daily` com 9 etapas, às 18:30 (Brasília). O `executor` roda como worker e recebe comandos do painel |

O detalhe das duas pipelines está em [Visão geral](docs/01-visao-geral.md).

## Início rápido

Pré-requisitos: Python 3.11 ou superior e acesso ao MySQL 8 configurado no `.env`. O projeto não tem
banco local.

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"           # para FinBERT e análise por LLM, acrescente os extras sentiment e llm
cp .env.example .env              # preencha as variáveis do banco e, se usar, as chaves
python -m tcc_scraper.cli db-info # confere para qual banco o .env aponta e se as tabelas existem
```

Em um banco novo, aplique o schema antes de coletar:

```bash
alembic upgrade head
```

Em um banco que já tem dados, leia antes [Banco de dados](docs/06-banco-de-dados.md). A migração
0008 mexe na tabela `alembic_version` e tem um cuidado próprio.

Para testar uma etapa só:

```bash
python -m tcc_scraper.cli daily --etapa precos
```

Atenção: os comandos gravam no banco que o `.env` aponta. Confira o resultado de `db-info` antes.

Para a lista completa de comandos, use `python -m tcc_scraper.cli --help`. As opções de cada um estão em
[Operação e deploy](docs/08-operacao-e-deploy.md).

## Documentação

- [Índice da documentação](docs/README.md): cada documento, para quem é e quando ler
- [01 - Visão geral](docs/01-visao-geral.md): objetivo, camadas, fontes e glossário
- [02 - Pipeline diária](docs/02-pipeline-diaria.md): as 9 etapas do daily
- [03 - Carga histórica](docs/03-carga-historica.md): anos passados, pausa e retomada
- [04 - Juíza e LLM](docs/04-juiza-e-llm.md): julgamento das notícias e análise diária
- [05 - Entrega](docs/05-entrega.md): as oito tabelas entregues, coluna por coluna
- [06 - Banco de dados](docs/06-banco-de-dados.md): tabelas, migrações e regras de tipo
- [07 - Executor e painel](docs/07-executor-e-painel.md): worker, comandos e tabelas de progresso
- [08 - Operação e deploy](docs/08-operacao-e-deploy.md): variáveis, Railway e checklist de produção
- [09 - Histórico de mudanças](docs/09-historico-de-mudancas.md): o que foi feito e o que ficou pendente

Decisões de arquitetura estão em [ADR](docs/ADR.md). A spec de entrega está em
[spec de entrega](docs/spec-entrega-etapa-seguinte.md). As regras de código para agentes estão em
[CLAUDE.md](CLAUDE.md).
