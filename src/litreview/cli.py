"""CLI interface for the literature review pipeline."""

from __future__ import annotations

import asyncio
import functools
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from litreview.config import get_config
from litreview.models import PICOQuestion
from litreview.pipeline.orchestrator import run_pipeline
from litreview.pipeline.quarto_renderer import render_quarto, write_outputs
from litreview.utils.statistics import format_statistics_table

if TYPE_CHECKING:  # the brief pipeline is imported lazily, per command
    from litreview.pipeline.brief_flow import PicoState

app = typer.Typer(name="lit-review", help="Robust Literature Review Pipeline")
console = Console()


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@app.command()
def review(
    topic: str = typer.Argument(..., help="Research topic for the literature review"),
    terms: list[str] = typer.Option(None, "--term", "-t", help="Additional search terms"),
    max_results: int = typer.Option(100, "--max-results", "-n", help="Max results per database"),
    min_quartile: str = typer.Option("Q1", "--min-quartile", "-q", help="Minimum SJR quartile (Q1, Q2, Q3, Q4)"),
    min_citescore: float = typer.Option(3.0, "--min-citescore", help="Minimum CiteScore fallback threshold"),
    target: int = typer.Option(50, "--target", help="Target number of articles"),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o", help="Output directory"),
    render: bool = typer.Option(True, "--render/--no-render", help="Render Quarto to PDF/DOCX"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Run a complete literature review on a topic."""
    setup_logging(verbose)

    config = get_config()
    config.max_results_per_db = max_results
    config.min_quartile = min_quartile
    config.min_citescore = min_citescore
    config.target_articles = target
    config.output_dir = output_dir

    # Show configuration
    keys = config.validate_keys()
    console.print(Panel(f"[bold]Literature Review: {topic}[/bold]", style="blue"))

    table = Table(title="API Configuration")
    table.add_column("Service", style="cyan")
    table.add_column("Status", style="green")
    for service, configured in keys.items():
        status = "[green]configured[/green]" if configured else "[red]missing[/red]"
        table.add_row(service, status)
    console.print(table)

    # Run pipeline
    with console.status("[bold green]Running pipeline..."):
        output = asyncio.run(run_pipeline(topic, terms, config))

    # Write files
    console.print("\n[bold]Writing outputs...[/bold]")
    paths = write_outputs(output, output_dir)
    for name, path in paths.items():
        console.print(f"  {name}: {path}")

    # Render
    if render:
        console.print("\n[bold]Rendering Quarto...[/bold]")
        rendered = render_quarto(output_dir)
        for fmt, path in rendered.items():
            console.print(f"  {fmt}: {path}")

    # Show statistics
    console.print(Panel(format_statistics_table(output.statistics), title="Review Statistics"))
    console.print(f"\n[bold green]Done![/bold green] {len(output.articles)} articles reviewed.")


@app.command()
def validate(
    bib_file: Path = typer.Argument(..., help="BibTeX file to validate"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Validate all DOIs in a BibTeX file."""
    import re

    setup_logging(verbose)

    from litreview.utils.doi_validator import batch_validate_dois

    content = bib_file.read_text()
    dois = re.findall(r"doi\s*=\s*\{([^}]+)\}", content)

    console.print(f"Found {len(dois)} DOIs to validate")

    with console.status("[bold green]Validating DOIs..."):
        results = asyncio.run(batch_validate_dois(dois))

    valid = sum(1 for v in results.values() if v)
    invalid = [doi for doi, v in results.items() if not v]

    console.print(f"\n[green]{valid}[/green] valid, [red]{len(invalid)}[/red] invalid")
    for doi in invalid:
        console.print(f"  [red]Invalid:[/red] {doi}")


@app.command()
def build_site(
    appraisal: Path = typer.Argument(..., help="Path to appraisal.json (the web render contract)"),
    output_dir: Path = typer.Option(Path("output/site"), "--output", "-o", help="Site output directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Render a ClaimAppraisal (appraisal.json) into the verdict-style static site."""
    setup_logging(verbose)
    from litreview.pipeline.site_renderer import render_site

    if not appraisal.exists():
        console.print(f"[red]Not found:[/red] {appraisal}")
        raise typer.Exit(1)

    index = render_site(appraisal, output_dir)
    console.print(f"[green]Site rendered:[/green] {index}")
    console.print(f"  Preview locally: [cyan]wrangler pages dev {output_dir}[/cyan]")
    console.print(f"  Deploy:          [cyan]wrangler pages deploy {output_dir} --project-name=<project>[/cyan]")


brief_app = typer.Typer(
    name="brief",
    help="Evidence brief: question -> PubMed-only PICO search -> GRADE -> one page.",
    pretty_exceptions_enable=False,
)
app.add_typer(brief_app, name="brief")

# What the brief stages raise when the state on disk is not what the command
# needs: a missing artefact, a broken contract, a failed runner. Each is a
# message the operator can act on, so it is printed as one line — a traceback
# through Typer would only bury it.
_BRIEF_ERRORS = (FileNotFoundError, ValueError, RuntimeError, subprocess.TimeoutExpired)


def brief_command(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn the brief stages' expected failures into one red line + exit 1."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            # typer.Exit subclasses RuntimeError; it is control flow, not a failure.
            raise
        except _BRIEF_ERRORS as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e

    return wrapper


def _brief_base(slug: str, output_dir: Path) -> Path:
    return output_dir / slug


@brief_app.command("search")
@brief_command
def brief_search(
    slug: str = typer.Argument(..., help="Brief slug; reads output/<slug>/picos.json"),
    pico: str = typer.Option(None, "--pico", help="Run only this pico_id (default: all)"),
    max_results: int = typer.Option(30, "--max-results", "-n", help="Results per search pass per PICO (there are up to 4 passes)"),
    min_year: int = typer.Option(2000, "--min-year", help="Earliest publication year"),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """PubMed -> dedup -> year -> Q1 -> DOI -> CrossRef for each PICO; writes pico_NN.json."""
    setup_logging(verbose)
    from litreview.pipeline.brief import brief_config, load_brief, search_pico

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    if pico:
        picos = [p for p in picos if p.pico_id == pico]
        if not picos:
            console.print(f"[red]No PICO with id {pico}[/red]")
            raise typer.Exit(1)
    cfg = brief_config(max_results=max_results, min_year=min_year)

    async def _run():
        for p in picos:
            with console.status(f"[bold green]{p.pico_id}: searching PubMed..."):
                flow = await search_pico(base, p, cfg)
            console.print(
                f"[cyan]{p.pico_id}[/cyan] found={flow.total_found} dedup={flow.after_dedup} "
                f"year={flow.after_year_filter} Q1={flow.after_quality_filter} "
                f"doi={flow.after_validation} crossref={flow.after_crossref} "
                f"citation=+{flow.included_by_citation}/{flow.identified_by_citation} "
                f"-> [bold]{flow.included} included[/bold]"
            )

    asyncio.run(_run())


def _print_studies(base: Path, picos: list[PICOQuestion]) -> None:
    """The expert-checkpoint table: included trials and syntheses, one table per PICO."""
    from litreview.pipeline.brief import list_evidence

    for p in picos:
        rows = list_evidence(base, p.pico_id)
        table = Table(title=f"{p.pico_id}: {p.question_text or p.outcome}  ({len(rows)} trials/syntheses)")
        table.add_column("Year", style="cyan", no_wrap=True)
        table.add_column("Design", no_wrap=True)
        table.add_column("Journal", max_width=22, overflow="ellipsis", no_wrap=True)
        table.add_column("Title", ratio=3, overflow="fold")
        table.add_column("PMID", no_wrap=True)
        for a in rows:
            table.add_row(str(a.year or ""), a.pub_type, a.journal, a.title, a.pmid or "")
        console.print(table)


@brief_app.command("studies")
@brief_command
def brief_studies(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """List the included trials and syntheses per PICO — for the expert to spot a missing landmark."""
    from litreview.pipeline.brief import load_brief

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    _print_studies(base, picos)


@brief_app.command("add")
@brief_command
def brief_add(
    slug: str = typer.Argument(...),
    pico: str = typer.Argument(..., help="pico_id to add to"),
    ids: list[str] = typer.Option(..., "--id", help="PMID or DOI (repeatable)"),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Add studies an expert named. Same gates as the search; rejections are explained."""
    setup_logging(verbose)
    from litreview.pipeline.brief import add_studies, brief_config

    base = _brief_base(slug, output_dir)
    added, rejected = asyncio.run(add_studies(base, pico, ids, brief_config()))
    for a in added:
        console.print(f"[green]added[/green] {a.year} {a.pub_type:14} {a.title[:70]}")
    for ident, why in rejected.items():
        console.print(f"[red]rejected[/red] {ident}: {why}")
    if not added and not rejected:
        console.print("nothing to do")


@brief_app.command("grade-tasks")
@brief_command
def brief_grade_tasks(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Write tasks/grade_<pico>.md for each PICO; dispatch one Sonnet agent per file."""
    from litreview.pipeline.brief import load_brief, write_grade_task

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    from litreview.pipeline.brief import write_evidence_view

    for p in picos:
        path = write_grade_task(base, p.pico_id)
        _ev, shown, total = write_evidence_view(base, p.pico_id)
        console.print(
            f"[cyan]{p.pico_id}[/cyan] -> {path}   agent sees {shown}/{total} studies; "
            f"writes {base / f'grade_{p.pico_id}.json'}"
        )


@brief_app.command("screen-tasks")
@brief_command
def brief_screen_tasks(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Write batched abstract-screening tasks for every PICO."""
    from litreview.pipeline.brief import load_brief, write_screen_tasks

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    for p in picos:
        paths = write_screen_tasks(base, p.pico_id)
        console.print(f"[cyan]{p.pico_id}[/cyan] -> {len(paths)} screen batch(es)")
        for path in paths:
            console.print(f"  {path}")


@brief_app.command("collect-screen")
@brief_command
def brief_collect_screen(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Merge screening-agent JSON batches for every PICO."""
    from litreview.pipeline.brief import collect_screen, load_brief

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    for p in picos:
        screen = collect_screen(base, p.pico_id)
        total = len(screen["included"]) + len(screen["excluded"])
        console.print(f"[cyan]{p.pico_id}[/cyan] screened {len(screen['included'])}/{total} included")


@brief_app.command("fulltext")
@brief_command
def brief_fulltext(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Download + convert full text for each PICO's body of evidence (via JournalFetcher)."""
    setup_logging(verbose)
    from litreview.pipeline.brief import brief_config, fetch_fulltext, load_brief

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    cfg = brief_config()
    for p in picos:
        with console.status(f"[bold green]{p.pico_id}: fetching full text..."):
            index = fetch_fulltext(base, p.pico_id, cfg)
        for e in index["body_of_evidence"]:
            mark = "[green]ok[/green]" if e["status"] == "ok" else f"[red]failed[/red] {e['error']}"
            console.print(f"  {e['citation_key']:28} {mark}" + (f"  {e['chars']:,} chars" if e["chars"] else ""))


@brief_app.command("verify-tasks")
@brief_command
def brief_verify_tasks(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Write tasks/verify_<pico>.md: the GRADE agent re-rates from full text (risk of bias, numbers)."""
    from litreview.pipeline.brief import load_brief, write_verify_task

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    for p in picos:
        path = write_verify_task(base, p.pico_id)
        console.print(f"[cyan]{p.pico_id}[/cyan] -> {path}   (agent overwrites {base / f'grade_{p.pico_id}.json'})")


@brief_app.command("write-tasks")
@brief_command
def brief_write_tasks(
    slug: str = typer.Argument(...),
    min_year: int = typer.Option(2000, "--min-year"),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Collect GRADE, derive each verdict, write tasks/write_<pico>.md for the writer agents."""
    from litreview.pipeline.brief import CERTAINTY_ZH, VERDICT_ZH, collect_verdict, load_brief, write_writer_task

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    for p in picos:
        _grade, verdict = collect_verdict(base, p.pico_id)
        path = write_writer_task(base, p.pico_id, min_year=min_year)
        console.print(
            f"[cyan]{p.pico_id}[/cyan] {VERDICT_ZH[verdict.verdict]} / 確定性 {CERTAINTY_ZH[verdict.confidence]}"
            f" -> {path}   (agent writes {base / f'write_{p.pico_id}.json'})"
        )


@brief_app.command("check")
@brief_command
def brief_check(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Verify every DOI the writers cited is in that PICO's included set. Exit 1 on any unknown."""
    from litreview.pipeline.brief import check_citations, load_brief

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    problems = check_citations(base, [p.pico_id for p in picos])
    failed = False
    for pico_id, unknown in problems.items():
        if unknown:
            failed = True
            console.print(f"[red]{pico_id}[/red] cites {len(unknown)} unknown DOI(s):")
            for d in unknown:
                console.print(f"    {d}")
        else:
            console.print(f"[green]{pico_id}[/green] all citations in included set")
    if failed:
        raise typer.Exit(1)


@brief_app.command("render")
@brief_command
def brief_render(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
    out: Path = typer.Option(None, "--out", help="HTML path (default output/<slug>/brief.html)"),
):
    """Assemble brief.html from the JSON on disk (runs the citation check first)."""
    from litreview.pipeline.brief import render_brief

    base = _brief_base(slug, output_dir)
    try:
        path = render_brief(base, out)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Rendered:[/green] {path}")


# ---------------------------------------------------------------------------
# Flow control: doctor / preview -> next (loop) -> status
# ---------------------------------------------------------------------------


def _print_status(base: Path, states: list[PicoState], fulltext: bool) -> None:
    """One row per PICO: where it is and what is known about it so far."""
    from litreview.pipeline.brief import CERTAINTY_ZH, VERDICT_ZH
    from litreview.pipeline.brief_flow import checkpoint_done

    table = Table(title=f"{base.name}: {len(states)} PICO")
    table.add_column("PICO", style="cyan", no_wrap=True)
    table.add_column("Included", justify="right")
    table.add_column("Screen", justify="right", no_wrap=True)
    table.add_column("Stage", no_wrap=True)
    table.add_column("GRADE", no_wrap=True)
    table.add_column("Full text", justify="right", no_wrap=True)
    table.add_column("Verified", justify="center")
    table.add_column("Written", justify="center")
    for st in states:
        grade = "–"
        if st.certainty:
            grade = (f"{CERTAINTY_ZH.get(st.certainty, st.certainty)} / "
                     f"{VERDICT_ZH.get(st.verdict, st.verdict)}")
        screen = f"{st.screen_included}/{st.screen_total}" if st.screened else "–"
        fulltext_cell = f"{st.fulltext_ok}/{st.fulltext_total}" if st.fulltext_total else "n/a"
        table.add_row(st.pico_id, str(st.included), screen, st.stage, grade, fulltext_cell,
                      "✓" if st.verified else "–", "✓" if st.written else "–")
    console.print(table)
    console.print(
        f"專家檢查點：{'[green]已完成[/green]' if checkpoint_done(base) else '[yellow]未完成[/yellow]'}"
        f"　　全文：{'[green]開啟[/green]' if fulltext else '[yellow]關閉（摘要層級）[/yellow]'}"
    )


@brief_app.command("status")
@brief_command
def brief_status(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Where every PICO stands: stage, GRADE, full-text coverage, checkpoint."""
    from litreview.pipeline.brief import brief_config
    from litreview.pipeline.brief_flow import flow_state

    base = _brief_base(slug, output_dir)
    _question, _picos, states, fulltext = flow_state(base, brief_config())
    _print_status(base, states, fulltext)


@brief_app.command("checkpoint")
@brief_command
def brief_checkpoint(
    slug: str = typer.Argument(...),
    note: str = typer.Option("", "--note", help="What the expert said (kept in checkpoint_log.json)"),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Record that the expert has reviewed the included studies; unblocks `next`."""
    from litreview.pipeline.brief import load_brief
    from litreview.pipeline.brief_flow import CHECKPOINT_LOG, record_checkpoint

    base = _brief_base(slug, output_dir)
    load_brief(base)  # fail loudly if this is not a brief directory
    record_checkpoint(base, note)
    console.print(f"[green]expert checkpoint recorded[/green] -> {base / CHECKPOINT_LOG}")


def _print_gaps(base: Path, picos: list[PICOQuestion], limit: int) -> None:
    """Print persisted gate exclusions, ranked by citations."""
    from litreview.pipeline.brief import load_gaps

    for p in picos:
        try:
            rows = load_gaps(base, p.pico_id, limit=limit)
        except FileNotFoundError as e:
            console.print(f"[yellow]{p.pico_id}[/yellow]: {e}")
            continue
        table = Table(title=f"{p.pico_id}: gate near-misses (top {limit})")
        table.add_column("Year", no_wrap=True)
        table.add_column("Cites", justify="right", no_wrap=True)
        table.add_column("Reason", no_wrap=True)
        table.add_column("Journal", overflow="ellipsis", max_width=20)
        table.add_column("Title", ratio=3, overflow="fold")
        for row in rows:
            table.add_row(
                str(row.get("year") or ""), str(row.get("citation_count") or 0),
                str(row.get("reason") or ""), str(row.get("journal") or ""),
                str(row.get("title") or ""),
            )
        console.print(table)
        console.print("  [dim]These records did not pass the deterministic gates.[/dim]")


@brief_app.command("gaps")
@brief_command
def brief_gaps(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Show the most-cited records excluded by deterministic gates."""
    from litreview.pipeline.brief import load_brief

    base = _brief_base(slug, output_dir)
    _question, picos = load_brief(base)
    _print_gaps(base, picos, limit=10)


@brief_app.command("next")
@brief_command
def brief_next(
    slug: str = typer.Argument(...),
    max_results: int = typer.Option(30, "--max-results", "-n", help="Results per search pass per PICO"),
    min_year: int = typer.Option(2000, "--min-year", help="Earliest publication year"),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run every deterministic stage that is ready, then say what is needed next."""
    setup_logging(verbose)
    from litreview.pipeline.brief import brief_config, load_brief
    from litreview.pipeline.brief_flow import run_next

    base = _brief_base(slug, output_dir)
    cfg = brief_config(max_results=max_results, min_year=min_year)
    result = asyncio.run(run_next(base, cfg))

    for action in result.actions:
        console.print(f"  {action}")

    if result.status == "checkpoint":
        _question, picos = load_brief(base)
        _print_studies(base, picos)
        _print_gaps(base, picos, limit=5)
        console.print(
            f"[bold yellow]專家檢查點[/bold yellow]：有漏掉的 landmark 研究就 "
            f"[cyan]lit-review brief add {slug} <pico> --id <PMID|DOI>[/cyan]，完成後 "
            f"[cyan]lit-review brief checkpoint {slug}[/cyan]"
        )
    elif result.status == "dispatch":
        console.print(f"\n[bold]→ dispatch {len(result.dispatch)} agent(s) in ONE message:[/bold]")
        for d in result.dispatch:
            console.print(f"   {d.kind} {d.pico_id}   model={d.model}   {d.prompt}", highlight=False)
        console.print()
    else:
        console.print(f"[green]{result.status}:[/green] {result.rendered}")

    _print_status(base, result.states, result.fulltext_enabled)


@brief_app.command("doctor")
@brief_command
def brief_doctor(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """Pre-flight: the picos.json contract, credentials, and whatever is already on disk."""
    from litreview.pipeline.brief import brief_config
    from litreview.pipeline.brief_flow import run_doctor

    checks = run_doctor(_brief_base(slug, output_dir), brief_config())
    for c in checks:
        tag = "[green]ok[/green]" if c.ok else ("[yellow]warn[/yellow]" if c.warn else "[red]FAIL[/red]")
        console.print(f"{tag} {c.name}: {c.detail}", highlight=False)
    if any(not c.ok and not c.warn for c in checks):
        raise typer.Exit(1)


@brief_app.command("preview")
@brief_command
def brief_preview(
    slug: str = typer.Argument(...),
    output_dir: Path = typer.Option(Path("output"), "--output", "-o"),
):
    """PubMed hit counts per PICO and per term — a dead term costs a whole run."""
    from litreview.pipeline.brief import brief_config
    from litreview.pipeline.brief_flow import run_preview

    base = _brief_base(slug, output_dir)
    cfg = brief_config()
    with console.status("[bold green]Counting PubMed hits..."):
        rows = asyncio.run(run_preview(base, cfg))

    for row in rows:
        table = Table(title=f"{row['pico_id']}: PubMed hits (>= {cfg.min_year})")
        table.add_column("Group", style="cyan", no_wrap=True)
        table.add_column("Term", overflow="fold")
        table.add_column("Hits", justify="right", no_wrap=True)
        table.add_row("query", "(intervention) AND (outcome)", f"{row['query']:,}")
        table.add_row("design sweep", "+ RCT / meta-analysis / systematic review", f"{row['design_sweep']:,}")
        for t in row["terms"]:
            # 0 hits means the term never matches and only narrows the AND;
            # a very broad term drowns the relevance ranking.
            style = "red" if t["hits"] == 0 else "yellow" if t["hits"] > 50_000 else None
            table.add_row(t["group"], t["term"], f"{t['hits']:,}", style=style)
        console.print(table)
        dead = [t["term"] for t in row["terms"] if t["hits"] == 0]
        if dead:
            console.print(f"  [red]0 hits — fix before searching:[/red] {', '.join(dead)}")


@app.command()
def check_config():
    """Check API key configuration."""
    config = get_config()
    keys = config.validate_keys()

    table = Table(title="API Configuration")
    table.add_column("Service", style="cyan")
    table.add_column("Status")
    for service, configured in keys.items():
        status = "[green]OK[/green]" if configured else "[red]MISSING[/red]"
        table.add_row(service, status)
    console.print(table)


if __name__ == "__main__":
    app()
