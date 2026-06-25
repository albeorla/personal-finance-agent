"""Tests for the background runner + telemetry (slice F)."""

import sqlite3
from datetime import datetime, timedelta

import financial_agent.background as background
from financial_agent.background import (
    get_background_run,
    get_job_health,
    list_background_runs,
    run_background_sync,
)
from financial_agent.obligations import apply_obligation_instances
from financial_agent.schema import ensure_app_schema


def _insert_run(conn, *, run_id, status, hours_ago, run_type="daily_sync"):
    """Insert a completed background run whose finished_at is N hours before now."""

    finished = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat()
    started = (datetime.now().astimezone() - timedelta(hours=hours_ago + 0.1)).isoformat()
    conn.execute(
        "INSERT INTO background_runs (id,trace_id,run_type,trigger_type,status,as_of_date,started_at,finished_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, f"trace_{run_id}", run_type, "manual", status, "2026-06-30", started, finished, started),
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
    apply_obligation_instances(
        conn,
        obligation={"id": "rent", "name": "Rent check", "kind": "housing", "status": "active", "source": "seed"},
        instances=[{"id": "rent:2026-06-01", "due_date": "2026-06-01", "amount": -3000.0, "source": "seed"}],
    )
    conn.commit()
    return conn


_EXPECTED_SEQUENCE = [
    "run_started", "scan_charge_candidates", "reconcile", "detect_drift",
    "suppress_dormant_estimates", "preview_review_batch", "surface_due_items",
    "run_finished",
]


def test_run_background_sync_records_run_and_ordered_events(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    result = run_background_sync(conn, as_of_date="2026-06-30")

    assert result["status"] == "succeeded"
    assert result["errors"] == 0
    assert result["run_id"].startswith("run_")
    assert result["trace_id"].startswith("trace_")
    assert result["duration_ms"] >= 0
    # All four pipeline steps ran and produced summaries.
    assert set(result["result_summary"]) == {
        "scan_charge_candidates", "reconcile", "detect_drift",
        "suppress_dormant_estimates", "preview_review_batch", "surface_due_items",
    }
    assert result["result_summary"]["scan_charge_candidates"]["created"] == 1
    # With Todoist write-back gated off (the default), surfacing makes no live
    # call and reports awaiting-integration.
    assert result["result_summary"]["surface_due_items"]["status"] == "awaiting-integration"

    run = get_background_run(conn, result["run_id"])
    assert [e["event_type"] for e in run["events"]] == _EXPECTED_SEQUENCE
    assert all(e["status"] in ("ok", "succeeded") for e in run["events"])


def test_event_sequence_is_deterministic_across_runs(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    first = run_background_sync(conn, as_of_date="2026-06-30")
    second = run_background_sync(conn, as_of_date="2026-06-30")

    assert first["run_id"] != second["run_id"]  # each run is its own record
    seq1 = [e["event_type"] for e in get_background_run(conn, first["run_id"])["events"]]
    seq2 = [e["event_type"] for e in get_background_run(conn, second["run_id"])["events"]]
    assert seq1 == seq2 == _EXPECTED_SEQUENCE
    # Idempotent pipeline: the second scan creates nothing new.
    assert second["result_summary"]["scan_charge_candidates"]["created"] == 0


def test_list_background_runs(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    run_background_sync(conn, as_of_date="2026-06-30")
    run_background_sync(conn, as_of_date="2026-07-01")
    runs = list_background_runs(conn, run_type="daily_sync")
    assert len(runs) == 2
    assert all(r["status"] == "succeeded" for r in runs)


def test_step_error_yields_partial_success_and_run_continues(tmp_path, monkeypatch):
    conn = _db(tmp_path / "b.sqlite")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated reconcile failure")

    monkeypatch.setattr(background, "reconcile_obligation_instances", _boom)
    result = run_background_sync(conn, as_of_date="2026-06-30")

    assert result["status"] == "partial_success"
    assert result["errors"] == 1
    assert "error" in result["result_summary"]["reconcile"]

    run = get_background_run(conn, result["run_id"])
    # The run still completed every step despite the failure.
    assert [e["event_type"] for e in run["events"]] == _EXPECTED_SEQUENCE
    reconcile_event = next(e for e in run["events"] if e["event_type"] == "reconcile")
    assert reconcile_event["status"] == "error"
    # Later steps still ran.
    assert "detect_drift" in result["result_summary"]


def test_get_background_run_unknown_returns_none(tmp_path):
    conn = _db(tmp_path / "b.sqlite")
    assert get_background_run(conn, "run_does_not_exist") is None


_EXPECTED_WITH_SYNC = [
    "run_started", "sync_simplefin", "sync_todoist", "scan_charge_candidates",
    "reconcile", "detect_drift", "suppress_dormant_estimates",
    "preview_review_batch", "surface_due_items", "run_finished",
]


def test_sync_steps_run_when_enabled_and_configured(tmp_path, monkeypatch):
    conn = _db(tmp_path / "b.sqlite")
    monkeypatch.setattr(background, "get_finance_config", lambda **k: {"has_simplefin": True, "has_todoist": True})
    monkeypatch.setattr(background, "sync_simplefin", lambda *a, **k: {"accounts": 9, "inserted": 700, "updated": 0, "error": None})
    monkeypatch.setattr(background, "sync_todoist", lambda *a, **k: {"tasks_seen": 20, "cashflow_tasks_seen": 7, "inserted": 20, "updated": 0, "error": None})

    result = run_background_sync(conn, as_of_date="2026-06-30", options={"sync": True})
    assert result["status"] == "succeeded"
    assert result["result_summary"]["sync_simplefin"]["accounts"] == 9
    assert result["result_summary"]["sync_todoist"]["tasks_seen"] == 20
    run = get_background_run(conn, result["run_id"])
    assert [e["event_type"] for e in run["events"]] == _EXPECTED_WITH_SYNC


def test_sync_steps_skipped_when_not_configured(tmp_path, monkeypatch):
    conn = _db(tmp_path / "b.sqlite")
    monkeypatch.setattr(background, "get_finance_config", lambda **k: {"has_simplefin": False, "has_todoist": False})
    result = run_background_sync(conn, as_of_date="2026-06-30", options={"sync": True})
    # Steps still appear in the timeline but no network call is made.
    assert "skipped" in result["result_summary"]["sync_simplefin"]
    assert "skipped" in result["result_summary"]["sync_todoist"]


def test_run_background_sync_suppresses_dormant_estimate(tmp_path):
    """Dormancy suppression runs as part of the pipeline and is recorded."""
    import json as _json

    from financial_agent.obligations import apply_obligation_instances as _apply

    conn = _db(tmp_path / "bg_dormant.sqlite")
    # Source account that has gone dormant: zero balance, no recent transactions.
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency) VALUES "
        "('chase_amazon','Chase Amazon','Chase','credit_card','USD')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS balance_snapshots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, balance REAL, "
        "available REAL, recorded_at TEXT, source TEXT)"
    )
    conn.execute(
        "INSERT INTO balance_snapshots (account_id,balance,available,recorded_at,source) "
        "VALUES ('chase_amazon',0.0,0.0,'2026-06-23T00:00:00+00:00','simplefin')"
    )
    conn.execute(
        """
        INSERT INTO charge_onboarding_candidates (
            id, merchant_key, display_name, direction, status, candidate_type,
            cash_flow_treatment, proposed_cash_impact_policy_json, evidence_count,
            existing_obligation_id, created_at, updated_at
        ) VALUES ('cand_chase','chase','Chase Card Payment','outflow','applied',
                  'card_statement_input','direct_checking', ?, 3,
                  'onboarded_chase','2026-01-01T00:00:00','2026-01-01T00:00:00')
        """,
        (_json.dumps({"evidence_account_ids": ["chase_amazon"]}, sort_keys=True),),
    )
    _apply(
        conn,
        obligation={
            "id": "onboarded_chase", "name": "Chase card payment estimate",
            "kind": "bill", "cadence": "monthly", "status": "active",
            "source": "charge_onboarding:cand_chase",
        },
        instances=[{
            "id": "onboarded_chase:2026-07-10", "due_date": "2026-07-10",
            "amount": -1162.0, "direction": "outflow", "status": "expected",
            "source": "charge_onboarding:cand_chase",
            "amount_status": "estimated", "estimation_method": "average",
        }],
    )
    conn.commit()

    result = run_background_sync(conn, as_of_date="2026-06-24")

    summary = result["result_summary"]["suppress_dormant_estimates"]
    assert summary["suppressed_count"] == 1
    assert conn.execute(
        "SELECT status FROM obligations WHERE id='onboarded_chase'"
    ).fetchone()["status"] == "dormant_suppressed"

    run = get_background_run(conn, result["run_id"])
    event = next(e for e in run["events"] if e["event_type"] == "suppress_dormant_estimates")
    assert event["status"] == "ok"
    assert event["event_data"]["suppressed_count"] == 1


# --- job health / heartbeat -------------------------------------------------


def test_get_job_health_fresh_run(tmp_path):
    """A successful daily run just completed: the job is healthy, not stale."""
    conn = _db(tmp_path / "h.sqlite")
    result = run_background_sync(conn, as_of_date="2026-06-30")

    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is True
    assert health["is_stale"] is False
    assert health["last_run_id"] == result["run_id"]
    assert health["last_run_status"] == "succeeded"
    assert health["hours_since_last_run"] is not None
    assert health["hours_since_last_run"] < 1


def test_get_job_health_stale_run(tmp_path):
    """The last successful run finished 30h ago: stale and unhealthy."""
    conn = _db(tmp_path / "h.sqlite")
    _insert_run(conn, run_id="run_old", status="succeeded", hours_ago=30)

    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is False
    assert health["is_stale"] is True
    assert health["last_run_id"] == "run_old"
    assert health["hours_since_last_run"] >= 26


def test_get_job_health_no_runs(tmp_path):
    """No daily run on record: unhealthy, stale, with no last run."""
    conn = _db(tmp_path / "h.sqlite")
    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is False
    assert health["is_stale"] is True
    assert health["last_run_id"] is None
    assert health["hours_since_last_run"] is None


def test_partial_success_run_counts_as_heartbeat(tmp_path, monkeypatch):
    """A run that completes with partial_success still saw the data, so a same-day
    partial run keeps the job healthy."""
    conn = _db(tmp_path / "h.sqlite")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated reconcile failure")

    monkeypatch.setattr(background, "reconcile_obligation_instances", _boom)
    result = run_background_sync(conn, as_of_date="2026-06-30")
    assert result["status"] == "partial_success"

    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["healthy"] is True
    assert health["is_stale"] is False
    assert health["last_run_status"] == "partial_success"


def test_get_job_health_old_partial_success_is_stale(tmp_path):
    """If only an OLD partial_success run exists, the job is still stale."""
    conn = _db(tmp_path / "h.sqlite")
    _insert_run(conn, run_id="run_partial_old", status="partial_success", hours_ago=30)
    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["is_stale"] is True
    assert health["last_run_status"] == "partial_success"


def test_stale_threshold_boundary(tmp_path):
    """26h exactly is not stale; just over the threshold is."""
    conn = _db(tmp_path / "h.sqlite")
    _insert_run(conn, run_id="run_at_threshold", status="succeeded", hours_ago=26)
    at = get_job_health(conn, as_of_date="2026-06-30")
    assert at["is_stale"] is False

    conn.execute("DELETE FROM background_runs")
    _insert_run(conn, run_id="run_over", status="succeeded", hours_ago=26.05)
    over = get_job_health(conn, as_of_date="2026-06-30")
    assert over["is_stale"] is True


def test_in_progress_run_does_not_count_as_heartbeat(tmp_path):
    """An in-progress run (finished_at NULL) is not a completed heartbeat."""
    conn = _db(tmp_path / "h.sqlite")
    conn.execute(
        "INSERT INTO background_runs (id,trace_id,run_type,trigger_type,status,as_of_date,started_at,created_at) "
        "VALUES ('run_inflight','t','daily_sync','manual','in_progress','2026-06-30',?,?)",
        (datetime.now().astimezone().isoformat(), datetime.now().astimezone().isoformat()),
    )
    conn.commit()
    health = get_job_health(conn, as_of_date="2026-06-30")
    assert health["is_stale"] is True
    assert health["last_run_id"] is None
