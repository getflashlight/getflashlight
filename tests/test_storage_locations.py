"""The Unity Catalog storage-location plane: URL parsing, record building, and the
partition-replace write.

The load-bearing property throughout is ``key_prefix is None`` meaning "this URL
addresses the bucket ROOT". Downstream that becomes ``mapping_confidence``, and the AWS
bill's S3 ResourceId is bucket-grained — so a prefix-scoped location can only ever yield
an upper bound on the platform's share of that bucket. Collapsing the distinction (to
``""``, say) would silently turn every upper bound into an exact figure.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from flashlight.core.settings import get_settings
from flashlight.ingest.connectors.databricks import _parse_storage_url
from flashlight.lake.storage_location_schema import (
    StorageLocationRecord,
    build_table,
    empty_table,
)


@pytest.fixture
def lake_home(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


# ── URL parsing ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Bucket root — the two forms that must yield key_prefix None.
        ("s3://acme-lakehouse", ("s3", "AWS", "acme-lakehouse", None)),
        ("s3://acme-lakehouse/", ("s3", "AWS", "acme-lakehouse", None)),
        # Prefix-scoped.
        ("s3://acme-lakehouse/uc/metastore", ("s3", "AWS", "acme-lakehouse", "uc/metastore")),
        ("s3a://bucket/p", ("s3a", "AWS", "bucket", "p")),
        ("s3n://bucket", ("s3n", "AWS", "bucket", None)),
        # Azure: the "bucket" must carry container AND account — a container name alone
        # isn't unique across storage accounts, so a cost row couldn't be identified.
        (
            "abfss://cont@acct.dfs.core.windows.net/path",
            ("abfss", "Microsoft", "cont@acct.dfs.core.windows.net", "path"),
        ),
        (
            "abfss://cont@acct.dfs.core.windows.net",
            ("abfss", "Microsoft", "cont@acct.dfs.core.windows.net", None),
        ),
        ("gs://gbucket/pre/fix", ("gs", "Google Cloud", "gbucket", "pre/fix")),
        # The legacy workspace DBFS root: a real location, but not resolvable to a
        # bucket here — a documented coverage gap, recorded rather than guessed at.
        ("dbfs:/mnt/thing", ("dbfs", None, None, None)),
        ("garbage", ("other", None, None, None)),
        ("", ("other", None, None, None)),
    ],
)
def test_parse_storage_url(
    url: str, expected: tuple[str, str | None, str | None, str | None]
) -> None:
    assert _parse_storage_url(url) == expected


def test_parse_storage_url_only_bucket_roots_have_no_prefix() -> None:
    """The distinction stated on its own, because everything downstream keys off it."""
    roots = ("s3://b", "s3://b/", "gs://b", "abfss://c@a.dfs.core.windows.net")
    prefixed = ("s3://b/x", "s3://b/x/y", "gs://b/x", "abfss://c@a.dfs.core.windows.net/x")
    for url in roots:
        assert _parse_storage_url(url)[3] is None, f"{url} should read as the bucket root"
    for url in prefixed:
        assert _parse_storage_url(url)[3] is not None, f"{url} should carry a key prefix"


# ── schema round-trip ─────────────────────────────────────────────────────────
def _record(**kw: Any) -> StorageLocationRecord:
    base: dict[str, Any] = {
        "provider_name": "Databricks",
        "snapshot_month": date(2026, 5, 20),  # normalized to the 1st
        "location_kind": "external_location",
        "location_name": "lake",
        "url": "s3://acme/dbx",
        "scheme": "s3",
        "cloud_provider_name": "AWS",
        "bucket_name": "acme",
        "key_prefix": "dbx",
        "x_source_connector": "databricks",
    }
    return StorageLocationRecord(**{**base, **kw})


def test_storage_location_schema_round_trip() -> None:
    table = build_table([_record(is_read_only=True, credential_name="cred")])
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["location_kind"] == "external_location"
    assert row["bucket_name"] == "acme"
    assert row["key_prefix"] == "dbx"
    assert row["is_read_only"] is True
    assert row["credential_name"] == "cred"
    assert row["provider_name"] == "Databricks"
    assert row["snapshot_month"] == "2026-05"  # first-of-month normalization


def test_storage_location_bucket_root_keeps_null_prefix_through_arrow() -> None:
    """NULL must survive the Arrow round-trip — an empty string here would read as
    'a prefix named ""' and downgrade a whole-bucket mapping to prefix-scoped."""
    row = build_table([_record(key_prefix=None, url="s3://acme")]).to_pylist()[0]
    assert row["key_prefix"] is None


def test_storage_location_empty_table_is_typed() -> None:
    table = empty_table()
    assert table.num_rows == 0
    assert table.schema.names == build_table([]).schema.names


# ── the Databricks pull ───────────────────────────────────────────────────────
def _connector(monkeypatch: Any, client: Any) -> Any:
    """A DatabricksConnector whose ``_client`` is a stub Unity Catalog surface.

    ``WorkspaceClient`` is patched at the module before construction because ``__init__``
    builds it eagerly and the real SDK blocks on auth discovery against a fake host —
    same seam ``test_databricks_focus._connector`` uses. No ``_execute`` stub needed
    here: this pull is pure REST and never touches a SQL warehouse, which is one of the
    points of implementing it this way.
    """
    from flashlight.ingest import config as config_mod
    from flashlight.ingest.connectors import databricks as db_mod

    monkeypatch.setattr(db_mod, "WorkspaceClient", lambda **_kw: client)
    monkeypatch.setenv("DATABRICKS_TOKEN", "tok")
    return db_mod.DatabricksConnector(
        config_mod.DatabricksConfig(
            type="databricks",
            host="https://example.cloud.databricks.com",
            token_env="DATABRICKS_TOKEN",
        )
    )


def _catalog(name: str, root: str | None, *, ctype: str = "MANAGED_CATALOG") -> Any:
    """A CatalogInfo-shaped stub. ``catalog_type`` is an enum-like on the real SDK, so the
    stub carries a ``.value`` too — it's what decides managed vs foreign."""
    return SimpleNamespace(
        name=name,
        storage_root=root,
        storage_location=None,
        catalog_type=SimpleNamespace(value=ctype) if ctype else None,
    )


