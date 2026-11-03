# Plano: calendário de resultados (CVM IPE) e anúncios de proventos (B3)

Data: 2026-10-29. Autor: arquiteto. Aprovado pelo dono: dependência `pypdf` e tabelas novas (DDL aplicada à mão pelo dono).

Como usar: cada tarefa abaixo é **um despacho** para o implementador. Mande no prompt a seção
"Regras comuns" + a tarefa inteira. Nenhuma tarefa precisa ler este plano todo.

---

## 0. Decisões e evidências (resumo para o orquestrador)

### Frente A — datas futuras de divulgação de balanço

- **Caminho próprio, NÃO entra em `IPE_CATEGORIAS`.** O coletor `cvm_ipe` grava todo documento
  como matéria em `raw_articles` com `origem='cvm_ipe'` (`collectors/cvm_ipe.py:191-207`). Essa origem
  entra na entrega sem juíza (`entrega/filtros.py:35`) e a juíza a rotula como `fato_relevante`
  protagonista sem LLM (`llm/juiza_regras.py:93`). Pôr o calendário ali faria ~500 PDFs/ano virarem
  "notícia" em `v1-noticias`. Por isso: coletor novo `cvm_calendario`, que lê o mesmo zip do IPE.
- **Spike do pypdf: FEITO pelo arquiteto, passou.** pypdf 6.21.4 extraiu texto limpo dos 3 PDFs
  (PETR, VALE, ITUB) e de mais 14 PDFs de amostra (SMTO3, SBSP3, HYPE3, BRKM5, ITSA4, LWSA3, GOAU4,
  MULT3, RADL3, HAPV3, COGN3, GGBR4, POMO4, ALOS3). O parser da tarefa A2 achou DFP + 3 ITR em 17/17.
  A decodificação manual (glifo = byte+29) **não** será usada: depende da fonte embutida e só foi vista
  em 3 PDFs. **Fallback:** se o pypdf não render texto, o PDF fica gravado no raw com `texto = NULL`,
  sem eventos, e aparece no relatório de cobertura (`pdfs_sem_texto`); reprocessável depois sem rede.
  Nada de OCR nem dependência extra sem nova aprovação.
- **O RAD aceita o User-Agent do projeto** (`TCC-Scraper/1.0 ...`): HTTP 200, Content-Type `text/html`,
  corpo `%PDF-`, sem redirecionar para outro domínio (testado em 2026-10-29).
- **Cobertura medida no CSV (sem baixar PDF):** dos 85 tickers do IBOV (83 companhias), 84 têm
  calendário com referência 2026 somando os zips de 2025 e 2026. Falta só **SANB11** (cd_cvm 20532,
  nenhum calendário em nenhum dos dois anos). Só com o zip de 2026, seriam 63 de 83 companhias: o
  calendário de 2026 de 20 companhias foi entregue em dez/2025. **Por isso o daily lê `[ano-1, ano]`.**
- **Pegadinha do ano fiscal:** SMTO3 (São Martinho) fecha o exercício em 31/03. O período do "1º
  trimestre" é contado a partir do `findo em` da DFP, não do ano civil (já tratado no parser).
- **Limitação a documentar:** entre a divulgação do 3T e a entrega do calendário do ano seguinte
  (dez a jan), a companhia fica sem data futura de ITR/DFP. Não é falha do coletor.
- **Daily:** vira a 5ª parte da etapa 7 `cadastro` ("Cadastro e eventos", `orquestrador/etapas.py:185-204`),
  sem etapa nova (ids/nome/ordem das 9 etapas são contrato com o painel, `etapas.py:3`). Não entra em
  `etapa_ipe` para não esconder falha do IPE de notícias (em `_executa_partes` a etapa só falha se
  todas as partes falharem, `etapas.py:156`).
- **Entrega: recomendo NÃO entrar agora.** As 9 tabelas da entrega são contrato com a etapa seguinte
  (`docs/spec-entrega-etapa-seguinte.md`). O dado fica em `v1-staging_calendario_eventos`, com a função
  `eventos_vigentes(eventos, conhecido_em)` que só usa versões com `data_entrega <= t` (sem vazamento).
  Se o dono quiser na entrega ou como feature ("dias até a próxima divulgação"), é uma tarefa nova
  em cima dessa função.
- **Catálogo do painel (`orquestrador/coletores.py`):** fora deste plano (o painel web teria de
  conhecer o id novo). O coletor roda pelo daily e pela CLI.

### Frente B — proventos

- **(3) `collect-proventos` já roda no daily:** parte `b3_proventos` da etapa `cadastro`
  (`etapas.py:196`). Confirmado no banco: `raw_collection_runs` tem 85 unidades `ok` de `b3_proventos`
  hoje (2026-10-29 19:59 UTC). Nada a fazer.
