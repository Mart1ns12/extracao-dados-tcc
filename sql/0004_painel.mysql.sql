-- Migration 0004 (painel de acompanhamento) para MySQL 8 -- gerado por:
--   alembic upgrade 0003:0004 --sql   (dialeto MySQL, modo offline)
--
-- Pre-requisito: o banco ja esta na 0003. Confira antes de rodar:
--   SELECT version_num FROM alembic_version;   -- deve devolver '0003'
-- Banco vazio? Use completo_ate_0003.mysql.sql (cria tudo, 0001 -> 0003) e depois esta.
--
-- As tabelas herdam o charset do database (deve ser utf8mb4; ver docs/MIGRACAO-MYSQL.md).
-- DDL no MySQL nao e transacional: se falhar no meio, as tabelas ja criadas ficam.

-- Running upgrade 0003 -> 0004

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

UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003';

