-- 2026-10-27: coleta hibrida de noticias (WordPress + Google News), tabelas com prefixo v1-.
--   v1-raw_wp_posts: posts dos sites WordPress (InfoMoney, Money Times, Exame), com o HTML bruto.
--   v1-staging_historias: historia (republicacao entre veiculos) a que cada artigo pertence.
--   v1-staging_wp_processamento: processamento de cada post WordPress por versao de regras e prompt.
-- So CREATE: nenhuma coluna existente muda. Sem clausula de engine/charset, como no schema.
-- Aplicar à mão, com usuário com DDL e aprovação do dono.

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
