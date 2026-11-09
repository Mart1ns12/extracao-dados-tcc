-- 2026-11-09: demonstracoes individuais (_ind_) da CVM em v1-raw_cvm_balancos.
--   consolidacao: 'con' (consolidada, tudo o que ja existia) ou 'ind' (individual, da controladora).
--   Entra na chave primaria: o mesmo documento e a mesma conta existem nas duas versoes.
--   DEFAULT 'con' cobre as ~148 mil linhas ja gravadas (todas vieram dos CSVs _con_) e mantem o
--   codigo antigo funcionando. Aplicar a mao, com usuario com DDL e aprovacao do dono, ANTES de
--   implantar o codigo novo.
ALTER TABLE `v1-raw_cvm_balancos`
    ADD COLUMN consolidacao VARCHAR(3) NOT NULL DEFAULT 'con' AFTER versao,
    DROP PRIMARY KEY,
    ADD PRIMARY KEY (cnpj, tipo_documento, dt_refer, versao, consolidacao, demonstracao, cd_conta,
                     dt_ini_exerc, dt_fim_exerc),
    ADD CONSTRAINT ck_raw_cvm_balancos_consolidacao CHECK (consolidacao IN ('con', 'ind'));
