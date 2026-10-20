# Análise diária por LLM (Gemini): deploy e operação

Este roteiro cobre o deploy do job `analyze-llm` no Railway, a configuração do MySQL remoto, a
operação da etapa `analise` do executor e o controle de custo. A decisão de arquitetura está no [ADR](ADR.md) e a regra
de custo está no `CLAUDE.md`, seção 9.

## 1. O que é e o que não é

**É** uma camada nova, independente do scraper. Uma vez por dia, um job chama o Gemini com
grounding do Google Search e grava o resultado de forma estruturada:

- notícias recentes ligadas aos dados financeiros das empresas do IBOV;
- notícias globais e brasileiras que podem mover o mercado, com as commodities afetadas;
- ações influenciadas por essas notícias, com direção esperada e confiança;
- mercados em alta e o motivo.

**Não é:**

- uma alteração do Dataset 1.0. `features_dataset_v1` fica intacta. O cruzamento com ela, por
  `(data, ticker)`, é trabalho futuro;
- uma fonte que usa `raw_articles`. A própria LLM pesquisa via Google Search;
- um loop. O processo sobe, roda uma vez, grava e encerra;
- reprodutível bit a bit. A busca muda de um dia para o outro; por isso o JSON bruto, o prompt
  renderizado, as buscas e as citações ficam em `raw_llm_runs`.

Tabelas: `raw_llm_prompts` (prompt versionado), `raw_llm_runs` (append-only, uma linha por
tentativa, inclusive em erro) e `staging_llm_*` (normalizadas, substituídas por dia).

## 2. Variáveis no Railway

Configure no serviço do executor no Railway (Variables). Nunca commite a chave.

| Variável | Obrigatória | Padrão | Uso |
|---|---|---|---|
| `GEMINI_API_KEY` | sim | — | Chave do Google AI Studio, de um projeto com billing ativado |
| `GEMINI_MODEL` | não | `gemini-3.8-flash` | Troca de modelo sem mexer em código (ver seção 8) |
| `LLM_TIMEOUT_S` | não | `180` | Timeout da chamada, em segundos |
| `LLM_MAX_ATTEMPTS` | não | `3` | Tentativas com backoff em 429, 5xx e timeout |
| `LLM_TZ` | não | `America/Sao_Paulo` | Fuso que define `data_referencia` |
| `IP_BANCO`, `USER_BANCO`, `SENHA_BANCO`, `PORTA_BANCO`, `NOME_BANCO` | sim | — | MySQL remoto (seção 3) |

## 3. MySQL remoto

O job grava no MySQL 8 do servidor existente. Crie um usuário dedicado ao Railway, com
privilégios mínimos:

```sql
CREATE USER 'tcc_railway'@'%' IDENTIFIED BY '...';
GRANT SELECT, INSERT, UPDATE, DELETE ON tcc_scraper.* TO 'tcc_railway'@'%';
```

- `DELETE` é necessário: as tabelas `staging_llm_*` são substituídas por dia (`DELETE` + upsert).
- Não há `CREATE` nem `ALTER`: a migração roda da sua máquina, com o usuário `tcc` (seção 4).
- O host é `'%'` porque o Railway, no plano básico, não tem IP de saída fixo. Por isso a senha
  precisa ser forte e única (32 caracteres aleatórios ou mais).
- **Aviso de segurança:** `'%'` com a porta 3306 aberta expõe o MySQL a qualquer origem. Antes de
  subir:
  - restrinja a porta 3306 no firewall do servidor ao máximo possível;
  - confirme que o `bind-address` permite conexão remota;
  - se o servidor já tiver certificado, considere `REQUIRE SSL` no usuário;
  - nunca grave a senha em arquivo versionado.
- O usuário `'tcc'@'localhost'` de [MIGRACAO-MYSQL.md](MIGRACAO-MYSQL.md) continua sendo o do
  scraper.

## 4. Migração (uma vez, da sua máquina)

