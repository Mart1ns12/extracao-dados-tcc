# Migração para o servidor MySQL

Roteiro para rodar o pipeline no servidor externo, onde o **script e o banco ficam na
mesma máquina**. Todos os passos foram validados contra um MySQL 8.4 real antes de
serem escritos aqui.

O código já é portável: o mesmo `alembic upgrade head` cria a estrutura nos dois bancos,
e o dataset gerado sai **bit a bit idêntico** (há teste automatizado para isso).

---

## Já executado (2026-09-21)

O banco do servidor **já está pronto**: database `tcc_scraper` criado com `utf8mb4`,
as 8 tabelas migradas e os 10.646 registros do piloto copiados e conferidos. O dataset
gerado a partir dele saiu idêntico ao do PostgreSQL local.

Servidor: MySQL 8.0.46 (Ubuntu 24.04), `system_time_zone = -03`, `max_allowed_packet`
64 MB, `wait_timeout` 8 h.

As seções abaixo servem para refazer o processo em outro servidor, ou para conferir o
que foi feito.

---

## Antes de começar

No servidor, confirme a versão:

```sql
SELECT VERSION();
```

Precisa ser **MySQL 8.0 ou superior**. Em 5.7 a migration falha — aquela versão não
aceita o tamanho de índice que o `url_hash` usa com `utf8mb4`.

---

## 1. Preparar o banco

```sql
CREATE DATABASE tcc_scraper
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER 'tcc'@'localhost' IDENTIFIED BY 'TROQUE_ESTA_SENHA';
GRANT ALL PRIVILEGES ON tcc_scraper.* TO 'tcc'@'localhost';
FLUSH PRIVILEGES;
```

**`utf8mb4` não é opcional.** O `utf8` do MySQL guarda no máximo 3 bytes por caractere
e quebra em emoji e em alguns caracteres que aparecem em manchetes. Com `utf8mb4` os
2.084 títulos do piloto voltaram idênticos aos do PostgreSQL, acentos inclusive.

Se o servidor expõe o MySQL para fora, **use `'tcc'@'localhost'`** como acima. Como o
script roda na mesma máquina, não há motivo para o usuário aceitar conexão remota.

---

## 2. Clonar e instalar

```bash
git clone <seu-repositorio> tcc-scraper
cd tcc-scraper

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e ".[mysql,sentiment]"
```

O extra `mysql` traz `pymysql` e `cryptography` — o segundo é exigido pelo
`caching_sha2_password`, que é o padrão de autenticação do MySQL 8. Sem ele a conexão
falha com um erro pouco óbvio sobre plugin de autenticação.

O extra `sentiment` traz torch e transformers (~2 GB). Se o servidor não vai rodar o
FinBERT, omita.

---

## 3. Configurar

```bash
cp .env.example .env
```

E no `.env`:

```ini
DB_TARGET=mysql

IP_BANCO=127.0.0.1
USER_BANCO=tcc
SENHA_BANCO=SUA_SENHA
PORTA_BANCO=3306
NOME_BANCO=tcc_scraper

DATA_START=2025-09-01
DATA_END=2026-09-01
PRICE_WARMUP_DAYS=400
HF_CACHE_DIR=/caminho/com/espaco/hf-cache
```

Com `DB_TARGET=mysql`, o `config.py` monta a URL a partir dessas variáveis — a senha
fica em um lugar só, em vez de repetida dentro de uma URL. Ela passa por `quote_plus`,
então caracteres como `@` e `/` na senha não quebram a conexão.

Para voltar ao PostgreSQL local, basta `DB_TARGET=postgres`.

Use `127.0.0.1`, não `localhost`: em algumas configurações do MySQL, `localhost` força
conexão por socket Unix e ignora a porta.

---

## 4. Criar as tabelas

```bash
alembic upgrade head
python -m tcc_scraper.cli db-info
```

O `db-info` confirma o dialeto detectado e se as 8 tabelas existem. São elas:

