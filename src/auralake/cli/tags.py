"""Tag governance commands."""
from __future__ import annotations

from typing import Annotated, Optional

import typer

from auralake.cli._options import (
    ApplyOption,
    AutoOption,
    ConfigOption,
    DryRunOption,
    OutputOption,
    PROption,
    ProviderOption,
    VerboseOption,
    WorkspaceOption,
)
from auralake.core.output import OutputFormat, print_warning, render_recommendations, render_table

tags_app = typer.Typer(no_args_is_help=True)


@tags_app.command("scan")
def tags_scan(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """Scan resources for missing or non-compliant tags."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.tag_analyzer import TagAnalyzer

    result = TagAnalyzer(ctx).analyze()

    render_table(
        "Tag Scan",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        OutputFormat(ctx.output_format),
    )
    if result.recommendations:
        render_recommendations(
            [
                {
                    "resource": r.resource_name,
                    "recommendation": r.title,
                    "estimated_savings": f"${r.estimated_monthly_savings_usd:.2f}/mo",
                    "priority": r.risk_level,
                }
                for r in result.recommendations
            ],
            OutputFormat(ctx.output_format),
        )


@tags_app.command("report")
def tags_report(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """Generate a tag compliance report."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.tag_analyzer import TagAnalyzer

    result = TagAnalyzer(ctx).analyze()

    render_table(
        "Tag Compliance Report",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        OutputFormat(ctx.output_format),
    )
    if result.recommendations:
        render_table(
            "Non-Compliant Resources",
            ["Resource", "Missing Tags", "Current Tags"],
            [
                [
                    r.resource_name,
                    ", ".join(r.recommended_state.get("missing_tags", [])),
                    str(r.current_state.get("tags", {})),
                ]
                for r in result.recommendations
            ],
            OutputFormat(ctx.output_format),
        )
    else:
        print_warning("All resources are tag-compliant.")


@tags_app.command("enforce")
def tags_enforce(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
    pr: PROption = False,
    tag_key: Annotated[
        Optional[str],
        typer.Option("--tag-key", help="Specific tag key to enforce."),
    ] = None,
    default_value: Annotated[
        Optional[str],
        typer.Option("--default-value", help="Default value to apply for missing tags."),
    ] = None,
) -> None:
    """Enforce tag policies by applying missing required tags."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose, dry_run, apply, auto, pr)

    from auralake.analyzers.tag_analyzer import TagAnalyzer

    result = TagAnalyzer(ctx).analyze()

    if not result.recommendations:
        print_warning("No tag violations found. All resources are compliant.")
        return

    render_recommendations(
        [
            {
                "resource": r.resource_name,
                "recommendation": r.title,
                "estimated_savings": f"${r.estimated_monthly_savings_usd:.2f}/mo",
                "priority": r.risk_level,
            }
            for r in result.recommendations
        ],
        OutputFormat(ctx.output_format),
    )

    if ctx.dry_run:
        print_warning("[DRY RUN] No changes applied.")
        return

    from auralake.actions.tag_actions import EnforceTagsAction

    action = EnforceTagsAction(ctx)
    for rec in result.recommendations:
        action.execute(rec)
