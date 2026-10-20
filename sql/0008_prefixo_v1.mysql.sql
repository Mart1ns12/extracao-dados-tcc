-- Migration 0008 (prefixo `v1-` nas 25 tabelas criadas pela pipeline nova) para MySQL 8 --
-- gerado por:
--   alembic upgrade 0007:0008 --sql   (dialeto MySQL, modo offline; usa o MySQL do .env)
--
-- Tabelas com `v1-` (25): as criadas a partir da 0004 -- painel_comandos, painel_heartbeat,
-- painel_execucoes, painel_etapas, painel_historico, painel_historico_progresso, painel_logs,
-- painel_comandos_aplicados, raw_ipe_docs, raw_cvm_balancos, raw_cvm_acoes, raw_cvm_cadastro,
-- raw_b3_cadastro, raw_b3_proventos, raw_b3_suplemento, raw_llm_juiza_runs,
-- staging_noticias_julgamento, noticias, precos, cadastro, mudancas_ticker, balancos,
-- acoes_emitidas, proventos, indices.
-- Sem prefixo (14): raw_articles, raw_article_bodies, raw_prices, raw_macro,
-- raw_collection_runs, staging_article_tickers, staging_article_sentiment, raw_llm_prompts,
-- raw_llm_runs, staging_llm_noticias_empresas, staging_llm_noticias_macro,
-- staging_llm_acoes_sinais, staging_llm_mercados, features_dataset_v1 (e alembic_version).
--
-- Pre-requisito: o banco esta na 0007, mas o alembic_version tem DUAS linhas ('0002' e
-- '0007'); a '0002' e uma sobra antiga e faz o UPDATE abaixo errar. Confira e remova a sobra:
--   SELECT version_num FROM alembic_version;   -- deve devolver '0002' e '0007'
DELETE FROM alembic_version WHERE version_num = '0002';
--   SELECT version_num FROM alembic_version;   -- depois do DELETE: so '0007'
--
-- RENAME TABLE nao e transacional no MySQL: se falhar no meio, parte das tabelas ja foi
-- renomeada. Nenhuma linha e reescrita. Chaves, indices e constraints mantem o nome antigo;
-- as foreign keys seguem a tabela renomeada (MySQL 8 atualiza a definicao).

-- Running upgrade 0007 -> 0008

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

