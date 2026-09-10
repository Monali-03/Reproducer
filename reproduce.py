#!/usr/bin/env python3
"""Red Hat Support Reproducer Agent CLI.

Takes customer issue descriptions and generates complete reproducer packages
using Claude for intelligent analysis and code generation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from agent.analyzer import IssueAnalyzer
from agent.case_parser import CaseParser
from agent.generator import ReproducerGenerator
from agent.models import ReproducerConfig
from agent.template_engine import TemplateEngine
from agent.validator import ReproducerValidator

console = Console()


# -----------------------------------------------------------------------
# CLI group
# -----------------------------------------------------------------------

@click.group()
@click.version_option(version="1.0.0", prog_name="reproducer-agent")
def cli() -> None:
    """Red Hat Support Reproducer Agent.

    Analyzes customer issue descriptions and generates complete,
    minimal reproducer packages.
    """


# -----------------------------------------------------------------------
# analyze command
# -----------------------------------------------------------------------

@cli.command()
@click.option(
    "-i", "--input", "input_path",
    type=click.Path(exists=True),
    help="Path to customer case file.",
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    help="API key (or set ANTHROPIC_API_KEY / LLM_API_KEY).",
)
@click.option(
    "--model",
    default=None,
    help="LLM model name.",
)
@click.option(
    "--llm-url",
    envvar="LLM_BASE_URL",
    default=None,
    help="Base URL for OpenAI-compatible LLM endpoint.",
)
@click.option(
    "--llm-provider",
    envvar="LLM_PROVIDER",
    default=None,
    type=click.Choice(["anthropic", "openai", "internal"], case_sensitive=False),
    help="LLM provider type.",
)
def analyze(
    input_path: str | None,
    api_key: str | None,
    model: str | None,
    llm_url: str | None,
    llm_provider: str | None,
) -> None:
    """Analyze a customer case file and display the results."""
    text = _read_input(input_path)
    if not text:
        console.print("[red]No input provided. Use --input or pipe via stdin.[/red]")
        raise SystemExit(1)

    parser = CaseParser()
    case = parser.parse_text(text)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Analyzing issue...", total=None)
        analyzer = IssueAnalyzer(
            api_key=api_key, model=model,
            base_url=llm_url, provider=llm_provider,
        )
        analysis = analyzer.analyze(case)

    _display_analysis(analysis)


# -----------------------------------------------------------------------
# generate command
# -----------------------------------------------------------------------

@cli.command()
@click.option(
    "-i", "--input", "input_path",
    type=click.Path(exists=True),
    help="Path to customer case file.",
)
@click.option(
    "-o", "--output",
    default="./reproducer-output",
    show_default=True,
    help="Output directory for reproducer.",
)
@click.option("-p", "--product", default=None, help="Override product detection.")
@click.option("-v", "--version", "version_override", default=None, help="Override version detection.")
@click.option(
    "--local/--no-local",
    default=True,
    show_default=True,
    help="Generate local reproducer.",
)
@click.option(
    "--openshift/--no-openshift",
    default=True,
    show_default=True,
    help="Generate OpenShift reproducer.",
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    help="API key (or set ANTHROPIC_API_KEY / LLM_API_KEY).",
)
@click.option(
    "--model",
    default=None,
    help="LLM model name.",
)
@click.option(
    "--llm-url",
    envvar="LLM_BASE_URL",
    default=None,
    help="Base URL for OpenAI-compatible LLM endpoint.",
)
@click.option(
    "--llm-provider",
    envvar="LLM_PROVIDER",
    default=None,
    type=click.Choice(["anthropic", "openai", "internal"], case_sensitive=False),
    help="LLM provider type.",
)
@click.option(
    "--interactive",
    is_flag=True,
    default=False,
    help="Interactive mode -- prompt for missing info.",
)
@click.option(
    "--describe",
    is_flag=True,
    default=False,
    help="Only print the analysis without generating files.",
)
def generate(
    input_path: str | None,
    output: str,
    product: str | None,
    version_override: str | None,
    local: bool,
    openshift: bool,
    api_key: str | None,
    model: str | None,
    llm_url: str | None,
    llm_provider: str | None,
    interactive: bool,
    describe: bool,
) -> None:
    """Generate a reproducer from a customer case file."""
    text = _read_input(input_path)
    if not text:
        console.print("[red]No input provided. Use --input or pipe via stdin.[/red]")
        raise SystemExit(1)

    # Step 1: Parse
    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Parsing customer case...", total=None)
        parser = CaseParser()
        case = parser.parse_text(text)

    if product:
        case.product = product
    if version_override:
        case.version = version_override

    _display_parsed_case(case)

    # Interactive mode: prompt for missing critical fields
    if interactive:
        case = _interactive_fill(case)

    # Step 2: Analyze
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Analyzing issue...", total=None)
        analyzer = IssueAnalyzer(
            api_key=api_key, model=model,
            base_url=llm_url, provider=llm_provider,
        )
        analysis = analyzer.analyze(case)

    _display_analysis(analysis)

    # If --describe, stop here
    if describe:
        console.print(
            Panel(
                "[yellow]--describe flag set. Stopping before generation.[/yellow]",
                title="Describe Only",
            )
        )
        return

    # Step 3: Generate
    config = ReproducerConfig(
        output_dir=output,
        issue_analysis=analysis,
        local_reproducer=local,
        openshift_reproducer=openshift,
    )

    # Determine EAP version flags
    if "eap" in analysis.product.lower():
        try:
            major = int(analysis.version.split(".")[0])
            if major >= 8:
                config.eap8_app = True
            else:
                config.eap7_app = True
        except (ValueError, IndexError):
            config.eap7_app = True

    if "data grid" in analysis.product.lower() or "datagrid" in analysis.product.lower():
        config.datagrid = True
    if "jws" in analysis.product.lower() or "web server" in analysis.product.lower():
        config.jws = True

    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Generating reproducer package...", total=None)
        generator = ReproducerGenerator(
            api_key=api_key, model=model,
            base_url=llm_url, provider=llm_provider,
        )
        reproducer_path = generator.generate(config)
        progress.update(task, description="Generation complete.")

    console.print(
        Panel(
            f"[green]Reproducer generated at:[/green] {reproducer_path}",
            title="Generation Complete",
        )
    )

    # Step 4: Validate
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Validating reproducer...", total=None)
        validator = ReproducerValidator()
        results = validator.validate(reproducer_path)

    _display_validation(results)

    # Step 5: Summary
    console.print()
    console.print(
        Panel(
            f"[bold green]Reproducer ready at:[/bold green] {reproducer_path}\n\n"
            "Next steps:\n"
            "  1. export EAP_HOME=/path/to/jboss-eap\n"
            "  2. ./run-reproducer.sh --repeat 5\n\n"
            "That builds the app, brings up every node in its own terminal\n"
            "window, starts the load balancer, runs the test and reports how\n"
            "often the issue reproduced. A compatible JDK is resolved (and\n"
            "downloaded if missing) automatically -- no JAVA_HOME needed.\n"
            "Add --no-terminals over SSH. Step by step instead:\n"
            "  ./build.sh -> ./start-cluster.sh -> ./test.sh -> ./stop-cluster.sh",
            title="Done",
        )
    )


# -----------------------------------------------------------------------
# validate command
# -----------------------------------------------------------------------

@cli.command("validate")
@click.argument("reproducer_dir", type=click.Path(exists=True))
def validate_cmd(reproducer_dir: str) -> None:
    """Validate a generated reproducer directory."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Validating reproducer...", total=None)
        validator = ReproducerValidator()
        results = validator.validate(reproducer_dir)

    _display_validation(results)


