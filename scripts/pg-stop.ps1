# Para o PostgreSQL local do projeto.
$PG   = "D:\pgsql-tcc\Library\bin"
$DATA = "D:\pgsql-tcc\data"
& "$PG\pg_ctl.exe" -D $DATA stop -m fast
