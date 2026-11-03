-- 2026-11-03: situacao de cada balanco na entrega (tabela v1-balancos, prefixo v1-).
--   ok: linha publicada como veio da CVM.
--   ajuste_ifrs17: a companhia publicou despesa de seguros dentro da 3.01 (receita liquida);
--     essa despesa foi tirada da receita (ver _despesa_de_seguro_na_receita em entrega/builder.py).
--   reapresentado: a receita original foi zerada e substituida pelo comparativo reapresentado
--     de uma remessa posterior (ver substituicoes em montar_balancos).
-- DEFAULT 'ok' cobre as linhas que ja existem. Aplicar à mão, com usuário com DDL e aprovação do dono.

ALTER TABLE `v1-balancos`
    ADD COLUMN situacao VARCHAR(16) NOT NULL DEFAULT 'ok' AFTER aplicacoes_financeiras;
