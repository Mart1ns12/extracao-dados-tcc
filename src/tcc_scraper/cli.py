"""Interface de linha de comando do pipeline.

Cada etapa e um comando separado, e nao um pipeline monolitico, porque elas tem
custos muito diferentes: coletar precos leva minutos, coletar noticias leva horas,
e montar o dataset leva segundos. Poder repetir so a ultima etapa e o que torna o
ajuste de features pratico.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated

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
    # O httpx loga cada requisicao em INFO; no executor (poll a cada 5 s) isso gerava
    # 40-80 MB de log por dia. So avisos e erros, salvo em modo verbose.
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


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
    console.print(f"dialeto : [cyan]{engine.dialect.name}[/cyan]")
    console.print(f"destino : {engine.url.render_as_string(hide_password=True)}")

    existentes = set(inspect(engine).get_table_names())
    esperadas = [t.name for t in Base.metadata.sorted_tables]
    faltando = [t for t in esperadas if t not in existentes]

    if faltando:
        console.print(f"[yellow]faltam {len(faltando)} tabelas[/yellow]: {', '.join(faltando)}")
        console.print("banco vazio: aplique [bold]sql/schema.mysql.sql[/bold]; tabela faltando: ver [bold]sql/alteracoes/[/bold]")
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
    desde: str = typer.Option(None, help="Inicio do recorte, YYYY-MM-DD (use com --ate)"),
    ate: str = typer.Option(
        None, help="Fim do recorte, exclusivo, YYYY-MM-DD (use com --desde)"
    ),
) -> None:
    """Fase 3: noticias historicas via Google News RSS (~10 h na carga completa)."""
    _setup_logging()
    from tcc_scraper.collectors.google_news import GoogleNewsCollector

    tickers = None
    if ticker:
        if ticker not in IBOV_TICKERS:
            raise typer.BadParameter(f"{ticker} nao esta em IBOV_TICKERS")
        tickers = {ticker: IBOV_TICKERS[ticker]}

    if (desde is None) != (ate is None):
        raise typer.BadParameter("--desde e --ate devem ser usados juntos")
    d_desde = d_ate = None
    if desde is not None:
        try:
            d_desde = dt.date.fromisoformat(desde)
            d_ate = dt.date.fromisoformat(ate)
        except ValueError as exc:
            raise typer.BadParameter("datas no formato YYYY-MM-DD") from exc
        if d_ate <= d_desde:
            raise typer.BadParameter("--ate deve ser posterior a --desde")

    with GoogleNewsCollector(tickers=tickers, desde=d_desde, ate=d_ate) as collector:
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


@app.command("executor")
def executor() -> None:
    """Worker do painel: busca comandos, roda o daily e confirma (fica em loop)."""
    _setup_logging()
    from tcc_scraper.config import get_settings
    from tcc_scraper.executor.loop import Executor
    from tcc_scraper.executor.painel_client import PainelClient
    from tcc_scraper.orquestrador import Daily, RepositorioBanco
    from tcc_scraper.orquestrador.historico import Historico

    settings = get_settings()
    if not settings.painel_api_url or not settings.executor_token:
        console.print("[red]PAINEL_API_URL e EXECUTOR_TOKEN precisam estar no .env[/red]")
        raise typer.Exit(1)

    repo = RepositorioBanco()
    client = PainelClient(settings.painel_api_url, settings.executor_token)
    executor_ = Executor(
        repo, Daily(repo, settings=settings), Historico(repo, settings), client, settings
    )
    executor_.run_forever()


@app.command("daily")
def daily_cmd(
    etapa: str = typer.Option(None, help="Roda so esta etapa (ex.: noticias)"),
    origem: str = typer.Option("manual", help="Origem registrada na execucao"),
) -> None:
    """Roda o daily agora (bloqueante) e imprime o resumo."""
    _setup_logging()
    from tcc_scraper.orquestrador import Daily, ExecucaoEmAndamento, RepositorioBanco

    repo = RepositorioBanco()
    tipo = "etapa" if etapa else "daily"
    try:
        execucao_id = Daily(repo).executar(tipo, etapa, origem)
    except (ExecucaoEmAndamento, ValueError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from exc

    exe = repo.obter(execucao_id)
    console.print(f"execução [cyan]{execucao_id}[/cyan]: [bold]{exe['status']}[/bold]")
    _report(
        "etapas",
        {f"{e['ordem']}. {e['etapa_id']} [{e['status']}]": e["itens"] for e in exe["etapas"]},
    )


@app.command("collect-cvm")
def collect_cvm(
    anos: Annotated[
        list[int] | None, typer.Option("--anos", help="Repita: --anos 2025 --anos 2026")
    ] = None,
    resume: bool = typer.Option(True, help="Pula o que ja foi coletado"),
) -> None:
    """ITR e DFP da CVM: grava v1-raw_cvm_balancos e v1-raw_cvm_acoes."""
    _setup_logging()
    from tcc_scraper.collectors.cvm_demonstracoes import CvmDemonstracoesCollector

    with CvmDemonstracoesCollector(anos=anos or None) as collector:
        _report("ITR/DFP (CVM)", collector.run(resume=resume))


@app.command("collect-ipe")
def collect_ipe(
    anos: Annotated[
        list[int] | None, typer.Option("--anos", help="Repita: --anos 2025 --anos 2026")
    ] = None,
    resume: bool = typer.Option(True, help="Pula o que ja foi coletado"),
) -> None:
    """Fatos relevantes (IPE) da CVM, gravados como noticias."""
    _setup_logging()
    from tcc_scraper.collectors.cvm_ipe import CvmIpeCollector

    with CvmIpeCollector(anos=anos or None) as collector:
        _report("fatos relevantes (CVM IPE)", collector.run(resume=resume))


@app.command("collect-cadastro")
def collect_cadastro(
    resume: bool = typer.Option(True, help="Pula o que ja foi coletado"),
) -> None:
    """Cadastro das companhias: CVM (cad_cia_aberta) e B3 (GetDetail por codigo CVM)."""
    _setup_logging()
    from tcc_scraper.collectors.b3_cadastro import B3CadastroCollector
    from tcc_scraper.collectors.cvm_cadastro import CvmCadastroCollector

    with CvmCadastroCollector() as collector:
        _report("cadastro (CVM)", collector.run(resume=resume))
    with B3CadastroCollector() as collector:
        _report("cadastro (B3)", collector.run(resume=resume))


@app.command("collect-proventos")
def collect_proventos(
    resume: bool = typer.Option(True, help="Pula o que ja foi coletado"),
) -> None:
    """Proventos (dividendos, JCP etc.) da B3."""
    _setup_logging()
    from tcc_scraper.collectors.b3_proventos import B3ProventosCollector

    with B3ProventosCollector() as collector:
        _report("proventos (B3)", collector.run(resume=resume))


@app.command("collect-indices")
def collect_indices(
    anos: Annotated[
        list[int] | None, typer.Option("--anos", help="Repita: --anos 2025 --anos 2026")
    ] = None,
    resume: bool = typer.Option(True, help="Pula o que ja foi coletado"),
) -> None:
    """CDI e IPCA do Banco Central (SGS 12 e 433)."""
    _setup_logging()
    from tcc_scraper.collectors.indices import IndicesBcbCollector

    with IndicesBcbCollector(anos=anos or None) as collector:
        _report("indices (BCB: CDI e IPCA)", collector.run(resume=resume))


@app.command("validate-cadastro")
def validate_cadastro() -> None:
    """Confere se cada ticker do universo tem codigo CVM e aparece na B3 desse mesmo codigo."""
    _setup_logging()
    from sqlalchemy import select

    from tcc_scraper.config import TICKER_CODE_CVM, TICKERS_DESLISTADOS, universo_coleta
    from tcc_scraper.db.models_fontes import RawB3Cadastro
    from tcc_scraper.db.session import session_scope

    with session_scope() as session:
        linhas = session.execute(
            select(RawB3Cadastro.cd_cvm, RawB3Cadastro.other_codes)
        ).all()
    por_cvm = {cd_cvm: set(other_codes or []) for cd_cvm, other_codes in linhas}

    ok, falhas, avisos = [], [], []
    for ticker in universo_coleta():
        cd_cvm = TICKER_CODE_CVM.get(ticker)
        if cd_cvm is None:
            motivo = "sem codigo CVM em TICKER_CODE_CVM"
        elif cd_cvm not in por_cvm:
            motivo = f"cd_cvm {cd_cvm} sem linha em v1-raw_b3_cadastro"
        elif ticker not in por_cvm[cd_cvm]:
            motivo = f"{ticker} ausente de other_codes do cd_cvm {cd_cvm}"
        else:
            ok.append(ticker)
            console.print(f"[green]ok[/green]    {ticker} (cd_cvm {cd_cvm})")
            continue
        if ticker in TICKERS_DESLISTADOS:
            avisos.append(ticker)
            console.print(f"[yellow]AVISO[/yellow] {ticker} (deslistado): {motivo}")
            continue
        falhas.append(ticker)
        console.print(f"[red]FALHA[/red] {ticker}: {motivo}")

    console.print(
        f"[green]validos: {len(ok)}[/green]  [yellow]avisos: {len(avisos)}[/yellow]  "
        f"[red]falhas: {len(falhas)}[/red]"
    )
    if falhas:
        raise typer.Exit(1)


@app.command("build-entrega")
def build_entrega_cmd(
    csv: bool = typer.Option(False, "--csv", help="Tambem grava cada tabela em CSV"),
    tabela: Annotated[
        list[str] | None, typer.Option("--tabela", help="Repita: --tabela A --tabela B")
    ] = None,
) -> None:
    """Monta as tabelas da entrega no banco (e em CSV com --csv)."""
    _setup_logging()
    from tcc_scraper.config import get_settings
    from tcc_scraper.entrega.builder import build_entrega

    gravadas = build_entrega(tabelas=tabela or None, csv=csv)
    _report("entrega", gravadas)
    if csv:
        pasta = get_settings().entrega_csv_dir
        for nome in gravadas:
            console.print(f"csv: {pasta / (nome + '.csv')}")


@app.command("judge-news")
def judge_news(
    limite: int | None = typer.Option(None, help="Maximo de itens nesta rodada"),
    avaliar: bool = typer.Option(
        False, "--avaliar", help="Mede precisao e revocacao no conjunto ouro, sem gravar"
    ),
) -> None:
    """Juiza de noticias: regra, Jev e Gemini sobre as pendentes."""
    _setup_logging()
    from tcc_scraper.config import PROJECT_ROOT
    from tcc_scraper.llm.juiza import avaliar_conjunto_ouro, julgar_pendentes

    if avaliar:
        caminho = PROJECT_ROOT / "tests" / "fixtures" / "juiza_gold.csv"
        if not caminho.exists():
            console.print(f"[yellow]conjunto ouro ausente: {caminho}[/yellow]")
            return
        try:
            metricas = avaliar_conjunto_ouro(caminho)
        except ValueError as exc:
            console.print(f"[yellow]avaliacao nao executada: {exc}[/yellow]")
            return
        console.print(metricas)
        return

    r = julgar_pendentes(limite_itens=limite)
    _report(
        "juiza de noticias",
        {
            "pendentes": r.pendentes,
            "regra": r.regra,
            "jev": r.jev,
            "gemini": r.gemini,
            "erros": r.erros,
            "runs": r.runs,
            "tokens_entrada": r.tokens_entrada,
            "tokens_saida": r.tokens_saida,
            "interrompido": r.interrompido,
        },
    )


@app.command("historico")
def historico_cmd(
    anos: Annotated[
        list[int] | None, typer.Option("--anos", help="Repita: --anos 2026 --anos 2025")
    ] = None,
    modo: str = typer.Option("pendentes", help="pendentes ou reprocessar_erros"),
) -> None:
    """Carga historica: dispara e espera (bloqueante) a coleta dos anos pedidos."""
    _setup_logging()
    if not anos:
        raise typer.BadParameter("informe ao menos um --anos")
    from tcc_scraper.orquestrador.historico import Historico
    from tcc_scraper.orquestrador.repositorio import RepositorioBanco

    historico = Historico(RepositorioBanco())
    execucao = historico.disparar(anos, modo)
    console.print(f"historico {execucao} disparado; aguardando conclusao")
    historico.join()
    console.print("historico encerrado")


if __name__ == "__main__":
    app()
