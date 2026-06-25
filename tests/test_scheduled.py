"""Tests for the scheduled daily runner (slice J)."""

import fcntl
import os
import sqlite3

from financial_agent.scheduled import LOCK_FILENAME, run_scheduled_daily_sync
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);"
        "CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,"
        " amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);"
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('chk','PREMIER PLUS CKG (XXXX)','Chase','','USD')")
    ensure_app_schema(conn)
    conn.commit()
    conn.close()
    return str(path)


def test_runs_and_records_a_scheduled_run(tmp_path):
    db = _db(tmp_path / "s.sqlite")
    res = run_scheduled_daily_sync(db, lock_dir=str(tmp_path), as_of_date="2026-06-21")
    assert res["status"] == "completed"
    assert res["run"]["status"] in ("succeeded", "partial_success")

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT trigger_type, run_type FROM background_runs").fetchone()
    assert row == ("scheduled", "daily_sync")


def test_skips_when_lock_already_held(tmp_path):
    db = _db(tmp_path / "s.sqlite")
    # Hold the lock from the test, then the run must skip rather than block.
    held = open(os.path.join(str(tmp_path), LOCK_FILENAME), "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = run_scheduled_daily_sync(db, lock_dir=str(tmp_path), as_of_date="2026-06-21")
        assert res["status"] == "skipped_lock_held"
        assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM background_runs").fetchone()[0] == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_dry_run_does_not_run(tmp_path):
    db = _db(tmp_path / "s.sqlite")
    res = run_scheduled_daily_sync(db, lock_dir=str(tmp_path), as_of_date="2026-06-21", dry_run=True)
    assert res["status"] == "dry_run"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM background_runs").fetchone()[0] == 0


def test_lock_is_released_so_a_second_run_succeeds(tmp_path):
    db = _db(tmp_path / "s.sqlite")
    run_scheduled_daily_sync(db, lock_dir=str(tmp_path), as_of_date="2026-06-21")
    second = run_scheduled_daily_sync(db, lock_dir=str(tmp_path), as_of_date="2026-06-22")
    assert second["status"] == "completed"  # lock from the first run was released
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM background_runs").fetchone()[0] == 2
