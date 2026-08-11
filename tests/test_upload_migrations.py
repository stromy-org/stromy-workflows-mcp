"""The facade's own migration chain (ORG-PLAN-164 / ORG-191 ownership rule).

Two chains share one database and one ledger. What can go wrong is not "the SQL
is invalid" — Postgres catches that on the first apply — but the *ownership*
boundary quietly moving: this chain reaching into a table the shared lifecycle
owns, or its namespace drifting so the ledger reads it as never applied. Both are
silent, both are asserted here, and neither needs a database.

The apply path itself is exercised where a real Postgres exists (the core's
``apply_app_migrations`` suite, and Stromy's integration conftest, which applies
this exact chain shape to stand in for this deployable).
"""

from __future__ import annotations

import re

import pytest

from stromy_workflows_mcp import migrations
from stromy_workflows_mcp.registry import RegistryError

#: Tables the shared lifecycle owns. This chain must never mention one: a facade
#: migration that altered ``runs`` would apply under its own namespace, so the
#: core's checksum ledger would have no record of the change and no way to refuse
#: a build that disagrees with it — the exact blindness the fork exploited.
CORE_OWNED = ("runs", "run_events", "schema_meta", "schema_migrations")


def test_the_namespace_is_this_app_and_not_the_core() -> None:
    assert migrations.NAMESPACE == "stromy_workflows_mcp"
    assert migrations.NAMESPACE != "core"
    # Same grammar the core enforces, checked here so a rename fails a test rather
    # than an apply against production.
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", migrations.NAMESPACE)


def test_versions_are_contiguous_from_one() -> None:
    """A gap makes the applied-version check skip a step silently."""
    assert [m.version for m in migrations.MIGRATIONS] == list(
        range(1, len(migrations.MIGRATIONS) + 1)
    )
    assert migrations.LATEST_VERSION == len(migrations.MIGRATIONS)


def test_the_chain_only_creates_tables_this_app_owns() -> None:
    sql = "\n".join(m.sql for m in migrations.MIGRATIONS)
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
    assert created == {"input_sessions", "input_files"}


def test_the_chain_never_touches_a_core_owned_table() -> None:
    sql = "\n".join(m.sql for m in migrations.MIGRATIONS).lower()
    for table in CORE_OWNED:
        assert not re.search(rf"\b(alter|drop|create)\s+table[^;]*\b{table}\b", sql), (
            f"this chain modifies {table}, which the core owns — route it through "
            "workflow-runtime-core's chain instead"
        )


def test_the_capability_token_is_stored_only_as_a_hash() -> None:
    """A registry dump must not be a working set of upload capabilities."""
    sql = "\n".join(m.sql for m in migrations.MIGRATIONS)
    assert "capability_token_hash" in sql
    assert not re.search(r"\bcapability_token\b(?!_hash)", sql)


# --- the readiness gate -------------------------------------------------------


def test_require_applied_returns_the_live_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrations, "live_version", lambda _conn: migrations.LATEST_VERSION)
    assert migrations.require_applied(object()) == migrations.LATEST_VERSION  # type: ignore[arg-type]


def test_require_applied_refuses_an_unmigrated_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving with no upload tables accepts every session call and fails each at
    its first write, which reads as "uploads are broken" rather than "this
    deployment was never migrated"."""
    monkeypatch.setattr(migrations, "live_version", lambda _conn: None)
    with pytest.raises(RegistryError, match=r"python -m stromy_workflows_mcp.migrations"):
        migrations.require_applied(object())  # type: ignore[arg-type]


def test_require_applied_refuses_a_stale_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrations, "live_version", lambda _conn: migrations.LATEST_VERSION - 1)
    with pytest.raises(RegistryError, match=f"needs v{migrations.LATEST_VERSION}"):
        migrations.require_applied(object())  # type: ignore[arg-type]
