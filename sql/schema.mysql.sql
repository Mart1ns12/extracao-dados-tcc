-- Schema completo do MySQL (estado atual, equivalente às antigas migrations 0001-0009).
-- Aplicar somente em banco VAZIO. O Alembic foi descontinuado em 2026-10-20; mudanças de schema:
-- alterar o model em src/tcc_scraper/db/ E este arquivo, e escrever o ALTER em
-- sql/alteracoes/AAAA-MM-DD_descricao.mysql.sql, aplicado à mão.
--
-- As tabelas criadas pela pipeline nova (30) saem com o prefixo `v1-` pelos RENAME da
-- Etapa 0008, no fim do arquivo (a tabela de tentativas da Etapa 0009 já nasce com o prefixo).
-- As 14 antigas mantêm o nome.
--

-- Etapa 0001: estrutura inicial

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
    published_time_source VARCHAR(8),
    lang VARCHAR(5) NOT NULL,
    collected_at DATETIME(6) NOT NULL,
    CONSTRAINT pk_raw_articles PRIMARY KEY (id),
    CONSTRAINT uq_raw_articles_url_hash UNIQUE (url_hash)
);

CREATE INDEX ix_articles_published_at ON raw_articles (published_at);

CREATE INDEX ix_articles_source_domain ON raw_articles (source_domain);

