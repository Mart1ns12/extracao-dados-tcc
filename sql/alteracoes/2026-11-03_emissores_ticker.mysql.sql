-- 2026-11-03: emissor de cada ticker por periodo (tabela da entrega, prefixo v1-).
--   v1-emissores_ticker: uma linha por (ticker, ordem). JBSS32 e NATU3 trocam de companhia no
--   tempo (ver EMISSORES_POR_PERIODO em config.py); sem periodo explicito, o ticker tem uma linha so.
-- Aplicar à mão, com usuário com DDL e aprovação do dono.
CREATE TABLE `v1-emissores_ticker` (
    ticker VARCHAR(12) NOT NULL,
    ordem SMALLINT NOT NULL,
    ticker_negociado VARCHAR(12) NOT NULL,
    cd_cvm INTEGER NULL,
    cnpj VARCHAR(14) NULL,
    empresa VARCHAR(128) NULL,
    vigencia_inicio DATE NULL,
    vigencia_fim DATE NULL,
    fator_acoes DOUBLE NOT NULL,
    CONSTRAINT pk_emissores_ticker PRIMARY KEY (ticker, ordem)
);

CREATE INDEX ix_emissores_ticker_cnpj ON `v1-emissores_ticker` (cnpj);
