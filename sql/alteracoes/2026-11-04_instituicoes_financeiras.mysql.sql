-- 2026-11-04: bancos e seguradoras (codigo CVM) numa tabela de referencia, camada ref_.
--   Antes era CD_CVM_BANCOS_SEGURADORAS em config.py. Fonte unica para o build-entrega (balancos:
--   lucro bruto, EBIT, D&A, emprestimos, caixa e aplicacoes em branco) e para o indicador
--   balancos_contas_novas do painel. Incluir um banco novo = novo arquivo em sql/alteracoes/.
-- Aplicar à mão, com usuário com DDL e aprovação do dono, ANTES do deploy do código novo
-- (sem a tabela, ou com ela vazia, a montagem de balancos falha).
CREATE TABLE `v1-ref_instituicoes_financeiras` (
    cd_cvm INTEGER NOT NULL,
    empresa VARCHAR(128) NOT NULL,
    tipo VARCHAR(16) NOT NULL,
    motivo VARCHAR(255) NOT NULL,
    incluido_em DATE NOT NULL,
    CONSTRAINT pk_ref_instituicoes_financeiras PRIMARY KEY (cd_cvm),
    CONSTRAINT ck_ref_instituicoes_financeiras_tipo CHECK (tipo IN ('banco', 'seguradora'))
);

INSERT INTO `v1-ref_instituicoes_financeiras` (cd_cvm, empresa, tipo, motivo, incluido_em) VALUES
    (906, 'Bradesco (BBDC3, BBDC4)', 'banco', 'Plano de contas de banco da CVM, sem lucro bruto, EBIT nem caixa do plano industrial', '2026-10-20'),
    (1023, 'Banco do Brasil (BBAS3)', 'banco', 'Plano de contas de banco da CVM, sem lucro bruto, EBIT nem caixa do plano industrial', '2026-10-20'),
    (19348, 'Itau Unibanco (ITUB4)', 'banco', 'Plano de contas de banco da CVM, sem 1.01.01 e 1.01.02 no BPA', '2026-10-20'),
    (20532, 'Santander (SANB11)', 'banco', 'Plano de contas de banco da CVM, sem 1.01.01 e 1.01.02 no BPA ate 2025-03', '2026-10-20'),
    (22616, 'BTG Pactual (BPAC11)', 'banco', 'Plano de contas de banco da CVM, sem 1.01.01 e 1.01.02 no BPA', '2026-10-20'),
    (23159, 'BB Seguridade (BBSE3)', 'seguradora', 'Tem Caixa e Equivalentes de Caixa como a industria, mas a spec deixa em branco', '2026-10-20'),
    (16659, 'Porto Seguro (PSSA3)', 'seguradora', 'Usa o plano industrial inteiro (ITR 2025 da CVM), a descricao nao separa', '2026-10-20'),
    (24180, 'IRB Brasil RE (IRBR3)', 'seguradora', 'Resseguradora com Caixa e Equivalentes de Caixa como a industria, a spec deixa em branco', '2026-10-20');
