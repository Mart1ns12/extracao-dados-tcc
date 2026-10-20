-- Migration 0006 (entrega para a etapa seguinte: 8 tabelas sem prefixo) para MySQL 8 -- gerado por:
--   alembic upgrade 0005:0006 --sql   (dialeto MySQL, modo offline, DB_TARGET=mysql)
--
-- Pre-requisito: o banco ja esta na 0005. Confira antes de rodar:
--   SELECT version_num FROM alembic_version;   -- deve devolver '0005'
--
-- As tabelas herdam o charset do database (deve ser utf8mb4; ver docs/MIGRACAO-MYSQL.md).
-- DDL no MySQL nao e transacional: se falhar no meio, as tabelas ja criadas ficam.

-- Running upgrade 0005 -> 0006

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

UPDATE alembic_version SET version_num='0006' WHERE alembic_version.version_num = '0005';

