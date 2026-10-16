"""Interface de linha de comando do pipeline.

Cada etapa e um comando separado, e nao um pipeline monolitico, porque elas tem
custos muito diferentes: coletar precos leva minutos, coletar noticias leva horas,
e montar o dataset leva segundos. Poder repetir so a ultima etapa e o que torna o
ajuste de features pratico.
"""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd
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
from sqlalchemy import select

from tcc_scraper.config import BENCHMARK_TICKER, IBOV_TICKERS, get_settings
from tcc_scraper.db.models import (
    Article,
    ArticleBody,
    ArticleSentiment,
    ArticleTicker,
    DatasetV1,
    Price,
)
from tcc_scraper.db.session import dialect_name, session_scope
from tcc_scraper.db.upsert import bulk_upsert
from tcc_scraper.export.writers import summarize, write_dataset
from tcc_scraper.features.dataset import DATASET_COLUMNS, assemble_dataset, drop_unlabeled

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


# Artigos pontuados e gravados por vez. Grande o bastante para o upsert render,
# pequeno o bastante para uma queda custar pouco trabalho refeito.
SCORE_CHUNK = 2_000


def _blocos(itens: list, tamanho: int):
    """Fatia a lista em blocos de `tamanho` (o ultimo pode ser menor)."""
    for inicio in range(0, len(itens), tamanho):
        yield itens[inicio : inicio + tamanho]


def _linhas_sentimento(rows: list, resultados: list, quando: dt.datetime) -> list[dict]:
    """Monta as linhas de staging_article_sentiment a partir da saida do router.

    `rows` sao tuplas (id, titulo, lang, corpo); `resultados`, pares (score, modelo)
    na MESMA ordem -- o router garante isso mesmo agrupando por idioma.
    """
    return [
        {
            "article_id": row[0],
            "model": modelo,
            "prob_pos": score.prob_pos,
            "prob_neu": score.prob_neu,
            "prob_neg": score.prob_neg,
            # Registra se o score saiu do corpo ou so da manchete: sem isso nao da
            # para avaliar depois se o enriquecimento melhorou o sinal.
            "text_source": "body" if row[3] else "title",
            "scored_at": quando,
        }
        for row, (score, modelo) in zip(rows, resultados, strict=True)
    ]


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


@app.command("score")
def score(
    batch_size: int = typer.Option(32, help="Textos por lote de inferencia"),
    limit: int = typer.Option(None, help="Pontua so os N primeiros artigos"),
) -> None:
    """Fase 5: sentimento via FinBERT (pt-BR e en), roteado por idioma."""
    _setup_logging()
    from tcc_scraper.sentiment.router import SentimentRouter

    # Prefere o corpo quando existe; senao usa a manchete. text_source registra
    # qual foi, para a analise de qualidade.
    with session_scope() as session:
        rows = session.execute(
            select(Article.id, Article.title, Article.lang, ArticleBody.body_text)
            .outerjoin(ArticleBody, ArticleBody.article_id == Article.id)
            .outerjoin(ArticleSentiment, ArticleSentiment.article_id == Article.id)
            .where(ArticleSentiment.article_id.is_(None))
        ).all()

    if limit:
        rows = rows[:limit]
    if not rows:
        console.print("[yellow]nenhum artigo pendente de scoring[/yellow]")
        return

    router = SentimentRouter()
    router.scorers = {
        k: type(v)(v.model_name, v.lang, batch_size) for k, v in router.scorers.items()
    }

    total = len(rows)
    com_corpo = sum(1 for r in rows if r[3])
    console.print(
        f"pontuando {total} artigos "
        f"({com_corpo} com corpo, {total - com_corpo} so manchete)"
    )

    gravados = 0
    with Progress(
        SpinnerColumn(), TextColumn("[cyan]FinBERT"), BarColumn(),
        TaskProgressColumn(), TimeRemainingColumn(), console=console,
    ) as progresso:
        tarefa = progresso.add_task("scoring", total=total)

        # Grava a cada bloco em vez de so no fim. Com ~88 mil artigos a inferencia
        # leva ~30 min: pontuar tudo antes de gravar significaria perder o trabalho
        # inteiro numa queda aos 25 min. Como a consulta de pendentes exclui quem ja
        # tem sentimento, gravar em blocos torna a etapa retomavel de graca.
        for bloco in _blocos(rows, SCORE_CHUNK):
            resultados = router.score_texts(
                [(r[3] or r[1]) for r in bloco], langs=[r[2] for r in bloco]
            )
            linhas = _linhas_sentimento(bloco, resultados, dt.datetime.now(dt.UTC))
            with session_scope() as session:
                bulk_upsert(
                    session,
                    ArticleSentiment.__table__,
                    linhas,
                    index_elements=["article_id", "model"],
                    update_columns=[
                        "prob_pos", "prob_neu", "prob_neg", "text_source", "scored_at"
                    ],
                )
            gravados += len(linhas)
            progresso.update(tarefa, advance=len(bloco))

    _report(
        "sentimento",
        {
            "artigos": total,
            "gravados": gravados,
            "com corpo": com_corpo,
            "so manchete": total - com_corpo,
        },
    )