def _client(
    *,
    metastores: list[Any] | None = None,
    summary: Any = None,
    catalogs: list[Any] | None = None,
    locations: list[Any] | None = None,
    metastores_list_raises: bool = True,
    summary_raises: bool = False,
    locations_raise: bool = False,
) -> Any:
    """A stub Unity Catalog surface.

    ``metastores_list_raises`` defaults True because ``metastores.list()`` is admin-only:
    most tests exercise the non-admin fallback to ``summary()``, and the ones that care
    about the complete-coverage rung pass ``metastores=[...]`` explicitly.
    """

    def _list_metastores() -> list[Any]:
        if metastores_list_raises:
            raise RuntimeError("PERMISSION_DENIED: only account admins may list metastores")
        return metastores or []

    def _summary() -> Any:
        if summary_raises:
            raise RuntimeError("PERMISSION_DENIED: metastore summary")
        return summary

    def _list_locations() -> list[Any]:
        if locations_raise:
            raise RuntimeError("PERMISSION_DENIED: external locations")
        return locations or []

    return SimpleNamespace(
        metastores=SimpleNamespace(list=_list_metastores, summary=_summary),
        catalogs=SimpleNamespace(list=lambda: catalogs or []),
        external_locations=SimpleNamespace(list=_list_locations),
    )


def _window() -> Any:
    from flashlight.ingest.base import IngestWindow

    return IngestWindow(date(2026, 5, 1), date(2026, 5, 31))


