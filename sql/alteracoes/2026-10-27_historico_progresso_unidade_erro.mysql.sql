-- 2026-10-27: unidade em curso e ultimo erro por (ano, coletor) da carga historica.
--   Com as duas trilhas paralelas (Google e principal), a posicao unica de `v1-painel_historico`
--   deixa de bastar: cada linha de progresso carrega a sua unidade e o seu ultimo erro.
-- Aplicar à mão, com usuário com DDL e aprovação do dono, ANTES de publicar o scraper e a API.
ALTER TABLE `v1-painel_historico_progresso`
    ADD COLUMN unidade_atual VARCHAR(128) NULL AFTER status_segmento,
    ADD COLUMN ultimo_erro_unidade VARCHAR(128) NULL AFTER unidade_atual,
    ADD COLUMN ultimo_erro_mensagem TEXT NULL AFTER ultimo_erro_unidade;
