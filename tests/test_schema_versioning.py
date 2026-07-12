"""Unit tests for the PRAGMA user_version migration driver in schema.py.

The full suite already exercises ensure_app_schema indirectly (every test builds
the schema). These tests assert the version logic itself: a fresh DB lands at the
latest version, re-running is a true no-op, only newer steps run, and an existing
v0 DB converges without losing data.
"""

from __future__ import annotations

import sqlite3

import pytest

from financial_agent import schema
from financial_agent.schema import (
    LATEST_SCHEMA_VERSION,
    ensure_app_schema,
    get_schema_version,
    has_app_schema,
)


def _mem() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def test_latest_version_matches_registry():
    # LATEST_SCHEMA_VERSION must equal the max target in the ordered registry,
    # or a fresh DB would not land at LATEST.
    assert LATEST_SCHEMA_VERSION == max(v for v, _ in schema._MIGRATIONS)


def test_fresh_db_lands_at_latest():
    conn = _mem()
    assert get_schema_version(conn) == 0  # brand-new DB
    ensure_app_schema(conn)
    assert get_schema_version(conn) == LATEST_SCHEMA_VERSION
    assert has_app_schema(conn) is True


def test_rerun_is_a_noop(monkeypatch):
    conn = _mem()
    ensure_app_schema(conn)
    assert get_schema_version(conn) == LATEST_SCHEMA_VERSION

    # Spy: wrap every registered migration so we can count re-applications.
    calls: list[int] = []
    wrapped = [
        (version, (lambda fn, ver: (lambda c: (calls.append(ver), fn(c))[1]))(fn, version))
        for version, fn in schema._MIGRATIONS
    ]
    monkeypatch.setattr(schema, "_MIGRATIONS", wrapped)

    ensure_app_schema(conn)  # already up to date
    assert calls == []  # no migration re-ran
    assert get_schema_version(conn) == LATEST_SCHEMA_VERSION


def test_only_newer_steps_run(monkeypatch):
    conn = _mem()
    # Pretend the DB is already past every known migration.
    conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 5}")

    calls: list[int] = []
    wrapped = [
        (version, (lambda fn, ver: (lambda c: (calls.append(ver), fn(c))[1]))(fn, version))
        for version, fn in schema._MIGRATIONS
    ]
    monkeypatch.setattr(schema, "_MIGRATIONS", wrapped)

    ensure_app_schema(conn)
    assert calls == []  # nothing older-or-equal should run
    # Driver must not lower a higher user_version.
    assert get_schema_version(conn) == LATEST_SCHEMA_VERSION + 5


def test_existing_v0_db_converges_without_data_loss():
    conn = _mem()
    ensure_app_schema(conn)  # build the real schema first

    # Simulate a legacy local DB: schema present but never version-stamped.
    conn.execute("PRAGMA user_version = 0")
    conn.execute(
        "INSERT INTO obligations "
        "(id, name, kind, cadence, status, source, created_at, updated_at) "
        "VALUES ('ob_keep', 'Keep me', 'bill', 'monthly', 'active', 'manual', "
        "'2026-06-25T00:00:00Z', '2026-06-25T00:00:00Z')"
    )
    conn.commit()

    ensure_app_schema(conn)  # re-run the driver over the v0 DB

    assert get_schema_version(conn) == LATEST_SCHEMA_VERSION
    rows = conn.execute("SELECT id FROM obligations WHERE id = 'ob_keep'").fetchall()
    assert len(rows) == 1  # baseline migration is idempotent: no data lost


def test_execute_script_rejects_incomplete_trailing_sql():
    conn = _mem()

    with pytest.raises(sqlite3.Error):
        schema._execute_script(conn, "CREATE TABLE silently_dropped (id INTEGER)")

    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'silently_dropped'"
    ).fetchone() is None


def test_schema_migrations_can_be_rolled_back(tmp_path):
    db = tmp_path / "rollback.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("BEGIN")

    ensure_app_schema(conn)
    conn.rollback()
    conn.close()

    reopened = sqlite3.connect(db)
    try:
        assert get_schema_version(reopened) == 0
        assert has_app_schema(reopened) is False
    finally:
        reopened.close()
