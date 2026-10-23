-- Lote de 2026-10-23: junta, na ordem, as tres alteracoes de sql/alteracoes/2026-10-23_*.
-- Aplicar a mao, com usuario com DDL e aprovacao do dono, no banco do pipeline.
-- DDL do MySQL nao tem rollback: rode uma vez so. Se um bloco falhar por ja existir, siga para o
-- proximo; a conferencia do fim mostra o que ficou aplicado.
-- Pre-requisitos (ja aplicados antes): raw_articles.published_precision existe;
-- 2026-10-20_balancos_contas_novas e 2026-10-21_article_bodies_longtext ja rodaram.

-- 1/3: 2026-10-23_articles_hora_origem -- de onde veio a hora da publicacao ('rss' | 'pagina').
ALTER TABLE raw_articles
    ADD COLUMN published_time_source VARCHAR(8) NULL AFTER published_precision;

-- 2/3: 2026-10-23_juiza_custo_tentativas -- custo e finalidade das runs; tentativas por par.
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

-- 3/3: 2026-10-23_composicao_ibov -- composicao do IBOV por data (tabela da entrega).
CREATE TABLE `v1-composicao_ibov` (
    ticker VARCHAR(12) NOT NULL,
    vigencia_inicio DATE NOT NULL,
    vigencia_fim DATE NULL,
    participacao DOUBLE NULL,
    fonte VARCHAR(32) NOT NULL,
    CONSTRAINT pk_composicao_ibov PRIMARY KEY (ticker, vigencia_inicio)
);

-- Conferencia: deve devolver 5 linhas (3 colunas e 2 tabelas).
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND (
        (table_name = 'raw_articles' AND column_name = 'published_time_source')
     OR (table_name = 'v1-raw_llm_juiza_runs' AND column_name IN ('custo_usd', 'finalidade'))
     OR (table_name IN ('v1-staging_juiza_tentativas', 'v1-composicao_ibov')
         AND column_name IN ('tentativas', 'vigencia_inicio'))
  )
ORDER BY table_name, column_name;