def test_fetch_storage_locations_maps_every_uc_surface(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _client(
        summary=SimpleNamespace(name="acme-metastore", storage_root="s3://acme-root"),
        catalogs=[_catalog("main", "s3://acme-root/main")],
        locations=[
            SimpleNamespace(
                name="landing",
                url="s3://acme-landing/incoming",
                read_only=True,
                credential_name="cred-1",
            )
        ],
    )
    records = list(_connector(monkeypatch, client).fetch_storage_locations(_window()))

    by_kind = {r.location_kind: r for r in records}
    assert set(by_kind) == {"metastore_root", "catalog", "external_location"}
    assert by_kind["metastore_root"].bucket_name == "acme-root"
    assert by_kind["metastore_root"].key_prefix is None  # the root itself
    assert by_kind["catalog"].key_prefix == "main"
    assert by_kind["external_location"].is_read_only is True
    assert by_kind["external_location"].credential_name == "cred-1"
    assert {r.provider_name for r in records} == {"Databricks"}
    # Stamped with the month it ran in, not a charge period.
    assert {r.snapshot_month for r in records} == {date.today().replace(day=1)}


def test_fetch_storage_locations_survives_one_missing_grant(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A token that can't read the metastore summary must still yield what it CAN see.

    Losing one source degrades coverage; it must not blank the map, because an empty map
    is what makes the dashboard say "no storage locations" — which a reader will hear as
    "Databricks has no storage cost".
    """
    client = _client(
        summary_raises=True,
        locations=[
            SimpleNamespace(
                name="landing", url="s3://acme-landing", read_only=False, credential_name=None
            )
        ],
    )
    records = list(_connector(monkeypatch, client).fetch_storage_locations(_window()))

    assert [r.location_kind for r in records] == ["external_location"]
    assert records[0].bucket_name == "acme-landing"


def test_fetch_storage_locations_yields_nothing_when_every_source_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _client(summary_raises=True, locations_raise=True)
    assert list(_connector(monkeypatch, client).fetch_storage_locations(_window())) == []


def test_fetch_storage_locations_dedupes_identical_urls(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A catalog whose storage_root equals the metastore root is common; counted twice it
    would inflate the tab's "N UC locations" figure for that bucket."""
    client = _client(
        summary=SimpleNamespace(name="acme", storage_root="s3://acme-root"),
        catalogs=[_catalog("main", "s3://acme-root"), _catalog("main", "s3://acme-root")],
    )
    records = list(_connector(monkeypatch, client).fetch_storage_locations(_window()))

    # metastore_root + exactly one catalog row (same kind+name+url collapses).
    assert len(records) == 2
    assert sorted(r.location_kind for r in records) == ["catalog", "metastore_root"]


def test_fetch_storage_locations_falls_back_to_storage_location(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _client(
        catalogs=[
            SimpleNamespace(
                name="legacy",
                storage_root=None,
                storage_location="s3://legacy/root",
                catalog_type=SimpleNamespace(value="MANAGED_CATALOG"),
            )
        ]
    )
    records = list(_connector(monkeypatch, client).fetch_storage_locations(_window()))
    assert records[0].bucket_name == "legacy"


# ── the write ─────────────────────────────────────────────────────────────────
def test_write_storage_locations_replaces_only_its_own_snapshot(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A re-pull replaces the current month's map, and leaves earlier snapshots alone.

    This is why the writer takes no IngestWindow: purging a window would delete five
    older snapshots during a six-month backfill while writing only one.
    """
    from flashlight.lake.storage_locations import write_storage_locations

    write_storage_locations([_record(snapshot_month=date(2026, 4, 1), location_name="april")])
    write_storage_locations([_record(snapshot_month=date(2026, 5, 1), location_name="may-v1")])
    write_storage_locations([_record(snapshot_month=date(2026, 5, 1), location_name="may-v2")])

    from flashlight.lake import duck

    con = duck.connect()
    try:
        duck.register_storage_locations(con)
        rows = con.execute(
            "SELECT snapshot_month, location_name FROM metrics.storage_location "
            "ORDER BY snapshot_month"
        ).fetchall()
    finally:
        con.close()

    assert rows == [("2026-04", "april"), ("2026-05", "may-v2")]


def test_write_storage_locations_empty_pull_keeps_the_existing_map(lake_home) -> None:  # type: ignore[no-untyped-def]
    """An empty pull must NOT purge.

    Unlike a cost window — where "the source no longer reports this month" is real
    information and self-purging is the point — an empty metadata pull means the API call
    failed or the token lacks a grant. Deleting a good map would turn a transient
    permission problem into permanent data loss, and would make the tab imply Databricks
    has no storage cost.
    """
    from flashlight.lake.storage_locations import write_storage_locations

    write_storage_locations([_record(snapshot_month=date(2026, 5, 1), location_name="keep-me")])
    assert write_storage_locations([]) == 0

    from flashlight.lake import duck

    con = duck.connect()
    try:
        duck.register_storage_locations(con)
        names = [r[0] for r in con.execute(
            "SELECT location_name FROM metrics.storage_location"
        ).fetchall()]
    finally:
        con.close()

    assert names == ["keep-me"]


def test_fetch_storage_locations_captures_every_metastore_not_just_this_workspace(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """``metastores.list()`` is preferred over ``summary()`` because summary() returns ONLY
    the metastore assigned to the workspace this connector points at.

    The real bug: an account with production and development metastores attached to
    different workspaces
    reported one of them, and the other's bucket sat in `unmapped` looking like it wasn't
    Databricks storage at all.
    """
    client = _client(
        metastores_list_raises=False,
        metastores=[
            SimpleNamespace(
                name="production-metastore",
                storage_root="s3://production-example-bucket/metastore/d5f",
            ),
            SimpleNamespace(
                name="development-metastore",
                storage_root="s3://development-example-bucket/metastore/a0b",
            ),
        ],
        # summary() would only ever have returned the production one.
        summary=SimpleNamespace(
            name="production-metastore",
            storage_root="s3://production-example-bucket/metastore/d5f",
        ),
    )
    records = list(_connector(monkeypatch, client).fetch_storage_locations(_window()))

    roots = {r.bucket_name for r in records if r.location_kind == "metastore_root"}
    assert roots == {"production-example-bucket", "development-example-bucket"}


def test_fetch_storage_locations_falls_back_to_summary_for_a_non_admin_token(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """``metastores.list()`` is admin-only, so a non-admin token must still get its own
    workspace's metastore rather than nothing at all."""
    client = _client(
        metastores_list_raises=True,
        summary=SimpleNamespace(
            name="production-metastore", storage_root="s3://production-example-bucket/m"
        ),
    )
    records = list(_connector(monkeypatch, client).fetch_storage_locations(_window()))

    assert [r.bucket_name for r in records if r.location_kind == "metastore_root"] == [
        "production-example-bucket"
    ]


@pytest.mark.parametrize(
    ("catalog_type", "expected_kind"),
    [
        ("MANAGED_CATALOG", "catalog"),  # Databricks provisioned it → costed
        ("FOREIGN_CATALOG", "foreign_catalog"),  # Glue/Hive federation → NOT costed
        ("DELTASHARING_CATALOG", "foreign_catalog"),
        ("SYSTEM_CATALOG", "foreign_catalog"),
    ],
)
def test_fetch_storage_locations_splits_managed_from_foreign_catalogs(  # type: ignore[no-untyped-def]
    monkeypatch, catalog_type: str, expected_kind: str
) -> None:
    """Only a MANAGED_CATALOG's storage is Databricks-owned.

    A federated catalog appears in the same `catalogs.list()` response but points at data
    that already existed — measured on a real account, one federated Glue catalog's bucket
    cost 5x the managed catalogs combined, so costing it would have charged another team's
    data lake to Databricks. The kinds are split here, at collection, so the GOLD filter
    stays a simple kind list.
    """
    client = _client(catalogs=[_catalog("cat", "s3://cat-bucket", ctype=catalog_type)])
    records = list(_connector(monkeypatch, client).fetch_storage_locations(_window()))

    assert [r.location_kind for r in records] == [expected_kind]
    # Either way it IS recorded — the inventory is the audit trail for why a bucket is or
    # isn't counted.
    assert records[0].bucket_name == "cat-bucket"
