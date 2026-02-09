"""Cluster policy management commands."""
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

policies_app = typer.Typer(no_args_is_help=True)


@policies_app.command("audit")
def policies_audit(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """Audit existing cluster policies for cost governance gaps."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.policy_analyzer import PolicyAnalyzer

    result = PolicyAnalyzer(ctx).analyze()

    render_table(
        "Policy Audit",
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


@policies_app.command("create")
def policies_create(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
    pr: PROption = False,
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Policy name."),
    ] = "",
    template: Annotated[
        Optional[str],
        typer.Option("--template", help="Policy template: cost-optimized, balanced, performance."),
    ] = None,
) -> None:
    """Create a new cluster policy from a template."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose, dry_run, apply, auto, pr)
    # TODO: implement policy creation
    print_warning("Policy creation not yet implemented.")


@policies_app.command("recommend")
def policies_recommend(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """Recommend policy improvements based on cluster usage patterns."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.policy_analyzer import PolicyAnalyzer

    result = PolicyAnalyzer(ctx).analyze()

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
    else:
        print_warning("No policy recommendations. All clusters follow best practices.")


@policies_app.command("apply")
def policies_apply(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
    pr: PROption = False,
    policy_id: Annotated[
        Optional[str],
        typer.Option("--policy-id", help="Policy ID to apply."),
    ] = None,
) -> None:
    """Apply a cluster policy to target clusters."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose, dry_run, apply, auto, pr)

    from auralake.analyzers.policy_analyzer import PolicyAnalyzer

    result = PolicyAnalyzer(ctx).analyze()

    if not result.recommendations:
        print_warning("No policy violations found to apply fixes.")
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

    from auralake.actions.policy_actions import SetAutotermination

    action = SetAutotermination(ctx)
    for rec in result.recommendations:
        if rec.type == "policy_no_autotermination":
            action.execute(rec)