```
raw_articles              raw_prices              staging_article_tickers
raw_article_bodies        raw_macro               staging_article_sentiment
raw_collection_runs                               features_dataset_v1
```

As camadas são **prefixo no nome**, não schema separado, porque o MySQL não tem
schemas como o PostgreSQL — lá "schema" e "database" são a mesma coisa.

---

## 5. Levar os dados já coletados (opcional)

Se quiser aproveitar o que já está no PostgreSQL local em vez de recoletar:

```bash
python scripts/migrar_dados.py \
  --origem  "postgresql+psycopg://tcc:senha@IP_DA_SUA_MAQUINA:5433/tcc_scraper" \
  --destino "mysql+pymysql://tcc:senha@127.0.0.1:3306/tcc_scraper?charset=utf8mb4"
```

O script é **idempotente** (usa o mesmo upsert do pipeline): rodar duas vezes não
duplica nada, e uma execução interrompida pode ser repetida sem limpar o destino. Ao
final ele confere a contagem tabela a tabela e retorna código 1 se algo divergir.

Use `--dry-run` para ver o que seria copiado sem escrever.

Alternativa: o piloto tem só ~10 mil linhas e preços/macro se recoletam em ~3 minutos.
Começar do zero no servidor é perfeitamente razoável.

---

## 6. Rodar o pipeline

Tudo de uma vez, com log por etapa:

```bash
chmod +x scripts/coleta-completa.sh
nohup ./scripts/coleta-completa.sh > logs/coleta.log 2>&1 &
tail -f logs/coleta.log
```

Ou etapa a etapa:

```bash
python -m tcc_scraper.cli collect-prices    # ~2 min, 88 séries
python -m tcc_scraper.cli collect-macro     # ~1 min, Selic + PTAX
python -m tcc_scraper.cli collect-news      # 6-9 h, retomável
python -m tcc_scraper.cli enrich            # corpo dos artigos da allowlist
python -m tcc_scraper.cli score             # FinBERT pt + en
python -m tcc_scraper.cli build-dataset     # 14 colunas -> banco + Parquet + CSV
```

A coleta de notícias é **retomável**: se cair na hora 7, rodar o mesmo comando continua
de onde parou. Em Linux, convém rodá-la sob `tmux` ou `nohup` para sobreviver à queda
da sessão SSH:

```bash
nohup python -m tcc_scraper.cli collect-news > coleta.log 2>&1 &
tail -f coleta.log
```

---

## Diferenças de comportamento que você vai notar

**Buscas `LIKE` ignoram acento.** A collation `utf8mb4_0900_ai_ci` é *accent-insensitive*:
`WHERE title LIKE '%ã%'` devolveu **584** linhas no PostgreSQL e **2.078** no MySQL sobre
os mesmos dados. O conteúdo é idêntico; o que muda é a comparação. Para busca sensível a
acento no MySQL, force a collation:

```sql
SELECT * FROM raw_articles WHERE title LIKE '%ã%' COLLATE utf8mb4_0900_as_cs;
```

Isso **não afeta o dataset** — nenhuma query do pipeline depende de `LIKE`.

**Conexões ociosas caem.** O MySQL fecha conexão parada (`wait_timeout`, 8 h por padrão)
e a coleta dura ~10 h. O `get_engine()` já define `pool_recycle=3600` para reciclar antes
disso; não remova.

**DDL não tem rollback.** No PostgreSQL uma migration que falha no meio desfaz tudo; no
MySQL, não. Se uma migration quebrar no meio, o banco fica parcialmente alterado — por
isso vale rodar `alembic upgrade head` num banco de teste antes do definitivo.

---

## Voltar atrás

Se algo der errado, o PostgreSQL local continua intacto — basta trocar a `DATABASE_URL`
no `.env` de volta. O mesmo `migrar_dados.py` faz o caminho inverso, invertendo
`--origem` e `--destino`.
