"""A deterministic answer, read off the semantic layer instead of guessed from rows.

The assistant's expensive step is not the query — a GOLD read through the MCP
tools measures **9 ms**. It's the two language boundaries around it: on real
captured turns, planning took 2.8-3.1s and *synthesis* 2.8-7.1s, i.e. the model
spent up to 7 seconds narrating rows the UI had already drawn as a chart (see
``views/assistant.py``'s ``_render_tool_step``: "the chart/table *is* the
answer").

So for a single query the summary is arithmetic, and arithmetic belongs in code.
The thing that makes it *safe* to write in code is that Flashlight already has a
semantic layer: :mod:`flashlight.transform.catalog` declares, per view, which
columns are dimensions and which are measures, which measures are dollars
(``MEASURE_UNITS``) and which dimension is the charge period
(``PERIOD_DIMENSIONS``). An earlier version of this module inferred all of that
from the rows — ``isinstance`` checks for measures, substring matches for money
and periods, and a runtime functional-dependency scan to decide which dimension
mattered. That is re-deriving a declaration, and it was wrong in both directions:
``first_seen_month`` looked like a trend axis, ``savings_pct`` looked like money.

Two things this buys beyond latency:

* **It cannot hallucinate a number.** Every figure is summed from the rows the
  chart above it was drawn from. A model asked to restate 200 rows of currency
  can transpose a digit; this cannot.
* **It's free.** No tokens, no request, no retry budget, no round-limit failure.

Deliberately narrow. :func:`caption_for` returns ``None`` for anything it can't
state honestly — several queries, a non-additive measure, an unrecognised view —
and the caller falls back to model synthesis. A wrong caption would be far worse
than a slow answer.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from flashlight.transform.catalog import (
    GoldView,
    MeasureUnit,
    current_catalog_by_name,
    is_period_dimension,
    measure_unit,
)

if TYPE_CHECKING:
    # Import-time only: assistant_engine imports caption_for from here, so a real
    # import would be circular. Annotations are strings (see __future__ above), so
    # nothing needs ToolStep at runtime.
    from flashlight.dashboard.assistant_engine import ToolStep

# Units whose values can be summed. A total is the caption's opening claim, so a
# measure that doesn't add up (a percentage, a $/DBU price) has no honest headline
# and the turn goes to the model instead.
_ADDITIVE = frozenset(
    {MeasureUnit.CURRENCY, MeasureUnit.COUNT, MeasureUnit.QUANTITY}
)

# Column-name fallbacks, for the *chart renderer* only (views/assistant.py imports
# these). ``run_sql`` can return any column at all — an expression alias the catalog
# has never heard of — and a chart still has to decide whether to draw a "$" on the
# axis. The caption itself never uses these: it declines an undeclared measure
# rather than guess about a figure it's about to state as fact.
_MONEY_HINTS = ("cost", "amount", "spend", "price", "waste", "savings")
_TEMPORAL_HINTS = ("month", "date", "day", "period", "week", "year", "hour")


def is_money_column(name: str) -> bool:
    """Whether to render *name* as currency, preferring the catalog's declaration."""
    unit = measure_unit(name)
    if unit is not None:
        return unit in (MeasureUnit.CURRENCY, MeasureUnit.RATE)
    return any(hint in name.lower() for hint in _MONEY_HINTS)


def is_temporal_column(name: str) -> bool:
    """Whether *name* reads as a time axis, preferring the catalog's declaration."""
    return is_period_dimension(name) or any(hint in name.lower() for hint in _TEMPORAL_HINTS)


def _format(value: float, unit: MeasureUnit) -> str:
    """A figure in its declared unit, with no more precision than it justifies.

    The minus sign goes *before* the ``$``, unlike ``theme.compact_money``, which
    renders a KPI card's negative as ``$-45K``. Inside a sentence ``-$45,000`` is
    the form a reader parses without stumbling, and a net credit (a real AWS
    goodwill refund) is what makes it show up here.
    """
    if unit is MeasureUnit.CURRENCY:
        sign = "-" if value < 0 else ""
        magnitude = abs(value)
        body = f"{magnitude:,.0f}" if magnitude >= 100 else f"{magnitude:,.2f}"
        return f"{sign}${body}"
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _plural(unit: str, count: int) -> str:
    """Enough English to not embarrass itself in a sentence — a column name is a
    noun phrase, and ``service_category`` naively pluralized reads "service
    categorys". Not a general inflector: only the endings FOCUS column names
    actually produce."""
    if count == 1:
        return unit
    if unit.endswith("y") and not unit.endswith(("ay", "ey", "oy", "uy")):
        return f"{unit[:-1]}ies"
    return unit if unit.endswith("s") else f"{unit}s"


