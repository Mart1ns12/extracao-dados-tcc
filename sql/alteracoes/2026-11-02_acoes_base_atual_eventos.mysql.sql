-- 2026-11-02: quantidade de acoes na base atual e eventos societarios em acoes (tabelas da entrega, prefixo v1-).
--   v1-acoes_emitidas: ganha data_entrega (data de recebimento da CVM), qtd_on/qtd_pn_base_atual
--   (quantidade ja ajustada pelos eventos posteriores) e situacao (ok por padrao).
--   v1-eventos_acoes: desdobramentos, grupamentos e bonificacoes; fator multiplica a quantidade anterior.
-- Aplicar à mão, com usuário com DDL e aprovação do dono.
ALTER TABLE `v1-acoes_emitidas`
    ADD COLUMN data_entrega DATE NULL AFTER qtd_pn,
    ADD COLUMN qtd_on_base_atual BIGINT NULL AFTER data_entrega,
    ADD COLUMN qtd_pn_base_atual BIGINT NULL AFTER qtd_on_base_atual,
    ADD COLUMN situacao VARCHAR(16) NOT NULL DEFAULT 'ok' AFTER qtd_pn_base_atual;

CREATE TABLE `v1-eventos_acoes` (
    ticker VARCHAR(12) NOT NULL,
    data_com DATE NOT NULL,
    tipo VARCHAR(16) NOT NULL,
    fator DOUBLE NOT NULL,
    isin VARCHAR(12) NOT NULL,
    isin_emitido VARCHAR(12) NOT NULL,
    cnpj VARCHAR(14) NULL,
    data_aprovacao DATE NULL,
    CONSTRAINT pk_eventos_acoes PRIMARY KEY (ticker, data_com, tipo, isin_emitido)
);

CREATE INDEX ix_eventos_acoes_cnpj ON `v1-eventos_acoes` (cnpj);
