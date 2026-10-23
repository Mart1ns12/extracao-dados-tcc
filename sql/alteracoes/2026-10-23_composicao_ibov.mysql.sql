-- 2026-10-23: composicao do IBOV por data (tabela da entrega, prefixo v1-).
--   Cada linha e um periodo continuo em que o papel esteve no indice. vigencia_fim NULL = vigente.
--   fonte: paccafelipe_csv (carteira oficial), rebalanceamento (eventos de set/2024 em diante)
--   ou b3_api (mudanca vista pelo coletor b3_carteira_ibov).
-- Aplicar à mão, com usuário com DDL e aprovação do dono.
CREATE TABLE `v1-composicao_ibov` (
    ticker VARCHAR(12) NOT NULL,
    vigencia_inicio DATE NOT NULL,
    vigencia_fim DATE NULL,
    participacao DOUBLE NULL,
    fonte VARCHAR(32) NOT NULL,
    CONSTRAINT pk_composicao_ibov PRIMARY KEY (ticker, vigencia_inicio)
);