# -----------------------------------------------------------------------
# templates command
# -----------------------------------------------------------------------

@cli.command("templates")
def templates_cmd() -> None:
    """List available templates."""
    engine = TemplateEngine()
    templates = engine.list_templates()

    if not templates:
        console.print(
            Panel(
                "[yellow]No templates found.[/yellow]\n"
                "Templates directory: templates/",
                title="Templates",
            )
        )
        return

    table = Table(title="Available Templates")
    table.add_column("Template", style="cyan")
    table.add_column("Type", style="green")

    for t in templates:
        ext = Path(t).suffix
        ttype = {
            ".j2": "Jinja2",
            ".xml": "XML",
            ".yaml": "YAML",
            ".yml": "YAML",
            ".java": "Java",
            ".sh": "Shell",
        }.get(ext, "Other")
        table.add_row(t, ttype)

    console.print(table)


# -----------------------------------------------------------------------
# Display helpers
# -----------------------------------------------------------------------

def _read_input(input_path: str | None) -> str:
    """Read input from file or stdin."""
    if input_path:
        return Path(input_path).read_text(encoding="utf-8")

    # Check if stdin has data (not a TTY)
    if not sys.stdin.isatty():
        return sys.stdin.read()

    return ""


def _display_parsed_case(case: object) -> None:
    """Display a summary of the parsed customer case."""
    table = Table(title="Parsed Customer Case")
    table.add_column("Field", style="cyan", width=20)
    table.add_column("Value", style="white")

    fields = [
        ("Product", case.product),
        ("Version", case.version),
        ("JDK", case.jdk),
        ("OS", case.os),
        ("Deployment Type", case.deployment_type),
        ("Application Type", case.application_type),
        ("Subsystem", case.subsystem),
    ]

    for label, value in fields:
        if value:
            table.add_row(label, value)

    if case.description:
        desc = case.description
        if len(desc) > 200:
            desc = desc[:200] + "..."
        table.add_row("Description", desc)

    if case.error_messages:
        err = case.error_messages
        if len(err) > 200:
            err = err[:200] + "..."
        table.add_row("Error Messages", err)

    console.print(table)