@app.command("build-dataset")
def build_dataset(
    export: bool = typer.Option(True, help="Grava Parquet e CSV ao final"),
    persist: bool = typer.Option(True, help="Grava em features_dataset_v1 no banco"),
    keep_unlabeled: bool = typer.Option(False, help="Mantem o ultimo pregao, sem target"),
) -> None:
    """Fase 6: monta as 14 colunas, grava no banco e exporta."""
    _setup_logging()
    settings = get_settings()

    with session_scope() as session:
        precos = pd.DataFrame(
            session.execute(
                select(Price.ticker, Price.date, Price.adj_close, Price.volume)
            ).all(),
            columns=["ticker", "date", "adj_close", "volume"],
        )
        artigos = pd.DataFrame(
            session.execute(
                select(
                    Article.id, ArticleTicker.ticker, Article.published_at,
                    ArticleSentiment.prob_pos, ArticleSentiment.prob_neu,
                    ArticleSentiment.prob_neg,
                )
                .join(ArticleTicker, ArticleTicker.article_id == Article.id)
                .join(ArticleSentiment, ArticleSentiment.article_id == Article.id)
            ).all(),
            columns=["article_id", "ticker", "published_at", "prob_pos", "prob_neu", "prob_neg"],
        )

    if precos.empty:
        console.print("[red]sem precos no banco -- rode collect-prices antes[/red]")
        raise typer.Exit(1)

    # Universo explicito: raw_prices tambem guarda o benchmark e as series macro
    # (Brent, dolar), que nao sao observacoes do dataset.
    #
    # start=data_start recorta as linhas a janela do dataset; o preco anterior
    # (coletado a partir de price_start) fica so como aquecimento das janelas de
    # 20 pregoes, para que a primeira linha ja saia com retorno_20d preenchido.
    dataset = assemble_dataset(
        precos,
        artigos,
        benchmark=BENCHMARK_TICKER,
        universe=IBOV_TICKERS.keys(),
        start=settings.data_start,
        end=settings.data_end,
    )
    if not keep_unlabeled:
        dataset = drop_unlabeled(dataset)

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
        n = _persist_dataset(dataset)
        console.print(f"[green]banco[/green]: {n} linhas em features_dataset_v1")

    if export:
        for kind, path in write_dataset(dataset, settings.export_dir).items():
            console.print(f"[green]{kind}[/green]: {path}")


def _persist_dataset(dataset: pd.DataFrame) -> int:
    """Materializa o dataset em features_dataset_v1.

    Upsert por (data, ticker): remontar o dataset com outra regra de feature
    sobrescreve as linhas em vez de duplicar, e a tabela reflete sempre a versao
    mais recente. Linhas de uma execucao anterior que nao aparecem mais (por exemplo,
    ao encurtar a janela) NAO sao removidas -- use `--no-persist` e limpe a tabela a
    mao se quiser um recorte exato.

    NaN do pandas vira NULL no banco: `positivo` nulo significa "nao medido", e
    gravar 0 afirmaria sentimento neutro observado.
    """
    if dataset.empty:
        return 0

    linhas = dataset.replace({np.nan: None}).to_dict("records")
    atualizaveis = [c for c in DATASET_COLUMNS if c not in ("data", "ticker")]
    with session_scope() as session:
        return bulk_upsert(
            session,
            DatasetV1.__table__,
            linhas,
            index_elements=["data", "ticker"],
            update_columns=atualizaveis,
        )


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
