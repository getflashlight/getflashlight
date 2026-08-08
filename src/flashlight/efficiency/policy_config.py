"""User-tunable thresholds for the policy and waste rule pools.

The rule *pools* (:mod:`policy_rules`, :mod:`waste_rules`) are the shipped catalog —
what Flashlight knows how to check. This module is the small set of **numbers** in
those rules a FinOps team would set as org policy: how long a cluster may idle
before auto-termination counts as configured, what utilization reads as
underutilized, and so on.

Defaults are deliberately *efficient* — they encode good FinOps hygiene rather than
whatever a platform's own permissive default happens to be — so a user who never
writes a ``policies.yml`` still gets a meaningful compliance signal. Loaded from
``<FLASHLIGHT_HOME>/config/policies.yml``; a missing file is not an error.

Thresholds are substituted into rule SQL at transform time
(:func:`~flashlight.efficiency.policy_rules.build_policy_record_sql` and its waste
counterpart), so classification stays deterministic — every consumer reads the same
published GOLD, and no reader re-evaluates a rule against its own local config.
"""

from __future__ import annotations

from functools import lru_cache
from string import Formatter
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from flashlight.core.logging import get_logger
from flashlight.lake import paths

logger = get_logger(__name__)


class PolicyThresholds(BaseModel):
    """The tunable numbers behind the rule pools. Every field has an efficient default.

    Only thresholds a practitioner would genuinely set as policy live here — the
    remaining literals in ``waste_rules.py`` stay inline (marked ``ponytail:`` where
    they're candidates) rather than shipping thirty knobs nobody asked for.
    """

    model_config = ConfigDict(extra="forbid")

    max_auto_termination_minutes: int = Field(
        default=60,
        gt=0,
        description="An interactive cluster's auto-termination timeout must be set and "
        "no longer than this to count as compliant.",
    )
    max_warehouse_auto_stop_minutes: int = Field(
        default=30,
        gt=0,
        description="A SQL warehouse's auto-stop timeout must be set and no longer than "
        "this to count as compliant.",
    )
    low_traffic_endpoint_requests: int = Field(
        default=100,
        gt=0,
        description="A model serving endpoint below this many requests in a month counts as "
        "low-traffic, which is what makes always-on provisioned capacity (scale-to-zero off, "
        "or a large GPU class) worth reviewing.",
    )

    underutilized_pct: int = Field(
        default=20,
        ge=0,
        le=100,
        description="Average utilization at or below this reads as underutilized waste.",
    )


def _read_yaml(path: Any) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping, got {type(raw).__name__}")
    # Accept both a bare mapping and one nested under `thresholds:` so the scaffolded
    # file can carry a heading without the loader caring which shape it gets.
    nested = raw.get("thresholds")
    return nested if isinstance(nested, dict) else raw


@lru_cache
def get_thresholds() -> PolicyThresholds:
    """Cached thresholds: ``config/policies.yml`` merged over the efficient defaults.

    A missing file means "defaults" — this is an optional override, not required
    config. A malformed one is loud: silently falling back to defaults would change
    every classification in the lake without telling anyone.
    """
    path = paths.policies_path()
    if not path.exists():
        return PolicyThresholds()
    thresholds = PolicyThresholds.model_validate(_read_yaml(path))
    logger.info("policy_thresholds_loaded", path=str(path))
    return thresholds


def threshold_values() -> dict[str, int]:
    """Thresholds as the mapping rule SQL is ``.format()``-ed with."""
    return get_thresholds().model_dump()


def referenced_thresholds(*sql: str | None) -> dict[str, int]:
    """The effective threshold values the given rule SQL actually references.

    Lets a rule listing show the numbers it's really enforcing — "auto-terminate
    within 60 min" rather than an opaque "auto-termination policy" — without every
    rule having to restate them in its remedy text.
    """
    names = {
        field
        for fragment in sql
        if fragment
        for _, field, _, _ in Formatter().parse(fragment)
        if field
    }
    values = threshold_values()
    return {name: values[name] for name in sorted(names) if name in values}