CREATE TABLE raw_article_bodies (
    article_id BIGINT NOT NULL, 
    body_text LONGTEXT NOT NULL, 
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

-- Etapa 0002: raw_collection_runs NOT NULL

UPDATE raw_collection_runs SET items_found = 0 WHERE items_found IS NULL;

UPDATE raw_collection_runs SET hit_cap = FALSE WHERE hit_cap IS NULL;

ALTER TABLE raw_collection_runs MODIFY items_found INTEGER NOT NULL;

ALTER TABLE raw_collection_runs MODIFY hit_cap BOOL NOT NULL;

-- Etapa 0003: analise LLM

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

-- Etapa 0004: painel

ALTER TABLE raw_articles ADD COLUMN origem VARCHAR(16) NOT NULL DEFAULT 'google_news';

ALTER TABLE raw_articles ADD COLUMN published_precision VARCHAR(8) NOT NULL DEFAULT 'datetime';

CREATE INDEX ix_articles_origem ON raw_articles (origem);

CREATE TABLE painel_comandos (
    id VARCHAR(36) NOT NULL, 
    tipo VARCHAR(16) NOT NULL, 
    parametros JSON NOT NULL, 
    criado_em DATETIME(6) NOT NULL, 
    ack_em DATETIME(6), 
    ok BOOL, 
    erro TEXT, 
    CONSTRAINT pk_painel_comandos PRIMARY KEY (id)
);

CREATE INDEX ix_painel_comandos_ack_em ON painel_comandos (ack_em);

CREATE TABLE painel_heartbeat (
    host VARCHAR(64) NOT NULL, 
    versao VARCHAR(32) NOT NULL, 
    ocupado_com VARCHAR(16), 
    visto_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_painel_heartbeat PRIMARY KEY (host)
);

CREATE TABLE painel_execucoes (
    id VARCHAR(48) NOT NULL, 
    tipo VARCHAR(12) NOT NULL, 
    etapa_alvo VARCHAR(24), 
    origem VARCHAR(8) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    aguardando VARCHAR(16), 
    iniciada_em DATETIME(6) NOT NULL, 
    finalizada_em DATETIME(6), 
    prazo_em DATETIME(6) NOT NULL, 
    erro TEXT, 
    atualizada_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_painel_execucoes PRIMARY KEY (id)
);

CREATE INDEX ix_painel_execucoes_iniciada_em ON painel_execucoes (iniciada_em);

CREATE TABLE painel_etapas (
    execucao_id VARCHAR(48) NOT NULL, 
    etapa_id VARCHAR(24) NOT NULL, 
    ordem SMALLINT NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    unidades_total INTEGER, 
    unidades_feitas INTEGER NOT NULL DEFAULT '0', 
    unidades_erro INTEGER NOT NULL DEFAULT '0', 
    unidades_saturadas INTEGER NOT NULL DEFAULT '0', 
    itens INTEGER NOT NULL DEFAULT '0', 
    iniciada_em DATETIME(6), 
    finalizada_em DATETIME(6), 
    ultimo_erro_unidade VARCHAR(128), 
    ultimo_erro_mensagem TEXT, 
    atualizada_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_painel_etapas PRIMARY KEY (execucao_id, etapa_id), 
    CONSTRAINT fk_painel_etapas_execucao_id FOREIGN KEY(execucao_id) REFERENCES painel_execucoes (id) ON DELETE CASCADE
);

CREATE TABLE painel_historico (
    id VARCHAR(32) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    modo VARCHAR(24) NOT NULL, 
    anos JSON NOT NULL, 
    ano_atual INTEGER, 
    coletor_atual VARCHAR(32), 
    unidade_atual VARCHAR(128), 
    iniciada_em DATETIME(6) NOT NULL, 
    finalizada_em DATETIME(6), 
    itens_por_min DOUBLE NOT NULL DEFAULT '0', 
    erro TEXT, 
    atualizada_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_painel_historico PRIMARY KEY (id)
);

CREATE TABLE painel_historico_progresso (
    ano INTEGER NOT NULL, 
    coletor VARCHAR(32) NOT NULL, 
    unidades_total INTEGER NOT NULL DEFAULT '0', 
    unidades_feitas INTEGER NOT NULL DEFAULT '0', 
    unidades_erro INTEGER NOT NULL DEFAULT '0', 
    itens INTEGER NOT NULL DEFAULT '0', 
    status_segmento VARCHAR(16) NOT NULL, 
    atualizado_em DATETIME(6) NOT NULL, 
    CONSTRAINT pk_painel_historico_progresso PRIMARY KEY (ano, coletor)
);

CREATE TABLE painel_logs (
    id BIGINT NOT NULL AUTO_INCREMENT, 
    ts DATETIME(6) NOT NULL, 
    nivel VARCHAR(8) NOT NULL, 
    origem VARCHAR(16) NOT NULL, 
    etapa VARCHAR(24), 
    mensagem TEXT NOT NULL, 
    CONSTRAINT pk_painel_logs PRIMARY KEY (id)
);

CREATE INDEX ix_painel_logs_ts ON painel_logs (ts);

CREATE TABLE painel_comandos_aplicados (
    id VARCHAR(36) NOT NULL, 
    aplicado_em DATETIME(6) NOT NULL, 
    ok BOOL NOT NULL, 
    erro TEXT, 
    CONSTRAINT pk_painel_comandos_aplicados PRIMARY KEY (id)
);

-- Etapa 0005: fontes e juiza

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

-- Etapa 0006: entrega

CREATE TABLE noticias (
    id VARCHAR(16) NOT NULL, 
    ticker VARCHAR(12) NOT NULL, 
    data_hora_publicacao VARCHAR(32), 
    data_hora_coleta VARCHAR(32) NOT NULL, 
    titulo TEXT NOT NULL, 
    fonte VARCHAR(255), 
    link TEXT, 
    prob_positivo DOUBLE, 
    prob_neutro DOUBLE, 
    prob_negativo DOUBLE, 
    data_publicacao DATE NOT NULL, 
    momento_pregao VARCHAR(16) NOT NULL, 
    origem VARCHAR(16) NOT NULL, 
    papel VARCHAR(24), 
    tipo_conteudo VARCHAR(24), 
    evento VARCHAR(24), 
    qualidade DOUBLE, 
    CONSTRAINT pk_noticias PRIMARY KEY (id, ticker)
);

CREATE INDEX ix_noticias_ticker_data_publicacao ON noticias (ticker, data_publicacao);

CREATE TABLE precos (
    data DATE NOT NULL, 
    ticker VARCHAR(12) NOT NULL, 
    abertura DOUBLE, 
    maxima DOUBLE, 
    minima DOUBLE, 
    fechamento DOUBLE, 
    fechamento_ajustado DOUBLE, 
    volume BIGINT, 
    CONSTRAINT pk_precos PRIMARY KEY (data, ticker)
);

CREATE INDEX ix_precos_ticker ON precos (ticker);

CREATE TABLE cadastro (
    ticker VARCHAR(12) NOT NULL, 
    cnpj VARCHAR(14), 
    empresa VARCHAR(128) NOT NULL, 
    setor VARCHAR(128), 
    subsetor VARCHAR(128), 
    tipo_acao VARCHAR(8) NOT NULL, 
    unit_qtd_on SMALLINT, 
    unit_qtd_pn SMALLINT, 
    CONSTRAINT pk_cadastro PRIMARY KEY (ticker)
);

CREATE TABLE mudancas_ticker (
    ticker_antigo VARCHAR(12) NOT NULL, 
    ticker_novo VARCHAR(12) NOT NULL, 
    data_mudanca DATE, 
    CONSTRAINT pk_mudancas_ticker PRIMARY KEY (ticker_antigo)
);

CREATE TABLE balancos (
    cnpj VARCHAR(14) NOT NULL, 
    tipo_documento VARCHAR(3) NOT NULL, 
    data_inicio_periodo DATE NOT NULL, 
    data_fim_periodo DATE NOT NULL, 
    data_entrega DATE, 
    receita_liquida DOUBLE, 
    lucro_bruto DOUBLE, 
    lucro_liquido DOUBLE, 
    patrimonio_liquido DOUBLE,
    ebit DOUBLE,
    depreciacao_amortizacao DOUBLE,
    emprestimos_curto_prazo DOUBLE,
    emprestimos_longo_prazo DOUBLE,
    caixa DOUBLE,
    aplicacoes_financeiras DOUBLE,
    CONSTRAINT pk_balancos PRIMARY KEY (cnpj, tipo_documento, data_inicio_periodo, data_fim_periodo)
);

CREATE TABLE acoes_emitidas (
    cnpj VARCHAR(14) NOT NULL, 
    data_referencia DATE NOT NULL, 
    qtd_on BIGINT, 
    qtd_pn BIGINT, 
    CONSTRAINT pk_acoes_emitidas PRIMARY KEY (cnpj, data_referencia)
);

CREATE TABLE proventos (
    ticker VARCHAR(12) NOT NULL, 
    tipo VARCHAR(10) NOT NULL, 
    data_com DATE NOT NULL, 
    valor_por_acao DOUBLE NOT NULL, 
    data_pagamento DATE, 
    CONSTRAINT pk_proventos PRIMARY KEY (ticker, tipo, data_com)
);

CREATE INDEX ix_proventos_ticker ON proventos (ticker);

CREATE TABLE indices (
    data DATE NOT NULL, 
    indice VARCHAR(8) NOT NULL, 
    valor DOUBLE NOT NULL, 
    CONSTRAINT pk_indices PRIMARY KEY (data, indice)
);

CREATE INDEX ix_indices_indice ON indices (indice);

-- Etapa 0007: IPE larguras

ALTER TABLE raw_ipe_docs MODIFY categoria VARCHAR(128) NOT NULL;

ALTER TABLE raw_ipe_docs MODIFY tipo VARCHAR(255) NULL;

ALTER TABLE raw_ipe_docs MODIFY especie VARCHAR(255) NULL;

ALTER TABLE raw_ipe_docs MODIFY tipo_apresentacao VARCHAR(64) NULL;

-- Etapa 0008: prefixo v1-

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

-- Etapa 0009: juiza com custo, finalidade e tentativas (ver sql/alteracoes/2026-10-23_juiza_custo_tentativas)

ALTER TABLE `v1-raw_llm_juiza_runs`
    ADD COLUMN custo_usd DOUBLE NULL AFTER tokens_saida,
    ADD COLUMN finalidade VARCHAR(16) NOT NULL DEFAULT 'producao' AFTER status;

CREATE INDEX ix_raw_llm_juiza_runs_finalidade ON `v1-raw_llm_juiza_runs` (finalidade, iniciado_em);

CREATE TABLE `v1-staging_juiza_tentativas` (
    article_id BIGINT NOT NULL,
    ticker VARCHAR(12) NOT NULL,
    prompt_versao INTEGER NOT NULL,
    tentativas INTEGER NOT NULL,
    ultimo_status VARCHAR(16) NOT NULL,
    ultimo_erro TEXT,
    atualizado_em DATETIME(6) NOT NULL,
    CONSTRAINT pk_staging_juiza_tentativas PRIMARY KEY (article_id, ticker, prompt_versao),
    CONSTRAINT fk_staging_juiza_tentativas_article_id FOREIGN KEY(article_id) REFERENCES raw_articles (id) ON DELETE CASCADE
);

-- Etapa 0010: composicao do IBOV por data (ver sql/alteracoes/2026-10-23_composicao_ibov)

CREATE TABLE `v1-composicao_ibov` (
    ticker VARCHAR(12) NOT NULL,
    vigencia_inicio DATE NOT NULL,
    vigencia_fim DATE NULL,
    participacao DOUBLE NULL,
    fonte VARCHAR(32) NOT NULL,
    CONSTRAINT pk_composicao_ibov PRIMARY KEY (ticker, vigencia_inicio)
);

-- Etapa 0011: coleta hibrida WordPress (ver sql/alteracoes/2026-10-27_wordpress_historias)

CREATE TABLE `v1-raw_wp_posts` (
    site VARCHAR(16) NOT NULL,
    wp_id BIGINT NOT NULL,
    link TEXT NOT NULL,
    date_gmt DATETIME(6) NOT NULL,
    wp_type VARCHAR(32) NOT NULL,
    titulo_html TEXT NOT NULL,
    content_html LONGTEXT NOT NULL,
    fetched_at DATETIME(6) NOT NULL,
    CONSTRAINT pk_raw_wp_posts PRIMARY KEY (site, wp_id)
);

CREATE INDEX ix_raw_wp_posts_date_gmt ON `v1-raw_wp_posts` (date_gmt);

CREATE TABLE `v1-staging_historias` (
    article_id BIGINT NOT NULL,
    historia_article_id BIGINT NOT NULL,
    metodo VARCHAR(16) NOT NULL,
    regras_versao INTEGER NOT NULL,
    atualizado_em DATETIME(6) NOT NULL,
    CONSTRAINT pk_staging_historias PRIMARY KEY (article_id),
    CONSTRAINT fk_staging_historias_article_id FOREIGN KEY(article_id) REFERENCES raw_articles (id) ON DELETE CASCADE,
    CONSTRAINT fk_staging_historias_historia_article_id FOREIGN KEY(historia_article_id) REFERENCES raw_articles (id)
);

CREATE INDEX ix_staging_historias_historia_article_id ON `v1-staging_historias` (historia_article_id);

CREATE TABLE `v1-staging_wp_processamento` (
    site VARCHAR(16) NOT NULL,
    wp_id BIGINT NOT NULL,
    regras_versao INTEGER NOT NULL,
    prompt_versao INTEGER NOT NULL,
    article_id BIGINT NULL,
    papel_historia VARCHAR(16) NOT NULL,
    n_pares INTEGER NOT NULL,
    processado_em DATETIME(6) NOT NULL,
    CONSTRAINT pk_staging_wp_processamento PRIMARY KEY (site, wp_id),
    CONSTRAINT fk_staging_wp_processamento_raw_wp_posts FOREIGN KEY(site, wp_id) REFERENCES `v1-raw_wp_posts` (site, wp_id),
    CONSTRAINT fk_staging_wp_processamento_article_id FOREIGN KEY(article_id) REFERENCES raw_articles (id)
);

-- Etapa 0012: unidade em curso e ultimo erro por (ano, coletor) da carga historica (ver sql/alteracoes/2026-10-27_historico_progresso_unidade_erro)

ALTER TABLE `v1-painel_historico_progresso`
    ADD COLUMN unidade_atual VARCHAR(128) NULL AFTER status_segmento,
    ADD COLUMN ultimo_erro_unidade VARCHAR(128) NULL AFTER unidade_atual,
    ADD COLUMN ultimo_erro_mensagem TEXT NULL AFTER ultimo_erro_unidade;

