-- 2026-10-23: a juiza passa a julgar 100% dos pares. Motivos:
--   custo_usd e finalidade em `v1-raw_llm_juiza_runs`: teto mensal em US$ e verba do backlog
--   sao somados a partir dessas colunas (finalidade = producao | backlog | concordancia).
--   `v1-staging_juiza_tentativas`: conta as tentativas por par (artigo, ticker, versao do prompt),
--   fora da staging de julgamentos, onde "linha existe" quer dizer "julgado".
-- Aplicar à mão, com usuário com DDL e aprovação do dono.
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
    CONSTRAINT fk_staging_juiza_tentativas_article_id FOREIGN KEY(article_id)
        REFERENCES raw_articles (id) ON DELETE CASCADE
);
