"""Tests for the Todoist reflection layer (slice E): preview + durable outbox.

The hard rule under test: nothing ever sends to a live Todoist. The outbox
lifecycle is exercised entirely as preview / dry-run / awaiting-integration.
"""

import sqlite3
from datetime import datetime, timedelta

from financial_agent.obligations import apply_obligation_instances
from financial_agent.onboarding import scan_charge_onboarding_candidates
from financial_agent.schema import ensure_app_schema
from financial_agent.todoist_outbox import (
    enqueue_todoist_review_batch,
    execute_action_outbox,
    list_action_outbox,
    preview_todoist_review_batch,
)


def _insert_stale_daily_run(conn, *, hours_ago=30):
    """A successful daily run that finished N hours ago (stale past the 26h bar)."""

    finished = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat()
    conn.execute(
        "INSERT INTO background_runs (id,trace_id,run_type,trigger_type,status,as_of_date,started_at,finished_at,created_at) "
        "VALUES ('run_stale','trace_stale','daily_sync','manual','succeeded','2026-06-20',?,?,?)",
        (finished, finished, finished),
    )
    conn.commit()


def _insert_fresh_daily_run(conn):
    """A successful daily run that just finished: the heartbeat is fresh."""

    now = datetime.now().astimezone().isoformat()
    conn.execute(
        "INSERT INTO background_runs (id,trace_id,run_type,trigger_type,status,as_of_date,started_at,finished_at,created_at) "
        "VALUES ('run_fresh','trace_fresh','daily_sync','manual','succeeded','2026-06-20',?,?,?)",
        (now, now, now),
    )
    conn.commit()


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, currency TEXT);
        CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, posted TEXT, transacted_at TEXT,
            amount REAL, payee TEXT, description TEXT, pending INTEGER, source TEXT);
        """
    )
    conn.execute("INSERT INTO accounts (id,name,org,kind,currency) VALUES ('ACT-chk','PREMIER PLUS CKG (XXXX)','Chase','','USD')")
    conn.executemany(
        "INSERT INTO transactions (id,account_id,posted,amount,payee,description,pending,source) VALUES (?,?,?,?,?,?,0,'simplefin')",
        [
            ("n1", "ACT-chk", "2026-04-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
            ("n2", "ACT-chk", "2026-05-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
            ("n3", "ACT-chk", "2026-06-23T08:00:00", -30.30, "New York Times", "NYTIMES"),
        ],
    )
    conn.row_factory = sqlite3.Row
    ensure_app_schema(conn)
    # A past-due rent (missing_expected) and a discovered NYT candidate (unexpected_recurring).
    apply_obligation_instances(
        conn,
        obligation={"id": "rent", "name": "Rent check", "kind": "housing", "status": "active", "source": "seed"},
        instances=[{"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"}],
    )
    scan_charge_onboarding_candidates(conn)
    conn.commit()
    return conn


def test_preview_renders_batch_and_writes_nothing(tmp_path):
    conn = _db(tmp_path / "e.sqlite")
    batch = preview_todoist_review_batch(conn, as_of_date="2026-06-20")

    assert batch["parent_task"]["content"] == "Finance review 2026-06-20"
    types = {s["finding_type"] for s in batch["subtasks"]}
    assert "missing_expected" in types
    assert "unexpected_recurring" in types
    assert batch["item_count"] == len(batch["subtasks"])
    # The rendered subtask is human and specific.
    miss = next(s for s in batch["subtasks"] if s["finding_type"] == "missing_expected")
    assert "Confirm payment" in miss["content"] and "Rent check" in miss["content"]
    # Nothing was enqueued.
    assert list_action_outbox(conn) == []


def test_enqueue_dry_run_records_outbox_without_sending(tmp_path):
    conn = _db(tmp_path / "e.sqlite")
    result = enqueue_todoist_review_batch(conn, as_of_date="2026-06-20", dry_run=True)
    assert result["status"] == "dry_run"
    assert result["action"] == "created"

    outbox = list_action_outbox(conn)
    assert len(outbox) == 1
    assert outbox[0]["status"] == "dry_run"
    assert outbox[0]["dry_run"] is True
    assert outbox[0]["idempotency_key"] == "todoist_review_batch:2026-06-20"


def test_enqueue_is_idempotent_per_day(tmp_path):
    conn = _db(tmp_path / "e.sqlite")
    enqueue_todoist_review_batch(conn, as_of_date="2026-06-20", dry_run=True)
    second = enqueue_todoist_review_batch(conn, as_of_date="2026-06-20", dry_run=True)
    assert second["action"] == "unchanged"
    assert len(list_action_outbox(conn)) == 1  # no duplicate batch


def test_enqueue_non_dry_run_is_pending_but_not_sent(tmp_path):
    conn = _db(tmp_path / "e.sqlite")
    result = enqueue_todoist_review_batch(conn, as_of_date="2026-06-20", dry_run=False)
    assert result["status"] == "pending"
    assert list_action_outbox(conn, status="pending")[0]["dry_run"] is False


def test_execute_outbox_never_sends_without_integration(tmp_path):
    conn = _db(tmp_path / "e.sqlite")
    enqueue_todoist_review_batch(conn, as_of_date="2026-06-20", dry_run=False)  # pending

    # Hermetic: never read ambient config (so `pytest` under a write-enabled env
    # can never fire a live Todoist send).
    result = execute_action_outbox(conn, write_enabled=False, token=None, project_id=None)
    assert result["sent"] == 0
    assert result["integration_enabled"] is False
    assert result["awaiting_integration"] == 1
    assert list_action_outbox(conn)[0]["status"] == "no_integration_configured"


def test_execute_outbox_simulates_dry_run_items(tmp_path):
    conn = _db(tmp_path / "e.sqlite")
    enqueue_todoist_review_batch(conn, as_of_date="2026-06-20", dry_run=True)
    result = execute_action_outbox(conn, write_enabled=False, token=None, project_id=None)
    assert result["sent"] == 0
    assert result["simulated"] == 1
    assert list_action_outbox(conn)[0]["status"] == "simulated"


def test_min_severity_filters_subtasks(tmp_path):
    conn = _db(tmp_path / "e.sqlite")
    # A fresh daily heartbeat keeps the stale-job alert out of this batch so the
    # severity filter is the only thing under test.
    _insert_fresh_daily_run(conn)
    # high+ keeps the missing rent (high) but drops the low unexpected_recurring.
    batch = preview_todoist_review_batch(conn, as_of_date="2026-06-20", options={"min_severity": "high"})
    types = {s["finding_type"] for s in batch["subtasks"]}
    assert "missing_expected" in types
    assert "unexpected_recurring" not in types
    assert "stale_daily_job" not in types


# --- stale daily-job surfacing ---------------------------------------------


def test_stale_job_alert_in_preview_batch(tmp_path):
    """A stale daily job surfaces a HIGH-severity stale_daily_job subtask."""
    conn = _db(tmp_path / "e.sqlite")
    _insert_stale_daily_run(conn, hours_ago=30)

    batch = preview_todoist_review_batch(conn, as_of_date="2026-06-20")
    stale = [s for s in batch["subtasks"] if s["finding_type"] == "stale_daily_job"]
    assert len(stale) == 1
    assert stale[0]["severity"] == "high"
    assert "stale" in stale[0]["content"].lower()


def test_fresh_job_has_no_stale_alert(tmp_path):
    """A fresh daily heartbeat means no stale-job subtask in the batch."""
    conn = _db(tmp_path / "e.sqlite")
    _insert_fresh_daily_run(conn)

    batch = preview_todoist_review_batch(conn, as_of_date="2026-06-20")
    types = {s["finding_type"] for s in batch["subtasks"]}
    assert "stale_daily_job" not in types


def test_stale_job_alert_surfaced_in_enqueue(tmp_path):
    """The stale-job alert is carried in the durable outbox payload (dry run)."""
    conn = _db(tmp_path / "e.sqlite")
    _insert_stale_daily_run(conn, hours_ago=30)

    enqueue_todoist_review_batch(conn, as_of_date="2026-06-20", dry_run=True)
    outbox = list_action_outbox(conn)
    assert len(outbox) == 1
    payload = outbox[0]["payload"]
    sub_types = {s["finding_type"] for s in payload["subtasks"]}
    assert "stale_daily_job" in sub_types


def test_stale_job_alert_filtered_below_min_severity_does_not_apply(tmp_path):
    """stale_daily_job is HIGH, so min_severity=high still keeps it."""
    conn = _db(tmp_path / "e.sqlite")
    _insert_stale_daily_run(conn, hours_ago=30)
    batch = preview_todoist_review_batch(conn, as_of_date="2026-06-20", options={"min_severity": "high"})
    assert any(s["finding_type"] == "stale_daily_job" for s in batch["subtasks"])
