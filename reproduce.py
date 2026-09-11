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
from agent.case_bundle import load_bundle
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
    help="Path to a customer case file, or a case bundle directory "
         "(case.txt + configs/ + logs/ + dumps/).",
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
    text, bundle = _read_input(input_path)
    if not text:
        console.print("[red]No input provided. Use --input or pipe via stdin.[/red]")
        raise SystemExit(1)

    parser = CaseParser()
    case = parser.parse_text(text)
    if bundle:
        case.config_files.update(bundle.config_files)

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
    help="Path to a customer case file, or a case bundle directory "
         "(case.txt + configs/ + logs/ + dumps/).",
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
    text, bundle = _read_input(input_path)
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

    # The customer's real config files outrank anything scraped from prose.
    if bundle:
        case.config_files.update(bundle.config_files)
        if bundle.detected_product and not case.product:
            case.product = bundle.detected_product
        if bundle.detected_version and not case.version:
            case.version = bundle.detected_version

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
        customer_configs=dict(bundle.config_files) if bundle else {},
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

    # Step 5: Summary. The wording follows the package that was actually
    # written -- a JVM reproducer has no cluster and a Data Grid one has no
    # war, and telling the user to run ./build.sh when there is no build.sh
    # is how a working reproducer gets reported as broken.
    kind = generator._product_kind(analysis)
    if kind == "jvm":
        how = (
            "That resolves a JDK, compiles the workload, runs it under the\n"
            "flags in jvm.env with a heap dump on OOM, a GC log and periodic\n"
            "thread dumps, and reports whether the failure happened.\n"
            "Edit jvm.env first -- -Xmx and the collector are usually what\n"
            "decide whether the issue shows up at all."
        )
    elif kind == "datagrid":
        how = (
            "That resolves the Data Grid install and a JDK, brings up every\n"
            "node in its own terminal window, runs the cache test and reports\n"
            f"how often the issue reproduced. Nothing to export: the target\n"
            f"[cyan]{analysis.product} {analysis.version}[/cyan] comes from the case file, and the run\n"
            "aborts if DATAGRID_HOME points at a different major version.\n"
            "Add --no-terminals over SSH. Step by step instead:\n"
            "  ./start-cluster.sh -> ./test.sh -> ./stop-cluster.sh"
        )
    else:
        how = (
            "That builds the app, brings up every node in its own terminal\n"
            "window, starts the load balancer, runs the test and reports how\n"
            "often the issue reproduced. Nothing to export: the target\n"
            f"[cyan]{analysis.product} {analysis.version}[/cyan] comes from the case file, and the run\n"
            "aborts if EAP_HOME points at a different major version. A\n"
            "compatible JDK is resolved (and downloaded if missing) too.\n"
            "Add --no-terminals over SSH. Step by step instead:\n"
            "  ./build.sh -> ./start-cluster.sh -> ./test.sh -> ./stop-cluster.sh"
        )
    console.print()
    console.print(
        Panel(
            f"[bold green]Reproducer ready at:[/bold green] {reproducer_path}\n\n"
            "Next step:\n"
            "  ./run-reproducer.sh --repeat 5\n\n" + how,
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

def _read_input(input_path: str | None) -> tuple[str, object | None]:
    """Read the case from a file, a bundle directory, or stdin.

    Returns (text, bundle). `bundle` is None unless a directory was given, in
    which case it also carries the customer's config files and a manifest of
    what was actually read.
    """
    if input_path:
        path = Path(input_path)
        if path.is_dir():
            bundle = load_bundle(path)
            _display_bundle(bundle)
            return bundle.text, bundle
        return path.read_text(encoding="utf-8"), None

    # Check if stdin has data (not a TTY)
    if not sys.stdin.isatty():
        return sys.stdin.read(), None

    return "", None


def _display_bundle(bundle: object) -> None:
    """Show exactly which artifacts were ingested, and which were not."""
    table = Table(title="Case Bundle Ingested")
    table.add_column("Artifact", style="cyan")
    for entry in bundle.manifest:
        table.add_row(entry)
    console.print()
    console.print(table)

    if bundle.detected_product:
        console.print(
            f"[green]Detected from the customer's own logs:[/green] "
            f"{bundle.detected_product} {bundle.detected_version}"
        )
    for warning in bundle.warnings:
        console.print(f"[yellow]![/yellow] {warning}")


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
