-- Migration 0005 (fontes novas CVM/B3 e juiza de noticias) para MySQL 8 -- gerado por:
--   alembic upgrade 0004:0005 --sql   (dialeto MySQL, modo offline)
--
-- Pre-requisito: o banco ja esta na 0004. Confira antes de rodar:
--   SELECT version_num FROM alembic_version;   -- deve devolver '0004'
-- Banco vazio? Use completo_ate_0003.mysql.sql e depois 0004 e esta.
--
-- As tabelas herdam o charset do database (deve ser utf8mb4; ver docs/MIGRACAO-MYSQL.md).
-- DDL no MySQL nao e transacional: se falhar no meio, as tabelas ja criadas ficam.

-- Running upgrade 0004 -> 0005

CREATE TABLE raw_ipe_docs (
    protocolo_entrega VARCHAR(32) NOT NULL, 
    cnpj VARCHAR(14) NOT NULL, 
    nome_companhia TEXT, 
    cd_cvm INTEGER NOT NULL, 
    data_referencia DATE, 
    categoria VARCHAR(64) NOT NULL, 
    tipo VARCHAR(128), 
    especie VARCHAR(128), 
    assunto TEXT, 
    data_entrega DATE NOT NULL, 
    tipo_apresentacao VARCHAR(32), 
    versao INTEGER, 
    link_download TEXT NOT NULL, 
    fetched_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_ipe_docs PRIMARY KEY (protocolo_entrega)
);

CREATE INDEX ix_raw_ipe_docs_cd_cvm ON raw_ipe_docs (cd_cvm);

CREATE TABLE raw_cvm_balancos (
    cnpj VARCHAR(14) NOT NULL, 
    tipo_documento VARCHAR(3) NOT NULL, 
    dt_refer DATE NOT NULL, 
    versao INTEGER NOT NULL, 
    demonstracao VARCHAR(3) NOT NULL, 
    cd_conta VARCHAR(16) NOT NULL, 
    dt_ini_exerc DATE NOT NULL, 
    dt_fim_exerc DATE NOT NULL, 
    cd_cvm INTEGER NOT NULL, 
    ds_conta VARCHAR(255), 
    ordem_exerc VARCHAR(16) NOT NULL, 
    escala_moeda VARCHAR(8) NOT NULL, 
    vl_conta DOUBLE NOT NULL, 
    dt_receb DATE, 
    fetched_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_cvm_balancos PRIMARY KEY (cnpj, tipo_documento, dt_refer, versao, demonstracao, cd_conta, dt_ini_exerc, dt_fim_exerc)
);

CREATE INDEX ix_raw_cvm_balancos_cd_cvm ON raw_cvm_balancos (cd_cvm);

CREATE TABLE raw_cvm_acoes (
    cnpj VARCHAR(14) NOT NULL, 
    tipo_documento VARCHAR(3) NOT NULL, 
    dt_refer DATE NOT NULL, 
    versao INTEGER NOT NULL, 
    cd_cvm INTEGER NOT NULL, 
    qt_on BIGINT, 
    qt_pn BIGINT, 
    qt_total BIGINT, 
    dt_receb DATE, 
    fetched_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_cvm_acoes PRIMARY KEY (cnpj, tipo_documento, dt_refer, versao)
);

CREATE TABLE raw_cvm_cadastro (
    cnpj VARCHAR(14) NOT NULL, 
    cd_cvm INTEGER NOT NULL, 
    denom_social TEXT NOT NULL, 
    sit VARCHAR(32), 
    setor_ativ TEXT, 
    fetched_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_cvm_cadastro PRIMARY KEY (cnpj)
);

CREATE INDEX ix_raw_cvm_cadastro_cd_cvm ON raw_cvm_cadastro (cd_cvm);

CREATE TABLE raw_b3_cadastro (
    cd_cvm INTEGER NOT NULL AUTO_INCREMENT, 
    cnpj VARCHAR(14), 
    trading_name VARCHAR(128), 
    issuing_company VARCHAR(8), 
    industry_classification TEXT, 
    other_codes JSON, 
    payload JSON NOT NULL, 
    fetched_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_b3_cadastro PRIMARY KEY (cd_cvm)
);

CREATE TABLE raw_b3_proventos (
    registro_hash VARCHAR(64) NOT NULL, 
    issuing_company VARCHAR(8) NOT NULL, 
    trading_name VARCHAR(128), 
    type_stock VARCHAR(16), 
    corporate_action VARCHAR(64), 
    value_cash DOUBLE, 
    last_date_prior_ex DATE, 
    date_approval DATE, 
    ratio VARCHAR(32), 
    quoted_per_shares VARCHAR(32), 
    payload JSON NOT NULL, 
    fetched_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_b3_proventos PRIMARY KEY (registro_hash)
);

CREATE INDEX ix_raw_b3_proventos_issuing_company ON raw_b3_proventos (issuing_company);

CREATE TABLE raw_b3_suplemento (
    registro_hash VARCHAR(64) NOT NULL, 
    issuing_company VARCHAR(8) NOT NULL, 
    tipo VARCHAR(8) NOT NULL, 
    payload JSON NOT NULL, 
    fetched_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_b3_suplemento PRIMARY KEY (registro_hash)
);

CREATE INDEX ix_raw_b3_suplemento_issuing_company ON raw_b3_suplemento (issuing_company);

CREATE TABLE raw_llm_juiza_runs (
    id BIGINT NOT NULL AUTO_INCREMENT, 
    prompt_id BIGINT NOT NULL, 
    modelo VARCHAR(64) NOT NULL, 
    n_itens INTEGER NOT NULL, 
    tokens_entrada INTEGER, 
    tokens_saida INTEGER, 
    latencia_ms INTEGER, 
    status VARCHAR(16) NOT NULL, 
    erro TEXT, 
    resposta_bruta LONGTEXT, 
    iniciado_em DATETIME(6) NOT NULL, 
    finalizado_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_llm_juiza_runs PRIMARY KEY (id), 
    CONSTRAINT fk_raw_llm_juiza_runs_prompt_id FOREIGN KEY(prompt_id) REFERENCES raw_llm_prompts (id)
);

CREATE TABLE staging_noticias_julgamento (
    article_id BIGINT NOT NULL, 
    ticker VARCHAR(12) NOT NULL, 
    prompt_versao INTEGER NOT NULL, 
    run_id BIGINT, 
    modelo VARCHAR(64) NOT NULL, 
    papel VARCHAR(24) NOT NULL, 
    tipo_conteudo VARCHAR(24) NOT NULL, 
    evento VARCHAR(24) NOT NULL, 
    tickers_adicionais JSON NOT NULL, 
    qualidade DOUBLE NOT NULL, 
    confianca DOUBLE NOT NULL, 
    julgado_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_staging_noticias_julgamento PRIMARY KEY (article_id, ticker, prompt_versao), 
    CONSTRAINT fk_staging_noticias_julgamento_article_id FOREIGN KEY(article_id) REFERENCES raw_articles (id) ON DELETE CASCADE, 
    CONSTRAINT fk_staging_noticias_julgamento_run_id FOREIGN KEY(run_id) REFERENCES raw_llm_juiza_runs (id)
);

CREATE INDEX ix_staging_noticias_julgamento_ticker ON staging_noticias_julgamento (ticker);

UPDATE alembic_version SET version_num='0005' WHERE alembic_version.version_num = '0004';