def _display_analysis(analysis: object) -> None:
    """Display the issue analysis in a rich panel."""
    table = Table(title="Issue Analysis", show_lines=True)
    table.add_column("Property", style="cyan", width=20)
    table.add_column("Value", style="white")

    rows = [
        ("Product", f"{analysis.product} {analysis.version}"),
        ("JDK", analysis.jdk),
        ("OS", analysis.os),
        ("Deployment", analysis.deployment_type),
        ("Nodes", str(analysis.num_nodes)),
        ("App Type", analysis.application_type),
        ("Subsystem", analysis.subsystem),
        ("Trigger", analysis.trigger),
        ("Error Signature", analysis.error_signature),
        ("Categories", ", ".join(analysis.categories)),
        ("Expected", analysis.expected_behavior),
        ("Actual", analysis.actual_behavior),
        ("Root Cause", analysis.possible_root_cause),
        ("Strategy", analysis.reproducer_strategy),
        ("Topology", analysis.topology_description),
    ]

    for label, value in rows:
        if value:
            table.add_row(label, value)

    console.print()
    console.print(table)


def _display_validation(results: dict) -> None:
    """Display validation results."""
    console.print()

    if results["valid"]:
        console.print(
            Panel("[bold green]Validation PASSED[/bold green]", title="Validation")
        )
    else:
        console.print(
            Panel("[bold red]Validation FAILED[/bold red]", title="Validation")
        )

    if results["errors"]:
        error_table = Table(title="Errors", style="red")
        error_table.add_column("#", width=4)
        error_table.add_column("Error")
        for i, err in enumerate(results["errors"], 1):
            error_table.add_row(str(i), err)
        console.print(error_table)

    if results["warnings"]:
        warn_table = Table(title="Warnings", style="yellow")
        warn_table.add_column("#", width=4)
        warn_table.add_column("Warning")
        for i, warn in enumerate(results["warnings"], 1):
            warn_table.add_row(str(i), warn)
        console.print(warn_table)

    if not results["errors"] and not results["warnings"]:
        console.print("[green]No issues found.[/green]")


def _interactive_fill(case: object) -> object:
    """Prompt the user to fill in missing critical fields."""
    console.print()
    console.print(
        Panel(
            "[yellow]Interactive mode: fill in any missing fields "
            "(press Enter to skip).[/yellow]",
            title="Interactive Input",
        )
    )

    if not case.product:
        value = console.input("[cyan]Product[/cyan] (e.g. EAP, JWS, Data Grid): ")
        if value.strip():
            case.product = value.strip()

    if not case.version:
        value = console.input("[cyan]Version[/cyan] (e.g. 7.4, 8.0): ")
        if value.strip():
            case.version = value.strip()

    if not case.jdk:
        value = console.input("[cyan]JDK[/cyan] (e.g. OpenJDK 11): ")
        if value.strip():
            case.jdk = value.strip()

    if not case.os:
        value = console.input("[cyan]OS[/cyan] (e.g. RHEL 8): ")
        if value.strip():
            case.os = value.strip()

    if not case.deployment_type:
        value = console.input(
            "[cyan]Deployment type[/cyan] (standalone/domain/openshift): "
        )
        if value.strip():
            case.deployment_type = value.strip()

    if not case.subsystem:
        value = console.input(
            "[cyan]Subsystem[/cyan] (e.g. undertow, ejb3, elytron): "
        )
        if value.strip():
            case.subsystem = value.strip()

    return case


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

if __name__ == "__main__":
    cli()