A migração cria as tabelas `raw_llm_*` e `staging_llm_*` via Alembic. Rode **uma vez**, da sua
máquina, com o `.env` apontando para o MySQL do projeto:

```bash
alembic upgrade head
python -m tcc_scraper.cli db-info    # confere dialeto e tabelas
```

Nunca rode `alembic upgrade` no executor. O usuário `tcc_railway` não tem permissão de DDL, e a
migração não pertence ao daily.

## 5. Agendamento

Não há cron do Railway. O serviço é um worker sempre ligado (`python -m tcc_scraper.cli executor`),
e a análise é a etapa `analise` do daily (ver [EXECUTOR.md](EXECUTOR.md)).

- **Horário:** o executor agenda o daily automático às **18:30 de Brasília** (`DAILY_HORA`, no fuso
  `DAILY_TZ`). A etapa `analise` é a última do daily e roda mesmo se a coleta estourar o orçamento.
- **Uma vez por dia:** o agendamento automático não repete no mesmo dia. O disparo manual pelo painel
  (`etapa: analise`) ou `python -m tcc_scraper.cli daily --etapa analise` é uma chamada a mais.
- **Sem reexecução automática:** se o processo reiniciar durante a análise, a execução vira `falhou`
  (`interrompida por reinício do executor`) e não é refeita. Refazer seria uma nova chamada paga.
- **Sem `GEMINI_API_KEY`:** a etapa sai `pulada`, sem chamada.
- **Custo:** cada disparo consome cota e custo, então use o manual com moderação.

## 6. Teste local

Antes de subir, valide na sua máquina, nesta ordem:

1. **Dry-run**: renderiza e imprime o prompt; não chama a API nem grava no banco:
   ```bash
   python -m tcc_scraper.cli analyze-llm --dry-run
   ```
2. **Execução real no MySQL do `.env`** (com `GEMINI_API_KEY` no `.env`):
   ```bash
   python -m tcc_scraper.cli analyze-llm
   ```
3. **Reexecução de um dia anterior**, para testar a substituição das staging:
   ```bash
   python -m tcc_scraper.cli analyze-llm --data 2026-10-13
   ```

Opcional, se houver Docker, valide a imagem:

```bash
docker build -t tcc-llm .
docker run --rm -e GEMINI_API_KEY=x tcc-llm python -m tcc_scraper.cli analyze-llm --dry-run
```

Confirme que a imagem contém o template `prompts/*.txt`: o wheel inclui o diretório inteiro de
`src/tcc_scraper`.

## 7. Consultas de leitura

Últimas execuções:

```sql
SELECT id, data_referencia, pregao_referencia, status, tokens_entrada, tokens_saida, latencia_ms
  FROM `raw_llm_runs` ORDER BY id DESC LIMIT 10;
```

Sinais do dia por ação:

```sql
SELECT ticker, direcao_esperada, confianca, motivo FROM `staging_llm_acoes_sinais`
 WHERE data_referencia = CURDATE() ORDER BY confianca DESC;
```

Notícias de uma empresa no dia:

```sql
SELECT ticker, titulo, fonte, url, sentimento, relevancia FROM `staging_llm_noticias_empresas`
 WHERE data_referencia = CURDATE() AND ticker = 'PETR4';
```

Atenção ao fuso: `CURDATE()` usa o fuso do **servidor MySQL**, enquanto `data_referencia` é o
calendário de `LLM_TZ`. Entre 21:00 e 23:59 de Brasília (00:00 a 02:59 UTC) as duas datas podem
diferir. Para consultar um dia específico, troque `CURDATE()` por uma data literal.

## 8. Custo

**Teto aprovado: US$ 5/mês** (~30 chamadas, ~US$ 0,17 por chamada). Esta é a única dependência
paga do projeto; é uma exceção explícita à regra de custo zero, registrada no `CLAUDE.md` e no ADR.

Preço do `gemini-3.8-flash` (página oficial de preços, verificada em 2026-10-15):

