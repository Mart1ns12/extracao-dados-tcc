-- Migration 0008 (prefixo `v1-` nas 38 tabelas da pipeline nova) para MySQL 8 -- gerado por:
--   alembic upgrade 0007:0008 --sql   (dialeto MySQL, modo offline, DB_TARGET=mysql)
--
-- Pre-requisito: o banco ja esta na 0007. Confira antes de rodar:
--   SELECT version_num FROM alembic_version;   -- deve devolver '0007'
--
-- RENAME TABLE nao e transacional no MySQL: se falhar no meio, parte das tabelas ja foi
-- renomeada. Nenhuma linha e reescrita. Chaves, indices e constraints mantem o nome antigo;
-- as foreign keys seguem a tabela renomeada (MySQL 8 atualiza a definicao).
-- Tabelas que ficam sem prefixo: features_dataset_v1 e alembic_version.

-- Running upgrade 0007 -> 0008

ALTER TABLE raw_articles RENAME TO `v1-raw_articles`;

ALTER TABLE raw_article_bodies RENAME TO `v1-raw_article_bodies`;

ALTER TABLE raw_prices RENAME TO `v1-raw_prices`;

ALTER TABLE raw_macro RENAME TO `v1-raw_macro`;

ALTER TABLE raw_collection_runs RENAME TO `v1-raw_collection_runs`;

ALTER TABLE staging_article_tickers RENAME TO `v1-staging_article_tickers`;

ALTER TABLE staging_article_sentiment RENAME TO `v1-staging_article_sentiment`;

ALTER TABLE raw_llm_prompts RENAME TO `v1-raw_llm_prompts`;

ALTER TABLE raw_llm_runs RENAME TO `v1-raw_llm_runs`;

ALTER TABLE staging_llm_noticias_empresas RENAME TO `v1-staging_llm_noticias_empresas`;

ALTER TABLE staging_llm_noticias_macro RENAME TO `v1-staging_llm_noticias_macro`;

ALTER TABLE staging_llm_acoes_sinais RENAME TO `v1-staging_llm_acoes_sinais`;

ALTER TABLE staging_llm_mercados RENAME TO `v1-staging_llm_mercados`;

ALTER TABLE painel_comandos RENAME TO `v1-painel_comandos`;

ALTER TABLE painel_heartbeat RENAME TO `v1-painel_heartbeat`;

ALTER TABLE painel_execucoes RENAME TO `v1-painel_execucoes`;

ALTER TABLE painel_etapas RENAME TO `v1-painel_etapas`;

ALTER TABLE painel_historico RENAME TO `v1-painel_historico`;

ALTER TABLE painel_historico_progresso RENAME TO `v1-painel_historico_progresso`;

ALTER TABLE painel_logs RENAME TO `v1-painel_logs`;

ALTER TABLE painel_comandos_aplicados RENAME TO `v1-painel_comandos_aplicados`;

ALTER TABLE raw_ipe_docs RENAME TO `v1-raw_ipe_docs`;

ALTER TABLE raw_cvm_balancos RENAME TO `v1-raw_cvm_balancos`;

ALTER TABLE raw_cvm_acoes RENAME TO `v1-raw_cvm_acoes`;

ALTER TABLE raw_cvm_cadastro RENAME TO `v1-raw_cvm_cadastro`;

ALTER TABLE raw_b3_cadastro RENAME TO `v1-raw_b3_cadastro`;

ALTER TABLE raw_b3_proventos RENAME TO `v1-raw_b3_proventos`;

ALTER TABLE raw_b3_suplemento RENAME TO `v1-raw_b3_suplemento`;

ALTER TABLE raw_llm_juiza_runs RENAME TO `v1-raw_llm_juiza_runs`;

ALTER TABLE staging_noticias_julgamento RENAME TO `v1-staging_noticias_julgamento`;

ALTER TABLE noticias RENAME TO `v1-noticias`;

ALTER TABLE precos RENAME TO `v1-precos`;

ALTER TABLE cadastro RENAME TO `v1-cadastro`;

ALTER TABLE mudancas_ticker RENAME TO `v1-mudancas_ticker`;

ALTER TABLE balancos RENAME TO `v1-balancos`;

ALTER TABLE acoes_emitidas RENAME TO `v1-acoes_emitidas`;

ALTER TABLE proventos RENAME TO `v1-proventos`;

ALTER TABLE indices RENAME TO `v1-indices`;

UPDATE alembic_version SET version_num='0008' WHERE alembic_version.version_num = '0007';

