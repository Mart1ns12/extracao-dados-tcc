# Sobe o PostgreSQL local do projeto (instalado via conda em D:, sem admin).
# O cluster fica em D: porque C: tem pouco espaco livre.
$ErrorActionPreference = "Stop"
$PG   = "D:\pgsql-tcc\Library\bin"
$DATA = "D:\pgsql-tcc\data"
$LOG  = "D:\pgsql-tcc\postgres.log"

& "$PG\pg_ctl.exe" -D $DATA -l $LOG start
Start-Sleep -Seconds 2
& "$PG\pg_isready.exe" -h localhost -p 5433
Write-Host "log: $LOG" -ForegroundColor DarkGray
