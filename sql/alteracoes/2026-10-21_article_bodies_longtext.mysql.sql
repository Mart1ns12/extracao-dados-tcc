-- 2026-10-21: `raw_article_bodies.body_text` de TEXT (64 KB) para LONGTEXT (4 GB).
-- Paginas longas passavam do limite e o MySQL recusava o lote inteiro de ~200 corpos
-- ("Data too long for column 'body_text'"), travando as unidades do enriquecimento.
-- Aplicar à mão, com usuário com DDL e aprovação do dono. Depois, refazer o enriquecimento
-- (refazer=true) para reprocessar os lotes que ficaram com erro.
ALTER TABLE raw_article_bodies
    MODIFY COLUMN body_text LONGTEXT NOT NULL;