def _view_for(step: ToolStep) -> GoldView | None:
    """The catalog entry the step queried, or None for anything not a plain
    ``query_metric`` against a known view (``run_sql``, a dropped view)."""
    name = step.arguments.get("name")
    if step.name != "query_metric" or not isinstance(name, str):
        return None
    return current_catalog_by_name().get(name)


def _axes(view: GoldView, rows: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """``(category, period)`` for these rows, from the view's declared dimensions.

    Only dimensions that actually *vary* count — ``provider_name`` is present and
    constant on an already-provider-scoped view, and a filtered query pins others.

    The category is the **finest** varying non-period dimension, because the
    catalog lists dimensions coarse-to-fine: ``spend_by_service_month`` declares
    ``(provider_name, service_category, service_name, charge_month)``, so
    service_name is the label a reader wants ("AmazonEC2" says more than
    "Compute"). Declared order is what the earlier version computed functional
    dependencies at runtime to approximate.
    """
    varying = [
        dimension
        for dimension in view.dimensions
        if dimension in rows[0] and len({str(row.get(dimension)) for row in rows}) > 1
    ]
    period = next((d for d in varying if is_period_dimension(d)), None)
    categories = [d for d in varying if not is_period_dimension(d)]
    return (categories[-1] if categories else None), period


def _label(value: object) -> str:
    """A dimension value as a reader should see it.

    NULL is a real, meaningful group — untagged spend, a SKU with no description —
    and CLAUDE.md's attribution-honesty rule says it's surfaced, never dropped. But
    ``str(None)`` renders it as the word "None", which reads as a bug rather than as
    "not set".
    """
    if value is None or str(value).strip() == "":
        return "(not set)"
    return str(value)


def _totals_by(rows: list[dict[str, Any]], dimension: str, measure: str) -> dict[str, float]:
    """Sum the measure per distinct value of *dimension*, so a cross-tab's rows are
    aggregated before being ranked or trended, never sampled."""
    totals: dict[str, float] = {}
    for row in rows:
        key = _label(row.get(dimension))
        totals[key] = totals.get(key, 0.0) + float(row[measure])
    return totals




# A figure written into the template itself: a currency amount, a percentage, or any
# run of 2+ digits. The sentence is authored at *plan* time, before a single row has
# been read, so a literal figure in it cannot have come from the data — it is either
# invented or copied from an example. Small bare numbers are allowed through because
# they're legitimate prose ("the top 5 services", "over 3 months").
_LITERAL_FIGURE = re.compile(r"[$£€]\s*\d|\d\s*%|\d{2,}")


def render_sentence(sentence: str, facts: dict[str, str]) -> str | None:
    """Substitute *facts* into a model-declared *sentence*, or None to fall back.

    Refuses three things, each of which would defeat the point of the design:

    * a placeholder that isn't a known fact — the model asking for a figure nobody
      computed;
    * **a literal figure in the template**, which at plan time can only be
      fabricated. This is the guard that keeps "the model writes the words, code
      writes the numbers" true rather than merely intended;
    * a template with no placeholders at all, i.e. model prose written before it saw
      any data.

    Deliberately not ``str.format``: that would evaluate attribute and index lookups
    inside the template (``{total.__class__}``), and the template comes from a model.
    Only bare, known field names are accepted — the same ``Formatter().parse()``
    inspection ``policy_config.referenced_thresholds`` uses.
    """
    from string import Formatter

    try:
        parsed = list(Formatter().parse(sentence))
    except ValueError:  # malformed braces
        return None
    fields = [field for _, field, _, _ in parsed if field is not None]
    if not fields or any(field not in facts for field in fields):
        return None
    # Check the literal text between placeholders, not the whole sentence: a
    # placeholder name can't contain digits anyway, but a *substituted* figure
    # obviously does, so scanning the rendered output would reject everything.
    if any(_LITERAL_FIGURE.search(literal) for literal, _, _, _ in parsed if literal):
        return None
    rendered = sentence
    for field in fields:
        rendered = rendered.replace("{" + field + "}", facts[field])
    return rendered


def facts_for(steps: list[ToolStep]) -> dict[str, str] | None:
    """Every figure a one-line answer could need, formatted in its declared unit —
    or None when the rows have no honest reading (see :func:`caption_for`).

    This is the whole vocabulary a model may reference in a
    :class:`~flashlight.dashboard.assistant_engine.SummarySpec`: the model chooses
    the wording, these are the only numbers it can put in it, and each is summed
    from the rows the chart was drawn from. A model cannot introduce a figure that
    isn't here, which is the property that makes letting it write the prose safe.
    """
    if len(steps) != 1 or steps[0].error or not steps[0].rows:
        # One query, no errors. Several queries mean a comparison the model was
        # asked to reason across (_PLAN_INSTRUCTIONS plans one step per provider for
        # a cross-provider total), which is not a sum.
        return None
    step = steps[0]
    view = _view_for(step)
    if view is None:
        return None
    rows = step.rows or []

    # Measures the view declares *and* this query returned — `measures` narrows it,
    # so asking for one is what makes the subject unambiguous.
    returned = [m for m in view.measures if m in rows[0]]
    if len(returned) != 1:
        return None
    measure = returned[0]
    unit = measure_unit(measure)
    if unit is None or unit not in _ADDITIVE:
        # A percentage or a $/unit rate has no honest total to open with.
        return None

    # Rows with no value for this measure contribute nothing to a sum — the same
    # thing SQL's SUM does with NULL, and normal in a real view: forecast_month
    # carries forecast_cost only on its forecast rows and NULL on the actuals, which
    # is why every forecast view used to decline.
    rows = [row for row in rows if isinstance(row.get(measure), int | float)]
    if not rows:
        return None

    total = sum(float(row[measure]) for row in rows)
    facts = {
        "total": _format(total, unit),
        "measure": measure.replace("_", " "),
        "rows": f"{len(rows):,}",
    }

    category, period = _axes(view, rows)
    head_dimension = category or period
    if head_dimension is None:
        # Nothing varies: one row, or one group after filtering. There's no
        # breakdown to describe, but the figure *is* the answer — "what did I spend
        # last month?" is exactly this shape, and declining it sent the single most
        # ordinary question in the product to a synthesis call. Name whichever
        # charge period the rows are pinned to, so the total says what it covers.
        pinned = next(
            (
                _label(rows[0][d])
                for d in view.dimensions
                if is_period_dimension(d) and d in rows[0]
            ),
            None,
        )
        if pinned:
            facts["period_label"] = pinned
        return facts

    # Count distinct values of the dimension the head names, not len(rows): a
    # service x month cross-tab has 31 rows but 6 services, and "31 service names"
    # would simply be wrong.
    counts = _totals_by(rows, head_dimension, measure)
    facts |= {
        "count": f"{len(counts):,}",
        "dimension": _plural(head_dimension.replace("_", " "), len(counts)),
    }
    if period:
        periods = _totals_by(rows, period, measure)
        ordered = sorted(periods)
        first, last = ordered[0], ordered[-1]
        facts |= {
            "first_period": first,
            "last_period": last,
            "first_value": _format(periods[first], unit),
            "last_value": _format(periods[last], unit),
            "periods": f"{len(periods):,}",
        }
        if periods[first] > 0:
            facts["change_pct"] = f"{(periods[last] - periods[first]) / abs(periods[first]):+.0%}"
    if category:
        by_category = _totals_by(rows, category, measure)
        name = max(by_category, key=lambda key: by_category[key])
        facts |= {"top_name": name, "top_value": _format(by_category[name], unit)}
        if total > 0:
            facts["top_share"] = f"{by_category[name] / total:.0%}"
    return facts


def caption_for(steps: list[ToolStep], sentence: str | None = None) -> str | None:
    """A correct-by-construction summary of *steps*, or None to defer to the model.

    *sentence* is a model-declared template (see ``SummarySpec``); when it's absent
    or names an unknown placeholder, the default assembly below is used instead —
    the same ``ChartSpec`` -> row-shape-inference relationship, so a wrong or
    missing declaration degrades rather than breaking the answer.

    Returns None — deliberately, not as a failure — whenever the rows have no
    single honest one-line reading, so widening this is always optional.
    """
    facts = facts_for(steps)
    if facts is None:
        return None
    if sentence:
        rendered = render_sentence(sentence, facts)
        if rendered:
            return rendered
    return _default_caption(facts)


def _default_caption(facts: dict[str, str]) -> str:
    """The floor: a fixed assembly used when the model declared no sentence.

    Says less than a tailored sentence would — with no question to go on it can't
    know whether the trend or the ranking was the point, so it states whichever
    the rows support — but it is never wrong.
    """
    if "dimension" not in facts:
        # Nothing varied, so the total is the whole answer.
        where = f" in {facts['period_label']}" if "period_label" in facts else ""
        return f"{facts['total']} {facts['measure']}{where}"
    clauses = [
        f"{facts['total']} total {facts['measure']} across "
        f"{facts['count']} {facts['dimension']}"
    ]
    if "first_period" in facts:
        change = f" ({facts['change_pct']})" if "change_pct" in facts else ""
        clauses.append(
            f"{facts['first_period']} {facts['first_value']} -> "
            f"{facts['last_period']} {facts['last_value']}{change}"
        )
    # A ranking needs a categorical dimension. facts only carries top_* when there
    # is one, so "the biggest month of a trend" — which says only that one of six
    # is largest, after the trend already said it — can't be emitted here.
    if "top_share" in facts:
        clauses.append(f"top: {facts['top_name']} {facts['top_value']} ({facts['top_share']})")
    return " · ".join(clauses)