| Item | Preço |
|---|---|
| Entrada | US$ 0,75 / 1M tokens |
| Saída (inclui thinking) | US$ 3,75 / 1M tokens |
| Buscas do Google (grounding) | 5.000 por mês grátis (Gemini 3+); depois US$ 14 / 1.000 |
| Vigência | Preço até 31/12/2026; dobra em 2027 |

Comparativo dos outros modelos (mesma página oficial, 2026-10-15; US$ por 1M tokens; estimativa
mensal com ~10 mil tokens de entrada e ~15 mil de saída por chamada, 1 chamada/dia). Os modelos
2.5 **não** aceitam `google_search` + JSON schema na mesma chamada, então exigiriam 2 chamadas/dia
e mudança no código do cliente:

| Modelo | Entrada | Saída | Chamadas/dia | US$/mês estimado | Observação |
|---|---|---|---|---|---|
| `gemini-3.1-flash-lite` | 0,25 | 1,50 | 1 | ~0,75 | mais barato com JSON + grounding juntos |
| `gemini-3.5-flash-lite` | 0,30 | 2,50 | 1 | ~1,22 | |
| `gemini-3-flash-preview` | 0,50 | 3,00 | 1 | ~1,50 | preview |
| `gemini-3.8-flash` (padrão) | 0,75 | 3,75 | 1 | ~1,91 | promocional até 31/12/2026; dobra em 2027 |
| `gemini-3.1-pro-preview` | 2,00 | 12,00 | 1 | ~6,00 | sem free tier; estoura o teto |
| `gemini-4-argon` | 2,00 | 10,00 | 1 | ~5,10 | até 30/11/2026; de 1/12 vira 4,00 / 20,00 (~10,20/mês) |
| `gemini-2.5-flash` | 0,30 | 2,50 | 2 | ~2,10 | precisa de 2 chamadas |

Antes de trocar `GEMINI_MODEL`, confirme o id com `client.models.list()` e o preço na página
oficial: só o `gemini-3.8-flash` foi exercitado no spike.

Estimativa para 1 chamada por dia, 30 dias:

| Cenário | Entrada | Saída | Custo/dia | Custo/mês (30 dias) |
|---|---|---|---|---|
| Estimativa | ~10 mil tokens | ~15 mil tokens | ~US$ 0,064 | ~US$ 1,91 |
| Pessimista (2x tokens) | ~20 mil tokens | ~30 mil tokens | ~US$ 0,128 | ~US$ 3,83 |

Uso real medido em 2026-10-15 (prompt v3, 3 execuções): ~3,2 mil tokens de entrada,
6,7 a 8,5 mil de saída e 8 a 14 mil de raciocínio (cobrado como saída), 14 buscas, 85 a 133 s.
Custo medido: **~US$ 0,06 a 0,09 por chamada, ~US$ 1,8 a 2,6/mês** até 31/12/2026. Com o preço
dobrado em 2027, ~US$ 3,6 a 5,1/mês: no limite do teto. Reavalie antes da virada do ano
(`gemini-3.1-flash-lite` custaria cerca de um terço).

Com 1 chamada por dia, seriam necessárias ~167 buscas por chamada para sair da faixa grátis de
5.000 buscas/mês.

Em 2027, com o preço dobrado, o cenário pessimista (~US$ 7,65/mês) ultrapassa o teto. Nesse caso,
reavalie o modelo ou a frequência antes da virada.

Alerta de orçamento: no Google Cloud Billing, crie um orçamento de US$ 5 no projeto do Gemini,
com alertas em 50%, 90% e 100%.

## 9. Versionar o prompt

O template fica em `src/tcc_scraper/llm/prompts/analise_diaria_v1.txt` e a versão em `VERSAO`, no
módulo `llm/prompt.py`. Cada execução grava o prompt renderizado em `raw_llm_prompts`, então
qualquer resultado antigo continua auditável.

Para mudar o prompt:

1. Crie `analise_diaria_v2.txt` (não edite a v1 no lugar).
2. Suba `VERSAO` para `2` em `llm/prompt.py`.
3. Faça o deploy normal. As linhas novas registram a versão usada.
