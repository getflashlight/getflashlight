"""Pins the claim that lets SILVER drop the billing period (see docs/architecture.md).

``BillingPeriodStart``/``End`` are stored on every BRONZE row but never projected into
``silver.focus_normalized``, partly because they are empirically a redundant derivation
of ``charge_month``. That is a statement about *data*, not about code, so it can quietly
stop being true when a connector is added or an upstream export changes shape — hence a
test rather than a comment. If this fails, the honest fix is usually to carry the columns
into SILVER and give the new billing cycle a real dimension, not to loosen the assertion.

Runs against the schema-driven local demo generator, so it does not depend on a
committed public-demo lake.

Two details this test exists to get right:

* It must run on a :func:`flashlight.lake.duck.connect` connection, because that is what
  pins ``SET TimeZone='UTC'``. ``charge_period_start`` is TIMESTAMPTZ, and ``date_trunc``
  resolves it in the *session* zone first — on any host west of UTC a charge stamped
  midnight UTC on the 1st truncates into the previous month, so an unpinned connection
  reports hundreds of false violations. That tz trap is most of this test's value.
* Oracle is excluded. The FinOps FOCUS sample contains exactly one synthetic Oracle row
  whose billing period genuinely differs from its charge month (charge 2024-09, billing
  2024-10 → 2024-11). It is real FOCUS-legal data and a useful reminder that the general
  case exists; it just isn't what any connector we ship actually emits.
"""

from __future__ import annotations

from flashlight.core.settings import get_settings

# Both halves of the claim, as one scan. Casting to DATE on the left keeps the comparison
# against the date32 billing_period_* columns type-clean.
_SQL = """
SELECT
    count(*)                                                        AS total,
    count(*) FILTER (
        WHERE billing_period_start
              <> date_trunc('month', charge_period_start)::DATE)    AS bad_start,
    count(*) FILTER (
        WHERE billing_period_end
              <> (date_trunc('month', charge_period_start) + INTERVAL 1 MONTH)::DATE
    )                                                               AS bad_end
FROM raw.focus_record
WHERE provider_name <> 'Oracle'
"""


def test_billing_period_is_a_redundant_derivation_of_charge_month(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    try:
        from flashlight.sample import load_sample

        load_sample()
        from flashlight.lake import duck

        con = duck.connect()
        try:
            duck.register_bronze(con)
            total, bad_start, bad_end = con.execute(_SQL).fetchall()[0]
        finally:
            con.close()
    finally:
        get_settings.cache_clear()

    # Guards against passing vacuously on an empty or unregistered lake.
    assert total > 0, "no BRONZE rows scanned — the assertions below would pass trivially"
    assert bad_start == 0, (
        f"{bad_start} of {total} rows have billing_period_start != the charge month. "
        "SILVER drops the billing period on the assumption this never happens — see "
        "docs/architecture.md, 'Why the billing period stops at BRONZE'."
    )
    assert bad_end == 0, (
        f"{bad_end} of {total} rows have billing_period_end != the month after the "
        "charge month."
    )


def test_silver_does_not_expose_the_billing_period(monkeypatch, tmp_path) -> None:
    """The other half of the invariant: nothing downstream can group by it.

    Asserted on the view's own columns rather than on the SQL text, so it keeps holding
    however ``010_silver_focus.sql`` is rewritten.
    """
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    try:
        from flashlight.sample import load_sample

        load_sample()
        from flashlight.lake import duck
        from flashlight.transform.runner import SQL_DIR, _statements

        con = duck.connect()
        try:
            duck.register_bronze(con)
            con.execute("CREATE SCHEMA IF NOT EXISTS silver")
            for stmt in _statements((SQL_DIR / "010_silver_focus.sql").read_text()):
                con.execute(stmt)
            columns = {
                str(row[0]) for row in con.execute("DESCRIBE silver.focus_normalized").fetchall()
            }
        finally:
            con.close()
    finally:
        get_settings.cache_clear()

    assert "charge_month" in columns, "sanity: SILVER should expose the charge-period grain"
    leaked = {c for c in columns if "billing_period" in c}
    assert not leaked, (
        f"SILVER now exposes {sorted(leaked)}. Aggregating on the billing period is not "
        "additive — see docs/architecture.md, 'Why the billing period stops at BRONZE'."
    )
