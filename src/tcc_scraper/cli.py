"""Interface de linha de comando do pipeline.

Cada etapa e um comando separado, e nao um pipeline monolitico, porque elas tem
custos muito diferentes: coletar precos leva minutos, coletar noticias leva horas,
e montar o dataset leva segundos. Poder repetir so a ultima etapa e o que torna o
ajuste de features pratico.
"""

from __future__ import annotations

import datetime as dt
import logging

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from tcc_scraper.config import BENCHMARK_TICKER, IBOV_TICKERS
from tcc_scraper.db.session import dialect_name
from tcc_scraper.export.writers import summarize
from tcc_scraper.features.build import SemPrecos, build_dataset_v1
from tcc_scraper.sentiment.scoring import score_pending

app = typer.Typer(
    help="Pipeline de coleta do Dataset 1.0 (noticias + mercado B3)",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def _report(titulo: str, stats: dict[str, int]) -> None:
    table = Table(title=titulo)
    table.add_column("metrica")
    table.add_column("valor", justify="right")
    for k, v in stats.items():
        table.add_row(k, str(v))
    console.print(table)


@app.command("db-info")
def db_info() -> None:
    """Mostra a qual banco o .env aponta e se as tabelas existem."""
    _setup_logging()
    from sqlalchemy import inspect

    from tcc_scraper.db.models import Base
    from tcc_scraper.db.session import get_engine

    engine = get_engine()
    console.print(f"dialeto : [cyan]{dialect_name()}[/cyan]")
    console.print(f"destino : {engine.url.render_as_string(hide_password=True)}")

    existentes = set(inspect(engine).get_table_names())
    esperadas = [t.name for t in Base.metadata.sorted_tables]
    faltando = [t for t in esperadas if t not in existentes]

    if faltando:
        console.print(f"[yellow]faltam {len(faltando)} tabelas[/yellow]: {', '.join(faltando)}")
        console.print("rode: [bold]alembic upgrade head[/bold]")
    else:
        console.print(f"[green]as {len(esperadas)} tabelas existem[/green]")


@app.command("collect-prices")
def collect_prices(
    resume: bool = typer.Option(True, help="Pula tickers ja coletados"),
    limit: int = typer.Option(None, help="Coleta so os N primeiros (para testar)"),
) -> None:
    """Fase 2: precos do Yahoo (acoes, Ibovespa, Brent, dolar)."""
    _setup_logging()
    from tcc_scraper.collectors.prices_yahoo import YahooPriceCollector

    with YahooPriceCollector() as collector:
        _report("precos (Yahoo)", collector.run(resume=resume, limit=limit))


@app.command("collect-macro")
def collect_macro(resume: bool = typer.Option(True)) -> None:
    """Fase 2: Selic (SGS 432) e dolar PTAX do Banco Central."""
    _setup_logging()
    from tcc_scraper.collectors.macro_bcb import BcbCollector

    with BcbCollector() as collector:
        _report("macro (BCB)", collector.run(resume=resume))


@app.command("collect-news")
def collect_news(
    resume: bool = typer.Option(True, help="Retoma de onde parou"),
    limit: int = typer.Option(None, help="Coleta so as N primeiras janelas"),
    ticker: str = typer.Option(None, help="Coleta um unico ticker"),
) -> None:
    """Fase 3: noticias historicas via Google News RSS (~10 h na carga completa)."""
    _setup_logging()
    from tcc_scraper.collectors.google_news import GoogleNewsCollector

    tickers = None
    if ticker:
        if ticker not in IBOV_TICKERS:
            raise typer.BadParameter(f"{ticker} nao esta em IBOV_TICKERS")
        tickers = {ticker: IBOV_TICKERS[ticker]}

    with GoogleNewsCollector(tickers=tickers) as collector:
        stats = collector.run(resume=resume, limit=limit)
    _report("noticias (Google News)", stats)
    if stats.get("saturadas"):
        console.print(
            f"[yellow]{stats['saturadas']} janelas bateram o teto de 100 itens "
            "e foram subdivididas em dias.[/yellow]"
        )


@app.command("enrich")
def enrich(limit: int = typer.Option(None, help="Processa so os N primeiros lotes")) -> None:
    """Fase 4: resolve o link e extrai o corpo dos artigos da allowlist."""
    _setup_logging()
    from tcc_scraper.enrich.article_fetcher import ArticleBodyCollector

    with ArticleBodyCollector() as collector:
        _report("corpo dos artigos", collector.run(resume=False, limit=limit))


class _ObservadorScore:
    """Liga o scoring a barra do rich: uma unidade de progresso por bloco."""

    def __init__(self, progresso: Progress) -> None:
        self._progresso = progresso
        self._tarefa = None

    def plano(self, n_blocos: int) -> None:
        self._tarefa = self._progresso.add_task("scoring", total=n_blocos)

    def deve_parar(self) -> bool:
        return False

    def unidade(self, nome: str, *, ok: bool, itens: int, saturada: bool,
                erro: str | None) -> None:
        self._progresso.update(self._tarefa, advance=1)


@app.command("score")
def score(
    batch_size: int = typer.Option(32, help="Textos por lote de inferencia"),
    limit: int = typer.Option(None, help="Pontua so os N primeiros artigos"),
) -> None:
    """Fase 5: sentimento via FinBERT (pt-BR e en), roteado por idioma."""
    _setup_logging()

    with Progress(
        SpinnerColumn(), TextColumn("[cyan]FinBERT"), BarColumn(),
        TaskProgressColumn(), TimeRemainingColumn(), console=console,
    ) as progresso:
        stats = score_pending(
            batch_size=batch_size, limit=limit, observador=_ObservadorScore(progresso)
        )

    if stats["artigos"] == 0:
        console.print("[yellow]nenhum artigo pendente de scoring[/yellow]")
        return
    _report(
        "sentimento",
        {
            "artigos": stats["artigos"],
            "gravados": stats["gravados"],
            "com corpo": stats["com_corpo"],
            "so manchete": stats["so_manchete"],
        },
    )
    if stats["interrompido"]:
        console.print("[yellow]interrompido: rode de novo para continuar[/yellow]")


@app.command("build-dataset")
def build_dataset(
    export: bool = typer.Option(True, help="Grava Parquet e CSV ao final"),
    persist: bool = typer.Option(True, help="Grava em features_dataset_v1 no banco"),
    keep_unlabeled: bool = typer.Option(False, help="Mantem o ultimo pregao, sem target"),
) -> None:
    """Fase 6: monta as 14 colunas, grava no banco e exporta."""
    _setup_logging()

    try:
        resultado = build_dataset_v1(
            persist=persist, export=export, keep_unlabeled=keep_unlabeled
        )
    except SemPrecos as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    dataset = resultado.dataset
    _report(
        "dataset",
        {
            "linhas": len(dataset),
            "tickers": dataset["ticker"].nunique(),
            "com noticia": int((dataset["quant_noticias"] > 0).sum()),
            "target=1": int((dataset["target"] == 1).sum()) if len(dataset) else 0,
        },
    )

    tabela = Table(title="nulos por coluna")
    tabela.add_column("coluna")
    tabela.add_column("nulos", justify="right")
    tabela.add_column("%", justify="right")
    for _, r in summarize(dataset).iterrows():
        tabela.add_row(r["coluna"], str(r["nulos"]), f"{r['pct_nulos']}%")
    console.print(tabela)

    if persist:
        console.print(
            f"[green]banco[/green]: {resultado.linhas_banco} linhas em features_dataset_v1"
        )
    for kind, path in resultado.arquivos.items():
        console.print(f"[green]{kind}[/green]: {path}")


@app.command("analyze-llm")
def analyze_llm(
    data: str = typer.Option(None, "--data", help="YYYY-MM-DD (padrao: hoje em LLM_TZ)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Imprime o prompt; nao chama a API nem grava"
    ),
    modelo: str = typer.Option(None, "--modelo", help="Sobrescreve GEMINI_MODEL"),
) -> None:
    """Analise diaria por LLM (Gemini + Google Search): grava raw_llm_runs e staging_llm_*."""
    _setup_logging()
    from tcc_scraper.llm.job import run_daily_analysis

    ref = dt.date.fromisoformat(data) if data else None
    try:
        r = run_daily_analysis(ref, dry_run=dry_run, modelo=modelo)
    except ImportError as exc:  # extra `llm` nao instalado
        # markup=False: "[llm]" e mensagens da API nao podem ser lidas como tags do rich
        console.print(f"{exc} -- instale com: pip install -e '.[llm]'", style="red",
                      markup=False)
        raise typer.Exit(1) from exc
    except ValueError as exc:   # GEMINI_API_KEY ausente, data invalida
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc

    if r.status == "dry_run":
        console.print(r.prompt, markup=False, highlight=False)
        return
    _report("analise LLM", {
        "status": r.status, "run_id": r.run_id, "data": str(r.data_referencia),
        "pregao": str(r.pregao_referencia), **r.linhas, **r.tokens,
    })
    if r.status != "ok":
        console.print(r.erro, style="red", markup=False)
        raise typer.Exit(1)


@app.command("validate-tickers")
def validate_tickers() -> None:
    """Confere se todos os tickers de IBOV_TICKERS respondem no Yahoo."""
    _setup_logging()
    from tcc_scraper.collectors.prices_yahoo import CHART_ENDPOINT, YahooPriceCollector
    from tcc_scraper.config import yahoo_symbol

    ok, falhas = [], []
    with YahooPriceCollector() as collector:
        for ticker in [*IBOV_TICKERS, BENCHMARK_TICKER]:
            try:
                url = f"{CHART_ENDPOINT}/{yahoo_symbol(ticker)}?range=1mo&interval=1d"
                resp = collector.fetch(url)
                (ok if resp.status_code == 200 else falhas).append(ticker)
            except Exception:
                falhas.append(ticker)

    console.print(f"[green]validos: {len(ok)}[/green]  [red]falhas: {len(falhas)}[/red]")
    if falhas:
        console.print(f"[red]{', '.join(falhas)}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
