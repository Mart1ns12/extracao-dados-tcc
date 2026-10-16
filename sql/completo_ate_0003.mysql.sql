CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE TABLE raw_articles (
    id BIGINT NOT NULL AUTO_INCREMENT, 
    url_hash VARCHAR(64) NOT NULL, 
    url TEXT NOT NULL, 
    google_id TEXT, 
    resolved_url TEXT, 
    title TEXT NOT NULL, 
    source_name TEXT, 
    source_domain VARCHAR(255), 
    published_at DATETIME(6) NOT NULL, 
    lang VARCHAR(5) NOT NULL, 
    collected_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_articles PRIMARY KEY (id), 
    CONSTRAINT uq_raw_articles_url_hash UNIQUE (url_hash)
);

CREATE INDEX ix_articles_published_at ON raw_articles (published_at);

CREATE INDEX ix_articles_source_domain ON raw_articles (source_domain);

CREATE TABLE raw_article_bodies (
    article_id BIGINT NOT NULL, 
    body_text TEXT NOT NULL, 
    extraction_method VARCHAR(32) NOT NULL, 
    fetched_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_article_bodies PRIMARY KEY (article_id), 
    CONSTRAINT fk_raw_article_bodies_article_id FOREIGN KEY(article_id) REFERENCES raw_articles (id) ON DELETE CASCADE
);

CREATE TABLE raw_prices (
    ticker VARCHAR(12) NOT NULL, 
    date DATE NOT NULL, 
    open DOUBLE, 
    high DOUBLE, 
    low DOUBLE, 
    close DOUBLE, 
    adj_close DOUBLE, 
    volume BIGINT, 
    source VARCHAR(16) NOT NULL, 
    CONSTRAINT pk_raw_prices PRIMARY KEY (ticker, date)
);

CREATE TABLE raw_macro (
    series VARCHAR(24) NOT NULL, 
    date DATE NOT NULL, 
    value DOUBLE NOT NULL, 
    CONSTRAINT pk_raw_macro PRIMARY KEY (series, date)
);

CREATE TABLE raw_collection_runs (
    id BIGINT NOT NULL AUTO_INCREMENT, 
    collector VARCHAR(32) NOT NULL, 
    unit_key VARCHAR(128) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    items_found INTEGER, 
    hit_cap BOOL, 
    error TEXT, 
    finished_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_raw_collection_runs PRIMARY KEY (id), 
    CONSTRAINT uq_collection_runs_collector_unit UNIQUE (collector, unit_key)
);

CREATE TABLE staging_article_tickers (
    article_id BIGINT NOT NULL, 
    ticker VARCHAR(12) NOT NULL, 
    CONSTRAINT pk_staging_article_tickers PRIMARY KEY (article_id, ticker), 
    CONSTRAINT fk_staging_article_tickers_article_id FOREIGN KEY(article_id) REFERENCES raw_articles (id) ON DELETE CASCADE
);

CREATE INDEX ix_article_tickers_ticker ON staging_article_tickers (ticker);

CREATE TABLE staging_article_sentiment (
    article_id BIGINT NOT NULL, 
    model VARCHAR(64) NOT NULL, 
    prob_pos DOUBLE NOT NULL, 
    prob_neu DOUBLE NOT NULL, 
    prob_neg DOUBLE NOT NULL, 
    text_source VARCHAR(8) NOT NULL, 
    scored_at DATETIME(6) NOT NULL, 
    CONSTRAINT pk_staging_article_sentiment PRIMARY KEY (article_id, model), 
    CONSTRAINT ck_article_sentiment_probs_nonneg CHECK (prob_pos >= 0 AND prob_neu >= 0 AND prob_neg >= 0), 
    CONSTRAINT fk_staging_article_sentiment_article_id FOREIGN KEY(article_id) REFERENCES raw_articles (id) ON DELETE CASCADE
);

CREATE TABLE features_dataset_v1 (
    data DATE NOT NULL, 
    ticker VARCHAR(12) NOT NULL, 
    quant_noticias INTEGER NOT NULL, 
    positivo DOUBLE, 
    neutro DOUBLE, 
    negativo DOUBLE, 
    score_finbert DOUBLE, 
    retorno_1d DOUBLE, 
    retorno_5d DOUBLE, 
    retorno_20d DOUBLE, 
    volatilidade_20d DOUBLE, 
    volume_relativo DOUBLE, 
    retorno_ibovespa DOUBLE, 
    target SMALLINT, 
    CONSTRAINT pk_features_dataset_v1 PRIMARY KEY (data, ticker)
);

INSERT INTO alembic_version (version_num) VALUES ('0001');

-- Running upgrade 0001 -> 0002

UPDATE raw_collection_runs SET items_found = 0 WHERE items_found IS NULL;

UPDATE raw_collection_runs SET hit_cap = FALSE WHERE hit_cap IS NULL;

ALTER TABLE raw_collection_runs MODIFY items_found INTEGER NOT NULL;

ALTER TABLE raw_collection_runs MODIFY hit_cap BOOL NOT NULL;

UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001';

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

