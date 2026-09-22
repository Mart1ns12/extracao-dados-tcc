# Cria o usuario e o banco do projeto. Rode UMA vez, com o servidor no ar.
$ErrorActionPreference = "Stop"
$PG = "D:\pgsql-tcc\Library\bin"
$env:PGPASSWORD = "tcc_local_dev"

& "$PG\psql.exe" -h localhost -p 5433 -U postgres -d postgres -v ON_ERROR_STOP=1 -c @"
DO `$`$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tcc') THEN
      CREATE ROLE tcc LOGIN PASSWORD 'tcc_local_dev';
   END IF;
END
`$`$;
"@

$exists = & "$PG\psql.exe" -h localhost -p 5433 -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='tcc_scraper'"
if ($exists -ne "1") {
    & "$PG\psql.exe" -h localhost -p 5433 -U postgres -d postgres -c "CREATE DATABASE tcc_scraper OWNER tcc ENCODING 'UTF8'"
    Write-Host "banco tcc_scraper criado" -ForegroundColor Green
} else {
    Write-Host "banco tcc_scraper ja existe" -ForegroundColor Yellow
}