- **(1) Sentinela 31/12/9999 vira NULL dentro de `data_b3()`** (`collectors/b3_base.py:55-62`), não no
  builder. Motivo: os chamadores querem data real, e o sentinela é pior fora do pagamento. Em
  `montar_proventos` um desdobramento com `lastDatePrior` 31/12/9999 seria aplicado a TODOS os
  proventos passados (`builder.py:952-955`: `data_evento <= data_com` nunca é verdade). Chamadores:
  `b3_cotahist.py:137` (grupamento sem data passa a levantar o `ValueError` "grupamento invalido" que
  já existe, em vez de aplicar fator a tudo: desejado), `b3_proventos.py:188-189` (raw: linhas novas
  gravam NULL; as antigas ficam), `builder.py:891, 898, 952`. As 3 linhas da entrega com 9999 (EGIE3
  2026-08-20, EMBJ3 2025-12-15, HYPE3 2025-12-26) viram NULL na próxima montagem: o `_gravar` faz
  upsert sobrescrevendo `data_pagamento` (`builder.py:1222-1229`).
- **(2) Medido no banco (SELECT):** o suplemento NÃO tem data-com futura hoje (máx. `lastDatePrior` =
  2026-10-26). Mas o `GetListedCashDividends` atrasa: máx. `last_date_prior_ex` = 2026-09-30 e máx.
  `date_approval` = 2026-09-25 em `v1-raw_b3_proventos`, mesmo coletado hoje. Há **18 eventos
  DIVIDENDO/JCP no suplemento com data-com de 2026-10-01 a 2026-10-26** (BBDC, RADL, ISAE, IGTI, SANB,
  ALOS, VIVT) que **somem da entrega** porque `montar_proventos` parte só de `raw_b3_proventos`
  (`builder.py:929`). Plano: incluir esses "anúncios" (cerca de 15 linhas novas hoje). O mesmo caminho
  pega anúncio com data-com futura se a B3 publicar.
- **Regra dos anúncios (sem duplicar, sem quebrar a PK):** entra evento do suplemento com data-com
  **depois** da maior data-com de `raw_b3_proventos` (o "corte"). Como nenhum provento do histórico
  passa do corte, a chave `(ticker, tipo, data_com)` do anúncio nunca colide com a do histórico;
  quando o evento chegar ao `GetListedCashDividends`, o corte avança, o anúncio sai do caminho
  "anúncio" e a mesma chave é regravada pelo histórico (upsert, sem duplicar). Antes do corte, evento
  sem par é divergência de cadastro, não anúncio: fica de fora.
- **Pegadinha medida: versões e parcelas no suplemento.** O raw é append-only por hash do payload:
  quando a B3 revisa um evento, a versão antiga fica. Ex.: IGTI 15/10/2026 tem taxa 0,02410116025
  (coletada em 10-20) e 0,02412606561 (10-22); somar daria o dobro. E 22 eventos aparecem com vários
  `paymentDate` e a mesma taxa (parcelas, ex.: KLBN). Regra: por `(ticker, tipo, data_com)`, vale só o
  retrato mais recente (linhas com o maior `fetched_at` do grupo); taxas iguais contam uma vez
  (parcelas), taxas distintas somam; `data_pagamento` = a menor data real.
- **Marcar "anunciado": decisão do dono (tarefa B4, opcional).** Marcar exige coluna nova em
  `v1-proventos`, que muda a spec da etapa seguinte (`docs/spec-entrega-etapa-seguinte.md:67`,
  colunas `ticker, tipo, valor_por_acao, data_com, data_pagamento`). Minha escolha: B2 inclui os
  anúncios **sem coluna nova** (são proventos declarados de verdade, com a taxa exata; a confirmação
  regrava a mesma chave). Se o dono quiser a marca, B4 adiciona `situacao` (`confirmado` ou
  `anunciado`) no fim da tabela.

---

## 1. Regras comuns (copiar em TODO despacho)

- Projeto: `c:/Users/consu/OneDrive/Desktop/Faculdade/tcc/Web-Scraping` (Windows; use Git Bash ou
  PowerShell). Python do projeto: `.venv/Scripts/python`. Código em `src/tcc_scraper/`.
- **Leia cada arquivo (Read) antes de editar.** Edite só os arquivos listados na sua tarefa.
- **Não toque no banco.** Nada de conectar, consultar, gravar ou aplicar DDL. Testes rodam sem rede e
  sem banco (testes com banco são opt-in por `TCC_TESTES_COM_BANCO=1`: não ligue).
- **Não faça commit** nem `git add`.
- Coletor herda de `collectors/base.Collector` (ou `CvmCollector`); toda requisição passa por
  `self.fetch(...)` (throttle por domínio, retry). Nunca `httpx`/`requests` cru.
- Persistência só por `db/upsert.bulk_upsert(session, Tabela.__table__, linhas, index_elements=[...],
  update_columns=...)`. Nunca `execute` por linha dentro de laço (DELETE/SELECT em lotes de 500 com
  `IN` é permitido).
- Tipos: `Double`, nunca `Float`; timestamp `UTCDateTime` (de `tcc_scraper.db.models`); data pura `Date`.
- Camada raw é append-only: `update_columns=None` no raw. Staging é derivada e pode ser refeita.
- Tabelas novas têm prefixo `v1-` (`PREFIXO_TABELA`). Em SQL escrito à mão, crase: `` `v1-raw_cvm_calendario` ``.
- Schema é manual: model em `src/tcc_scraper/db/`, `sql/schema.mysql.sql` e um arquivo em
  `sql/alteracoes/`. Nenhum `CREATE TABLE` em código Python.
