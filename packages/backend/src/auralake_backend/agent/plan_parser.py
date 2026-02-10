"""Spark EXPLAIN output parser.

Converts raw Spark physical plan text into structured PlanNode trees
and detects common anti-patterns.
"""

from __future__ import annotations

import re

from auralake_shared.models.query_plans import PlanAntiPattern, PlanNode, SparkPlan


class PlanParser:
    """Parses Spark EXPLAIN output into structured plans."""

    # Anti-pattern detection rules
    ANTI_PATTERNS = [
        {
            "pattern": r"Scan\s+\S+\s+\[.*\]\s*$",
            "check_no_filter": True,
            "type": "full_scan",
            "description": "Full table scan without partition pruning",
            "severity": "high",
            "recommendation": "Add partition filter predicates to reduce data scanned.",
        },
        {
            "pattern": r"BroadcastNestedLoopJoin|CartesianProduct",
            "type": "bad_join",
            "description": "Cartesian product or broadcast nested loop join detected",
            "severity": "high",
            "recommendation": (
                "Add proper join conditions or use explicit broadcast hints for small tables."
            ),
        },
        {
            "pattern": r"SortMergeJoin",
            "check_skew": True,
            "type": "potential_skew",
            "description": "Sort-merge join may indicate data skew",
            "severity": "medium",
            "recommendation": (
                "Check for data skew. Consider salting keys or broadcast join for smaller table."
            ),
        },
        {
            "pattern": r"Exchange\s+(hashpartitioning|rangepartitioning)",
            "type": "excessive_shuffle",
            "description": "Shuffle exchange detected — data redistribution across executors",
            "severity": "medium",
            "recommendation": (
                "Consider repartitioning data or adjusting spark.sql.shuffle.partitions."
            ),
        },
    ]

    def parse(self, query_id: str, plan_text: str, query_text: str | None = None) -> SparkPlan:
        """Parse a Spark EXPLAIN output into a SparkPlan."""
        nodes = self._parse_nodes(plan_text)
        anti_patterns = self._detect_anti_patterns(plan_text)

        return SparkPlan(
            query_id=query_id,
            query_text=query_text,
            physical_plan=plan_text,
            parsed_nodes=nodes,
            anti_patterns=anti_patterns,
        )

    def _parse_nodes(self, plan_text: str) -> list[PlanNode]:
        """Parse plan text into a list of PlanNode objects."""
        nodes = []
        lines = plan_text.strip().split("\n")

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("=="):
                continue

            # Extract node type and name
            indent = len(line) - len(line.lstrip())
            match = re.match(r"[+\-:*\s]*(\w+)\s*(.*)", stripped)
            if match:
                node_type = match.group(1)
                rest = match.group(2).strip()
                nodes.append(
                    PlanNode(
                        node_type=node_type,
                        name=rest[:100] if rest else node_type,
                        properties={"indent": indent, "raw": stripped},
                    )
                )

        return nodes

    def _detect_anti_patterns(self, plan_text: str) -> list[PlanAntiPattern]:
        """Detect anti-patterns in the plan text."""
        patterns_found = []

        for rule in self.ANTI_PATTERNS:
            matches = re.findall(rule["pattern"], plan_text, re.MULTILINE)
            if matches:
                patterns_found.append(
                    PlanAntiPattern(
                        type=rule["type"],
                        description=rule["description"],
                        severity=rule["severity"],
                        recommendation=rule.get("recommendation"),
                    )
                )

        return patterns_found
