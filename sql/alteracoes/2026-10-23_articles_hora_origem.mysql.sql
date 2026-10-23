-- 2026-10-23: `raw_articles.published_time_source`, de onde veio a hora da publicacao.
-- O RSS do Google News traz so a data; a hora que vem junto e 00:00 do fuso de Los Angeles
-- (published_precision='date'). Quando a hora e real, a origem fica registrada aqui:
-- 'rss' (pubDate com hora) ou 'pagina' (meta/JSON-LD/<time> do HTML, via enrich).
-- NULL quando published_precision='date'. Linhas antigas ficam NULL: a origem nao e conhecida.
-- Depende da coluna published_precision ja existir no banco.
-- Aplicar à mão, com usuário com DDL e aprovação do dono.
ALTER TABLE raw_articles
    ADD COLUMN published_time_source VARCHAR(8) NULL AFTER published_precision;
