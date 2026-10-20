# Banco do projeto: MySQL 8 (migrations e acesso)

O projeto tem **um banco só**: o MySQL 8 do `.env`. Não existe PostgreSQL nem banco local, e
nenhum comando sobe um banco de desenvolvimento. Todo o código (coleta, dataset, painel,
executor, testes com banco) fala com esse mesmo servidor.

Decisão e custo: [ADR.md](ADR.md), entrada "Um banco só: MySQL do .env".

---

## 1. Requisitos do servidor

No servidor, confirme a versão:

```sql
SELECT VERSION();
```

Precisa ser **MySQL 8.0 ou superior**. Em 5.7 a migration falha: aquela versão não aceita o
tamanho de índice que o `url_hash` usa com `utf8mb4`.

O banco `tcc_scraper` deve ser criado com `utf8mb4`. O `utf8` do MySQL guarda no máximo 3 bytes
por caractere e quebra em emoji e em alguns caracteres de manchetes:

```sql
CREATE DATABASE tcc_scraper
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
```

Servidor em produção hoje: MySQL 8.0.46 (Ubuntu 24.04), `system_time_zone = -03`,
`max_allowed_packet` 64 MB, `wait_timeout` 8 h.

---

## 2. Usuário

Para rodar as migrations, o usuário precisa de **DDL** (`CREATE`, `ALTER`, `DROP`, `INDEX`) sobre o
banco. Para só coletar e rodar o executor, basta `SELECT`, `INSERT`, `UPDATE`, `DELETE`.

Exemplo para um servidor novo, com acesso restrito à máquina que roda o projeto:

```sql
CREATE USER 'tcc'@'SEU_HOST' IDENTIFIED BY 'TROQUE_ESTA_SENHA';
GRANT ALL PRIVILEGES ON tcc_scraper.* TO 'tcc'@'SEU_HOST';
FLUSH PRIVILEGES;
```

Se o MySQL aceita conexão de fora, não use `'%'` sem necessidade: restrinja ao IP de quem
conecta.

---

## 3. Instalar e configurar

```bash
python -m venv .venv
.venv\Scripts\activate           # Linux: source .venv/bin/activate
pip install -e ".[dev,llm]"      # [sentiment] só se a máquina vai rodar o FinBERT
cp .env.example .env             # Windows: copy .env.example .env
```

O driver (`pymysql`) e o `cryptography` são dependências principais. O `cryptography` é exigido
pelo `caching_sha2_password`, que é o padrão do MySQL 8; sem ele a conexão falha com um erro
pouco óbvio sobre plugin de autenticação.

No `.env`:

```ini
IP_BANCO=IP_DO_SERVIDOR
USER_BANCO=tcc
SENHA_BANCO=SUA_SENHA
PORTA_BANCO=3306
NOME_BANCO=tcc_scraper
```

A URL é montada pelo `Settings.effective_url` a partir dessas variáveis (`mysql+pymysql`,
`charset=utf8mb4`). A senha é escapada pelo SQLAlchemy, então `@` e `/` nela não quebram a
conexão. Se faltar `IP_BANCO`, `USER_BANCO` ou `SENHA_BANCO`, o erro diz quais.

Use o IP, não `localhost`: em algumas configurações do MySQL, `localhost` força conexão por
socket Unix e ignora a porta.

---

## 4. Aplicar as migrations

Rode **da máquina local, contra o banco do `.env`**, com um usuário que tenha DDL:

```bash
alembic upgrade head
python -m tcc_scraper.cli db-info
```

O `db-info` mostra o destino (sem a senha) e lista as tabelas que faltam. Ele é só leitura.

Sem Alembic, o schema inteiro até a migration 0008 está em `sql/completo_ate_0008.mysql.sql`
(gerado com `alembic upgrade base:head --sql`). Banco vazio: rode esse arquivo. Banco já na 0003:
aplique só `sql/0004_*` a `sql/0008_*`. Antes, confira `SELECT version_num FROM alembic_version;`.
No MySQL atual, o `alembic_version` tem duas linhas (`0002` e `0007`): antes do `sql/0008_*`, rode o
`DELETE` que o cabeçalho desse arquivo traz.

**DDL não tem rollback no MySQL.** Se uma migration quebra no meio, o banco fica parcialmente
alterado. Antes de aplicar em banco com dados reais, confira o `--sql` gerado e o backup.

As camadas são **prefixo no nome** (`raw_`, `staging_`, `painel_`), não schema separado: no MySQL,
"schema" e "database" são a mesma coisa. As tabelas criadas pela pipeline nova levam `v1-` na frente
(`v1-raw_ipe_docs`, `v1-painel_logs`, `v1-noticias`); as que já existiam não (`raw_articles`,
`raw_prices`). Em SQL escrito à mão, o nome com hífen vai entre crases: `` SELECT * FROM `v1-painel_logs` ``.
O `features_dataset_v1` é o dataset v1 congelado e mantém o nome antigo.

---

## 5. Rodar o pipeline

Tudo de uma vez, com log por etapa:

```bash
chmod +x scripts/coleta-completa.sh
nohup ./scripts/coleta-completa.sh > logs/coleta.log 2>&1 &
tail -f logs/coleta.log
```

Ou etapa a etapa, por exemplo:

```bash
python -m tcc_scraper.cli collect-prices    # ~2 min, 88 séries
python -m tcc_scraper.cli collect-news      # 6-9 h, retomável
python -m tcc_scraper.cli build-dataset     # 14 colunas -> banco + Parquet + CSV
```

A coleta de notícias é **retomável**: rodar o mesmo comando de novo continua de onde parou.
Em Linux, rode-a sob `tmux` ou `nohup` para sobreviver à queda da sessão SSH.

---

## 6. Diferenças de comportamento que você vai notar

**Buscas `LIKE` ignoram acento.** A collation `utf8mb4_0900_ai_ci` é *accent-insensitive*:
`WHERE title LIKE '%ã%'` traz mais linhas do que uma busca sensível a acento. Para busca sensível a
acento, force a collation:

```sql
SELECT * FROM `raw_articles` WHERE title LIKE '%ã%' COLLATE utf8mb4_0900_as_cs;
```

Isso **não afeta o dataset**: nenhuma query do pipeline depende de `LIKE`.

**Conexões ociosas caem.** O MySQL fecha conexão parada (`wait_timeout`, 8 h por padrão) e a
coleta dura ~10 h. O `get_engine()` define `pool_recycle=3600` para reciclar antes disso; não
remova.

---

## 7. Testes com banco

A suite roda sem banco por padrão. Os testes que gravam no MySQL do `.env` são **opt-in**:

```bash
TCC_TESTES_COM_BANCO=1 pytest      # PowerShell: $env:TCC_TESTES_COM_BANCO=1; pytest
```

Sem a variável, eles são pulados com a mensagem `teste com banco desligado`. Com ela, gravam no
banco real. Por isso cada teste limpa só as linhas que criou (prefixos `TESTE-`, `REGR`, `t27.`,
`ZZZZ9`, datas de 2099) antes e depois, e não apaga o resto. A fixture `engine` que entrega a
conexão está em `tests/conftest.py`.
