-- 2026-10-20: seis colunas novas em `v1-balancos` (contas CVM: DRE 3.05, DFC 6.01.01.NN,
-- BPP 2.01.04.01/.02 e 2.02.01.01/.02, BPA 1.01.01 e 1.01.02). Valores em reais, NULL = em branco.
-- Aplicar à mão, com usuário com DDL e aprovação do dono, ANTES do deploy do código novo
-- (o build-entrega grava as colunas novas). A tabela raw `v1-raw_cvm_balancos` não muda.
ALTER TABLE `v1-balancos`
    ADD COLUMN ebit DOUBLE NULL AFTER patrimonio_liquido,
    ADD COLUMN depreciacao_amortizacao DOUBLE NULL AFTER ebit,
    ADD COLUMN emprestimos_curto_prazo DOUBLE NULL AFTER depreciacao_amortizacao,
    ADD COLUMN emprestimos_longo_prazo DOUBLE NULL AFTER emprestimos_curto_prazo,
    ADD COLUMN caixa DOUBLE NULL AFTER emprestimos_longo_prazo,
    ADD COLUMN aplicacoes_financeiras DOUBLE NULL AFTER caixa;
