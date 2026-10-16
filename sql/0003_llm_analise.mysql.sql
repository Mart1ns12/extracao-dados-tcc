-- Migration 0003 (analise diaria por LLM) para MySQL 8 -- gerado por:
--   alembic upgrade 0002:0003 --sql   (dialeto MySQL, modo offline)
--
-- Pre-requisito: o banco ja esta na 0002. Confira antes de rodar:
--   SELECT version_num FROM alembic_version;   -- deve devolver '0002'
-- Banco vazio? Use completo_ate_0003.mysql.sql (cria tudo, 0001 -> 0003).
--
-- As tabelas herdam o charset do database (deve ser utf8mb4; ver docs/MIGRACAO-MYSQL.md).
-- DDL no MySQL nao e transacional: se falhar no meio, as tabelas ja criadas ficam.

-- Running upgrade 0002 -> 0003

CREATE TABLE raw_llm_prompts (
    id BIGINT NOT NULL AUTO_INCREMENT, 
    nome VARCHAR(64) NOT NULL, 
    versao INTEGER NOT NULL, 
    texto_template LONGTEXT NOT NULL, 
    schema_json JSON NOT NULL, 
    sha256 VARCHAR(64) NOT NULL, 
    criado_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_llm_prompts PRIMARY KEY (id), 
    CONSTRAINT uq_llm_prompts_nome_versao UNIQUE (nome, versao)
);

CREATE TABLE raw_llm_runs (
    id BIGINT NOT NULL AUTO_INCREMENT, 
    prompt_id BIGINT NOT NULL, 
    data_referencia DATE NOT NULL, 
    pregao_referencia DATE, 
    modelo VARCHAR(64) NOT NULL, 
    prompt_renderizado LONGTEXT NOT NULL, 
    resposta_bruta LONGTEXT, 
    resposta_json JSON, 
    resumo_geral TEXT, 
    buscas_json JSON, 
    citacoes_json JSON, 
    tokens_entrada INTEGER, 
    tokens_saida INTEGER, 
    tokens_pensamento INTEGER, 
    latencia_ms INTEGER, 
    status VARCHAR(16) NOT NULL, 
    erro TEXT, 
    iniciado_em DATETIME(6) NOT NULL, 
    finalizado_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_llm_runs PRIMARY KEY (id), 
    CONSTRAINT fk_raw_llm_runs_prompt_id FOREIGN KEY(prompt_id) REFERENCES raw_llm_prompts (id)
);

CREATE INDEX ix_llm_runs_data_referencia ON raw_llm_runs (data_referencia);

CREATE TABLE staging_llm_noticias_empresas (
    data_referencia DATE NOT NULL, 
    ticker VARCHAR(12) NOT NULL, 
    item_hash VARCHAR(64) NOT NULL, 
    run_id BIGINT NOT NULL, 
    pregao_referencia DATE, 
    titulo TEXT NOT NULL, 
    resumo TEXT NOT NULL, 
    fonte VARCHAR(255), 
    url TEXT, 
    publicado_em DATE, 
    setor VARCHAR(32) NOT NULL, 
    sentimento VARCHAR(16) NOT NULL, 
    relevancia DOUBLE NOT NULL, 
    CONSTRAINT pk_staging_llm_noticias_empresas PRIMARY KEY (data_referencia, ticker, item_hash), 
    CONSTRAINT fk_staging_llm_noticias_empresas_run_id FOREIGN KEY(run_id) REFERENCES raw_llm_runs (id) ON DELETE CASCADE
);

CREATE INDEX ix_llm_noticias_empresas_ticker ON staging_llm_noticias_empresas (ticker);

CREATE TABLE staging_llm_noticias_macro (
    data_referencia DATE NOT NULL, 
    item_hash VARCHAR(64) NOT NULL, 
    run_id BIGINT NOT NULL, 
    pregao_referencia DATE, 
    escopo VARCHAR(8) NOT NULL, 
    titulo TEXT NOT NULL, 
    resumo TEXT NOT NULL, 
    fonte VARCHAR(255), 
    url TEXT, 
    publicado_em DATE, 
    commodities_afetadas JSON NOT NULL, 
    direcao_commodity VARCHAR(16) NOT NULL, 
    setores_afetados JSON NOT NULL, 
    CONSTRAINT pk_staging_llm_noticias_macro PRIMARY KEY (data_referencia, item_hash), 
    CONSTRAINT fk_staging_llm_noticias_macro_run_id FOREIGN KEY(run_id) REFERENCES raw_llm_runs (id) ON DELETE CASCADE
);

CREATE TABLE staging_llm_acoes_sinais (
    data_referencia DATE NOT NULL, 
    ticker VARCHAR(12) NOT NULL, 
    run_id BIGINT NOT NULL, 
    pregao_referencia DATE, 
    direcao_esperada VARCHAR(8) NOT NULL, 
    confianca DOUBLE NOT NULL, 
    motivo TEXT NOT NULL, 
    setor VARCHAR(32) NOT NULL, 
    noticias_relacionadas JSON NOT NULL, 
    CONSTRAINT pk_staging_llm_acoes_sinais PRIMARY KEY (data_referencia, ticker), 
    CONSTRAINT fk_staging_llm_acoes_sinais_run_id FOREIGN KEY(run_id) REFERENCES raw_llm_runs (id) ON DELETE CASCADE
);

CREATE INDEX ix_llm_acoes_sinais_ticker ON staging_llm_acoes_sinais (ticker);

CREATE TABLE staging_llm_mercados (
    data_referencia DATE NOT NULL, 
    nome VARCHAR(64) NOT NULL, 
    run_id BIGINT NOT NULL, 
    pregao_referencia DATE, 
    tipo VARCHAR(16) NOT NULL, 
    variacao_pct DOUBLE, 
    motivo TEXT NOT NULL, 
    CONSTRAINT pk_staging_llm_mercados PRIMARY KEY (data_referencia, nome), 
    CONSTRAINT fk_staging_llm_mercados_run_id FOREIGN KEY(run_id) REFERENCES raw_llm_runs (id) ON DELETE CASCADE
);

UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002';

