"""Tests for the scheduled daily runner (slice J)."""

import fcntl
import os
import sqlite3

import financial_agent.scheduled as scheduled
from financial_agent.scheduled import LOCK_FILENAME, run_scheduled_daily_sync
from financial_agent.schema import ensure_app_schema


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);"
        "CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,"
        " amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);"
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('chk','PREMIER PLUS CKG (4321)','Chase','','USD')")
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


def test_live_runner_enables_sync_surface_and_completion_reconciliation(tmp_path, monkeypatch):
    db = _db(tmp_path / "s.sqlite")
    seen = {}

    def fake_background(conn, **kwargs):
        seen.update(kwargs)
        return {
            "run_id": "run_test",
            "status": "succeeded",
            "duration_ms": 12,
            "result_summary": {
                "sync_simplefin": {"accounts": 2, "inserted": 3, "updated": 0},
                "surface_due_items": {"status": "ok", "created": 1, "failed": 0},
                "reconcile_todoist_completions": {"status": "ok", "resolved": 1, "failed": 0},
            },
        }

    monkeypatch.setattr(scheduled, "run_background_sync", fake_background)
    result = run_scheduled_daily_sync(
        db,
        lock_dir=str(tmp_path),
        as_of_date="2026-07-09",
        sync=True,
        surface=True,
    )

    assert seen["options"]["sync"] is True
    assert seen["options"]["surface"] == {"write_enabled": None}
    assert result["semantic_status"] == "ok"
    assert result["phases"]["sync"]["status"] == "ok"
    assert result["phases"]["surface"]["status"] == "ok"
    assert result["phases"]["reconcile_completions"]["status"] == "ok"


def test_sync_error_is_a_visible_warning_and_not_fresh(tmp_path, monkeypatch):
    db = _db(tmp_path / "s.sqlite")

    monkeypatch.setattr(
        scheduled,
        "run_background_sync",
        lambda conn, **kwargs: {
            "run_id": "run_warn",
            "status": "succeeded_with_warnings",
            "duration_ms": 10,
            "result_summary": {
                "sync_simplefin": {"error": "source unavailable"},
                "surface_due_items": {"status": "ok", "created": 0, "failed": 0},
                "reconcile_todoist_completions": {"status": "ok", "resolved": 0, "failed": 0},
            },
        },
    )
    result = run_scheduled_daily_sync(
        db,
        lock_dir=str(tmp_path),
        as_of_date="2026-07-09",
        sync=True,
        surface=True,
    )

    assert result["semantic_status"] == "warn"
    assert result["phases"]["sync"]["status"] == "failed"
    assert result["fresh_for_exports"] is False


def test_dry_run_reports_every_phase_without_running_background(tmp_path, monkeypatch):
    db = _db(tmp_path / "s.sqlite")
    monkeypatch.setattr(
        scheduled,
        "run_background_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("background ran")),
    )
    result = run_scheduled_daily_sync(
        db,
        lock_dir=str(tmp_path),
        as_of_date="2026-07-09",
        dry_run=True,
        sync=True,
        surface=True,
    )
    assert result["status"] == "dry_run"
    assert result["semantic_status"] == "ok"
    assert set(result["phases"]) == {"sync", "pipeline", "surface", "reconcile_completions"}