- Vazamento temporal: dado conhecido em `t` só usa documentos com `data_entrega <= t`.
- Estilo: ruff, linha até 100 colunas, docstrings e comentários em português sem acento (como no
  código existente), `from __future__ import annotations` no topo.
- Ao terminar: rode o comando de verificação da tarefa e devolva o diff resumido + a saída do pytest.
  Se a verificação falhar e você não souber por quê, pare e reporte (não "conserte" outros arquivos).

---

## 2. Tarefas e dependências

| Tarefa | O quê | Depende de | Pode rodar junto com |
|---|---|---|---|
| A0 | `pypdf` no pyproject + fixtures PDF + confirmação do spike | - | A1, B1 |
| A1 | Models + SQL das 2 tabelas novas + testes de schema | - | A0, B1 |
| A2 | Parser puro `enrich/calendario.py` + testes | A0 | A1, B1, B2 |
| A3 | Coletor `collectors/cvm_calendario.py` + testes | A1, A2 | B2 |
| A4 | CLI (3 comandos) + parte no daily + testes | A3 | B2, B3 |
| A5 | Documentação + CLAUDE.md | A4, B2 | B3 |
| A6 | Operação: DDL (dono), coleta real e relatório de cobertura | A4 + DDL aplicada + aprovação do dono | - |
| B1 | Sentinela 9999 em `data_b3` + `_data_pagamento` | - | A0, A1, A2 |
| B2 | Anúncios do suplemento em `montar_proventos` + docs/05 | B1 | A2, A3, A4 |
| B3 | Medição só leitura dos anúncios; montagem real só com aprovação | B2 | A4, A5 |
| B4 | (OPCIONAL, só se o dono aprovar) coluna `situacao` em `v1-proventos` | B2 | - |

Ondas sugeridas: **1** = A0, A1 e B1 em paralelo; **2** = A2 e B2 em paralelo; **3** = A3;
**4** = A4 e B3 (executor) em paralelo; **5** = A5; **6** = A6. Nenhum par paralelo edita o mesmo arquivo.

Verificação final (todas as tarefas): `.venv/Scripts/python -m pytest -q` (config em `pyproject.toml`,
`testpaths = ["tests"]`, `pythonpath = ["src"]`). Linha de base hoje nos arquivos afetados:
`191 passed, 6 skipped`.

---

## Tarefa A0 — dependência `pypdf` e fixtures

Arquivos: `pyproject.toml`; novos binários em `tests/fixtures/`.

1. Em `pyproject.toml`, na lista `dependencies`, logo depois da linha `"anthropic>=2.3",` acrescente:
   ```toml
       # Texto dos PDFs do Calendario de Eventos Corporativos da CVM (collectors/cvm_calendario.py).
       "pypdf>=6.0",
   ```
   (core, não extra: o daily usa. O Dockerfile instala `.[llm,sentiment]`, que inclui o core.)
2. Instale: `.venv/Scripts/python -m pip install "pypdf>=6.0"`.
3. Copie (binário, com `cp` no Git Bash) os PDFs reais já baixados do scratchpad
   `SP=C:/Users/consu/AppData/Local/Temp/claude/c--Users-consu-OneDrive-Desktop-Faculdade-tcc-Web-Scraping/db36ffb6-0460-4107-bafb-06f59afb414d/scratchpad`:
   - `$SP/ipe_doc_petr/doc_petr.pdf` para `tests/fixtures/cvm_calendario_petr_2026.pdf`
   - `$SP/ipe_doc_vale/doc_vale.pdf` para `tests/fixtures/cvm_calendario_vale_2026.pdf`
   - `$SP/ipe_doc_itub/doc_itub.pdf` para `tests/fixtures/cvm_calendario_itub_2026.pdf`
   - `$SP/amostra_cal/SMTO3.pdf` para `tests/fixtures/cvm_calendario_smto_2026.pdf` (exercício fiscal até 31/03)
   Se algum arquivo de origem não existir, PARE e reporte (não baixe nada).
4. Confirmação do spike: salve este script como `$SP/scripts/confere_pypdf.py` e rode
   `.venv/Scripts/python $SP/scripts/confere_pypdf.py` da raiz do projeto. Deve imprimir 4 linhas `ok`:
   ```python
   from pathlib import Path

   import pypdf

   for nome in ("petr", "vale", "itub", "smto"):
       p = Path("tests/fixtures") / f"cvm_calendario_{nome}_2026.pdf"
       assert p.read_bytes()[:4] == b"%PDF", p
       texto = "\n".join(pg.extract_text() or "" for pg in pypdf.PdfReader(p).pages)
       assert "Referentes ao 3" in texto and "DFP" in texto, p
       print("ok", p, pypdf.__version__)
   ```

Verificação: os 4 `ok` acima + `.venv/Scripts/python -m pytest -q tests/test_cvm_ipe.py`.

---