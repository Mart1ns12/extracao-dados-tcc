-- Migration 0007 (alarga colunas de texto de raw_ipe_docs que estouravam com dados da CVM) para MySQL 8 -- gerado por:
--   alembic upgrade 0006:0007 --sql   (dialeto MySQL, modo offline, DB_TARGET=mysql)
--
-- Pre-requisito: o banco ja esta na 0006. Confira antes de rodar:
--   SELECT version_num FROM alembic_version;   -- deve devolver '0006'
--
-- ALTER TABLE no MySQL nao e transacional: se falhar no meio, as colunas ja alteradas ficam.
-- Nenhuma linha e reescrita: so o tamanho maximo das colunas muda.

-- Running upgrade 0006 -> 0007

ALTER TABLE raw_ipe_docs MODIFY categoria VARCHAR(128) NOT NULL;

ALTER TABLE raw_ipe_docs MODIFY tipo VARCHAR(255) NULL;

ALTER TABLE raw_ipe_docs MODIFY especie VARCHAR(255) NULL;

ALTER TABLE raw_ipe_docs MODIFY tipo_apresentacao VARCHAR(64) NULL;

UPDATE alembic_version SET version_num='0007' WHERE alembic_version.version_num = '0006';

